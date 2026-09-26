"""家庭土地权益额度账本的事务用例。

按家庭隔离记录权益来源、适用地类、有效期、冻结与实际消耗；
分配确认时按规则选择可用额度并与地块预留原子落账；
跨年度项目用可注入时钟切分预计占用；退出或失败只返还尚未交付部分；
超额申请进入有期限的复核队列，复核人不得批准自己提交的例外；
规则更新不能回写已经结算的年度。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .ledger import (
    QuotaCandidate,
    canonical_json,
    decimal_text,
    digest,
    quantize_mu,
    select_quota,
    split_yearly_occupancy,
    summarize_balances,
)
from .models import ApplicationInput, DeliveryInput, GrantInput, RuleInput, identifier, land_category
from .storage import initialize, transaction


ZERO = Decimal("0")

ROLE_PERMISSIONS = {
    "clerk": {"household.write", "application.write", "ledger.read"},
    "manager": {
        "plot.write",
        "grant.write",
        "rule.write",
        "application.write",
        "allocation.confirm",
        "delivery.write",
        "reservation.write",
        "settlement.run",
        "exception.review",
        "ledger.read",
        "explain.read",
        "audit.read",
    },
    "reviewer": {"exception.review", "ledger.read"},
    "auditor": {"ledger.read", "explain.read", "audit.read"},
    "household": {"own.read"},
}

ENTRY_VISIBILITY = {
    "grant": "household",
    "freeze": "internal",
    "unfreeze": "internal",
    "consume": "household",
    "return": "household",
    "expire": "household",
    "reserve": "household",
}


def _mu(value: object) -> Decimal:
    return Decimal(str(value))


class RightsLedgerService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    # ------------------------------------------------------------------
    # 用户、权限与审计
    # ------------------------------------------------------------------
    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM ledger_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM ledger_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO ledger_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _entry(
        self,
        *,
        household_id: str,
        kind: str,
        amount_mu: Decimal,
        actor_id: str,
        reason: str,
        grant_id: str | None = None,
        application_id: str | None = None,
        reservation_id: str | None = None,
        exception_id: str | None = None,
        delivery_id: str | None = None,
        rule_id: str | None = None,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO ledger_entries(household_id,grant_id,kind,amount_mu,application_id,reservation_id,"
            "exception_id,delivery_id,rule_id,reason,visibility,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                household_id,
                grant_id,
                kind,
                decimal_text(quantize_mu(amount_mu)),
                application_id,
                reservation_id,
                exception_id,
                delivery_id,
                rule_id,
                reason,
                ENTRY_VISIBILITY[kind],
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def create_user(
        self,
        user_id: str,
        display_name: str,
        role: str,
        household_id: str | None = None,
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "household" and not (household_id or "").strip():
            raise ValidationFailed("家庭用户必须绑定 household_id")
        if role != "household" and household_id is not None:
            raise ValidationFailed("只有家庭用户可以绑定 household_id")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO ledger_users(user_id,display_name,role,household_id,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, household_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 基础档案：家庭、地块、分配规则
    # ------------------------------------------------------------------
    def register_household(self, actor_id: str, household_id: str, name: str, village: str) -> dict[str, Any]:
        self._require(actor_id, "household.write")
        household_id = identifier(household_id, "household_id")
        if not name.strip() or not village.strip():
            raise ValidationFailed("家庭名称和所属村不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO households(household_id,name,village,created_by,created_at) VALUES(?,?,?,?,?)",
                    (household_id, name.strip(), village.strip(), actor_id, self._now()),
                )
                self._audit("household", household_id, "household.registered", actor_id, {"name": name.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("家庭户已经存在") from exc
        return {"household_id": household_id, "state": "active"}

    def register_plot(self, actor_id: str, plot_id: str, village: str, category: str, area_mu: object) -> dict[str, Any]:
        self._require(actor_id, "plot.write")
        plot_id = identifier(plot_id, "plot_id")
        category = land_category(category)
        area = quantize_mu(_mu(area_mu))
        if area <= ZERO:
            raise ValidationFailed("area_mu 必须为正数")
        if not village.strip():
            raise ValidationFailed("地块所属村不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO plots(plot_id,village,category,area_mu,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (plot_id, village.strip(), category, decimal_text(area), actor_id, self._now()),
                )
                self._audit("plot", plot_id, "plot.registered", actor_id, {"category": category})
        except sqlite3.IntegrityError as exc:
            raise Conflict("地块编号已经存在") from exc
        return {"plot_id": plot_id, "state": "available"}

    def create_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        rule = RuleInput.from_dict(raw)
        current_year = self.clock.now().year
        if rule.effective_year < current_year:
            raise Conflict("规则生效年度不能早于当前年度")
        settled = self.connection.execute(
            "SELECT MAX(year) AS max_year FROM settled_years"
        ).fetchone()["max_year"]
        if settled is not None and rule.effective_year <= settled:
            raise Conflict("规则更新不能回写已经结算的年度")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO allocation_rules(rule_id,effective_year,selection_policy,source_priority_json,"
                    "review_deadline_days,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        rule.rule_id,
                        rule.effective_year,
                        rule.selection_policy,
                        canonical_json(list(rule.source_priority)),
                        rule.review_deadline_days,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "rule",
                    rule.rule_id,
                    "rule.created",
                    actor_id,
                    {"effective_year": rule.effective_year, "selection_policy": rule.selection_policy},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("规则编号已经存在") from exc
        return {"rule_id": rule.rule_id, "effective_year": rule.effective_year}

    def _active_rule(self, year: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM allocation_rules WHERE effective_year<=? ORDER BY effective_year DESC,rule_id DESC LIMIT 1",
            (year,),
        ).fetchone()
        if row is None:
            raise InvalidState("当前年度没有生效的分配规则")
        return row

    # ------------------------------------------------------------------
    # 权益授予与到期核销
    # ------------------------------------------------------------------
    def grant_entitlement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "grant.write")
        grant = GrantInput.from_dict(raw)
        household = self._household(grant.household_id)
        if household["state"] != "active":
            raise InvalidState("家庭户不是有效状态")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO entitlement_grants(grant_id,household_id,source,category,quantity_mu,"
                    "effective_from,expires_at,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant.grant_id,
                        grant.household_id,
                        grant.source,
                        grant.category,
                        decimal_text(quantize_mu(grant.quantity_mu)),
                        grant.effective_from,
                        grant.expires_at,
                        grant.reason,
                        actor_id,
                        self._now(),
                    ),
                )
                entry_id = self._entry(
                    household_id=grant.household_id,
                    grant_id=grant.grant_id,
                    kind="grant",
                    amount_mu=grant.quantity_mu,
                    actor_id=actor_id,
                    reason=grant.reason,
                )
                self._audit(
                    "grant",
                    grant.grant_id,
                    "grant.created",
                    actor_id,
                    {"household_id": grant.household_id, "source": grant.source, "entry_id": entry_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("权益批次编号已经存在") from exc
        return {"grant_id": grant.grant_id, "state": "active"}

    def _household(self, household_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM households WHERE household_id=?", (household_id,)
        ).fetchone()
        if row is None:
            raise NotFound("家庭户不存在")
        return row

    @staticmethod
    def _available(row: sqlite3.Row) -> Decimal:
        return _mu(row["quantity_mu"]) - _mu(row["consumed_mu"]) - _mu(row["frozen_mu"]) - _mu(row["expired_mu"])

    def _date_valid(self, row: sqlite3.Row) -> bool:
        today = self._today()
        if row["effective_from"] > today:
            return False
        return row["expires_at"] is None or row["expires_at"] >= today

    def _grant_state(self, *, quantity: Decimal, consumed: Decimal, frozen: Decimal, expired: Decimal, expires_at: str | None) -> str:
        available = quantity - consumed - frozen - expired
        if frozen == ZERO and available <= ZERO:
            return "exhausted" if consumed > ZERO else "expired"
        if expires_at is not None and expires_at < self._today() and frozen == ZERO:
            return "expired"
        return "active"

    def _update_grant(
        self,
        grant_id: str,
        *,
        consumed: Decimal,
        frozen: Decimal,
        expired: Decimal,
    ) -> None:
        row = self.connection.execute(
            "SELECT * FROM entitlement_grants WHERE grant_id=?", (grant_id,)
        ).fetchone()
        state = self._grant_state(
            quantity=_mu(row["quantity_mu"]),
            consumed=consumed,
            frozen=frozen,
            expired=expired,
            expires_at=row["expires_at"],
        )
        cursor = self.connection.execute(
            "UPDATE entitlement_grants SET consumed_mu=?,frozen_mu=?,expired_mu=?,state=?,revision=revision+1 "
            "WHERE grant_id=? AND revision=?",
            (
                decimal_text(quantize_mu(consumed)),
                decimal_text(quantize_mu(frozen)),
                decimal_text(quantize_mu(expired)),
                state,
                grant_id,
                row["revision"],
            ),
        )
        if cursor.rowcount != 1:
            raise Conflict("权益批次被并发修改")

    def _sweep_household_expiries(self, actor_id: str, household_id: str) -> list[str]:
        """把已过期且未冻结的可用余额核销为过期额度，返回核销的批次编号。"""
        today = self._today()
        rows = self.connection.execute(
            "SELECT * FROM entitlement_grants WHERE household_id=? AND expires_at IS NOT NULL AND expires_at<? "
            "ORDER BY grant_id",
            (household_id, today),
        ).fetchall()
        swept: list[str] = []
        for row in rows:
            available = self._available(row)
            if available <= ZERO:
                continue
            expired = _mu(row["expired_mu"]) + available
            self._update_grant(
                row["grant_id"],
                consumed=_mu(row["consumed_mu"]),
                frozen=_mu(row["frozen_mu"]),
                expired=expired,
            )
            self._entry(
                household_id=household_id,
                grant_id=row["grant_id"],
                kind="expire",
                amount_mu=available,
                actor_id=actor_id,
                reason="权益超过有效期未使用，核销可用余额",
            )
            self._audit(
                "grant",
                row["grant_id"],
                "grant.expired",
                actor_id,
                {"household_id": household_id, "expired_mu": decimal_text(quantize_mu(available))},
            )
            swept.append(row["grant_id"])
        return swept

    # ------------------------------------------------------------------
    # 申请、冻结与超额复核
    # ------------------------------------------------------------------
    def _candidates(self, household_id: str, category: str) -> list[QuotaCandidate]:
        rows = self.connection.execute(
            "SELECT * FROM entitlement_grants WHERE household_id=? AND category=? ORDER BY grant_id",
            (household_id, category),
        ).fetchall()
        candidates: list[QuotaCandidate] = []
        for row in rows:
            if not self._date_valid(row):
                continue
            available = self._available(row)
            if available <= ZERO:
                continue
            candidates.append(
                QuotaCandidate(
                    grant_id=row["grant_id"],
                    source=row["source"],
                    available_mu=quantize_mu(available),
                    effective_from=row["effective_from"],
                    expires_at=row["expires_at"],
                )
            )
        return candidates

    def _freeze(
        self,
        *,
        actor_id: str,
        household_id: str,
        grant_id: str,
        application_id: str,
        amount_mu: Decimal,
    ) -> None:
        row = self.connection.execute(
            "SELECT * FROM entitlement_grants WHERE grant_id=?", (grant_id,)
        ).fetchone()
        available = self._available(row)
        if amount_mu > available:
            raise Conflict("权益批次可用额度不足，无法冻结")
        self._update_grant(
            grant_id,
            consumed=_mu(row["consumed_mu"]),
            frozen=_mu(row["frozen_mu"]) + amount_mu,
            expired=_mu(row["expired_mu"]),
        )
        self.connection.execute(
            "INSERT INTO freezes(household_id,grant_id,application_id,amount_mu,created_by,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (household_id, grant_id, application_id, decimal_text(quantize_mu(amount_mu)), actor_id, self._now()),
        )
        self._entry(
            household_id=household_id,
            grant_id=grant_id,
            kind="freeze",
            amount_mu=amount_mu,
            application_id=application_id,
            actor_id=actor_id,
            reason="申请占用额度冻结",
        )

    def _release_application_freezes(self, actor_id: str, application_id: str, reason: str) -> None:
        rows = self.connection.execute(
            "SELECT * FROM freezes WHERE application_id=? AND state='active' ORDER BY freeze_id",
            (application_id,),
        ).fetchall()
        for freeze in rows:
            remainder = _mu(freeze["amount_mu"]) - _mu(freeze["consumed_mu"]) - _mu(freeze["released_mu"])
            if remainder <= ZERO:
                continue
            grant = self.connection.execute(
                "SELECT * FROM entitlement_grants WHERE grant_id=?", (freeze["grant_id"],)
            ).fetchone()
            frozen = _mu(grant["frozen_mu"]) - remainder
            expired = _mu(grant["expired_mu"])
            if grant["expires_at"] is not None and grant["expires_at"] < self._today():
                expired += remainder
                kind = "expire"
                entry_reason = f"{reason}，权益已过有效期，核销返还额度"
            else:
                kind = "unfreeze"
                entry_reason = reason
            self._update_grant(
                grant["grant_id"],
                consumed=_mu(grant["consumed_mu"]),
                frozen=frozen,
                expired=expired,
            )
            self.connection.execute(
                "UPDATE freezes SET released_mu=?,state='closed',closed_at=? WHERE freeze_id=?",
                (
                    decimal_text(quantize_mu(_mu(freeze["released_mu"]) + remainder)),
                    self._now(),
                    freeze["freeze_id"],
                ),
            )
            self._entry(
                household_id=freeze["household_id"],
                grant_id=freeze["grant_id"],
                kind=kind,
                amount_mu=remainder,
                application_id=application_id,
                actor_id=actor_id,
                reason=entry_reason,
            )

    def submit_application(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "application.write")
        application = ApplicationInput.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM ledger_idempotency "
            "WHERE scope='application' AND idempotency_key=?",
            (application.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同申请内容")
            return json.loads(stored["response_json"])
        household = self._household(application.household_id)
        if household["state"] != "active":
            raise InvalidState("家庭户不是有效状态")
        plot = self.connection.execute(
            "SELECT * FROM plots WHERE plot_id=?", (application.plot_id,)
        ).fetchone()
        if plot is None:
            raise NotFound("地块不存在")
        if plot["category"] != application.category:
            raise ValidationFailed("申请地类与地块地类不一致")
        rule = self._active_rule(self.clock.now().year)
        source_priority = json.loads(rule["source_priority_json"])
        with transaction(self.connection, immediate=True):
            if self.connection.execute(
                "SELECT 1 FROM applications WHERE application_id=?", (application.application_id,)
            ).fetchone() is not None:
                raise Conflict("申请编号已经存在")
            self._sweep_household_expiries(actor_id, application.household_id)
            selection = select_quota(
                self._candidates(application.household_id, application.category),
                quantize_mu(application.requested_mu),
                rule["selection_policy"],
                source_priority,
            )
            state = "submitted" if selection.shortfall_mu == ZERO else "pending_review"
            self.connection.execute(
                "INSERT INTO applications(application_id,household_id,plot_id,category,requested_mu,"
                "project_start,project_end,state,rule_id,idempotency_key,submitted_by,submitted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    application.application_id,
                    application.household_id,
                    application.plot_id,
                    application.category,
                    decimal_text(quantize_mu(application.requested_mu)),
                    application.project_start,
                    application.project_end,
                    state,
                    rule["rule_id"],
                    application.idempotency_key,
                    actor_id,
                    self._now(),
                ),
            )
            for take in selection.takes:
                self._freeze(
                    actor_id=actor_id,
                    household_id=application.household_id,
                    grant_id=take.grant_id,
                    application_id=application.application_id,
                    amount_mu=take.amount_mu,
                )
            response: dict[str, Any] = {
                "application_id": application.application_id,
                "state": state,
                "frozen_mu": decimal_text(quantize_mu(application.requested_mu - selection.shortfall_mu)),
                "shortfall_mu": decimal_text(selection.shortfall_mu),
                "rule_id": rule["rule_id"],
            }
            if selection.shortfall_mu > ZERO:
                exception_id = f"exc-{application.application_id}"
                deadline = self.clock.now() + timedelta(days=int(rule["review_deadline_days"]))
                available_mu = quantize_mu(application.requested_mu - selection.shortfall_mu)
                self.connection.execute(
                    "INSERT INTO exceptions(exception_id,application_id,household_id,category,requested_mu,"
                    "available_mu,over_mu,rule_id,submitted_by,submitted_at,deadline_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        exception_id,
                        application.application_id,
                        application.household_id,
                        application.category,
                        decimal_text(quantize_mu(application.requested_mu)),
                        decimal_text(available_mu),
                        decimal_text(selection.shortfall_mu),
                        rule["rule_id"],
                        actor_id,
                        self._now(),
                        utc_text(deadline),
                    ),
                )
                self._audit(
                    "exception",
                    exception_id,
                    "exception.queued",
                    actor_id,
                    {
                        "application_id": application.application_id,
                        "over_mu": decimal_text(selection.shortfall_mu),
                        "deadline_at": utc_text(deadline),
                    },
                )
                response["exception_id"] = exception_id
                response["review_deadline_at"] = utc_text(deadline)
            self.connection.execute(
                "INSERT INTO ledger_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('application',?,?,?,?)",
                (application.idempotency_key, request_digest, canonical_json(response), self._now()),
            )
            self._audit(
                "application",
                application.application_id,
                "application.submitted",
                actor_id,
                {"household_id": application.household_id, "state": state},
            )
        return response

    def _load_exception(self, exception_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM exceptions WHERE exception_id=?", (exception_id,)
        ).fetchone()
        if row is None:
            raise NotFound("复核例外不存在")
        return row

    def _expire_exception(self, actor_id: str, row: sqlite3.Row) -> None:
        self._release_application_freezes(actor_id, row["application_id"], "复核超期，释放申请冻结额度")
        self.connection.execute(
            "UPDATE exceptions SET state='expired',decided_at=? WHERE exception_id=?",
            (self._now(), row["exception_id"]),
        )
        self.connection.execute(
            "UPDATE applications SET state='expired' WHERE application_id=? AND state='pending_review'",
            (row["application_id"],),
        )
        self._audit(
            "exception",
            row["exception_id"],
            "exception.expired",
            actor_id,
            {"application_id": row["application_id"], "deadline_at": row["deadline_at"]},
        )

    def review_exception(self, actor_id: str, exception_id: str, decision: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exception.review")
        if decision not in {"approve", "reject"}:
            raise ValidationFailed("decision 必须是 approve 或 reject")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("复核意见不能为空")
        with transaction(self.connection, immediate=True):
            row = self._load_exception(exception_id)
            if row["state"] == "pending" and self.clock.now() > parse_utc(row["deadline_at"], "deadline_at"):
                self._expire_exception(actor_id, row)
        row = self._load_exception(exception_id)
        if row["state"] != "pending":
            raise InvalidState("复核例外已经处理或已过复核期限")
        if decision == "approve" and row["submitted_by"] == actor_id:
            raise Forbidden("复核人不得批准自己提交的例外")
        with transaction(self.connection, immediate=True):
            if decision == "approve":
                grant_id = f"excgrant-{exception_id}"
                today = self._today()
                expires_at = date(self.clock.now().year, 12, 31).isoformat()
                over_mu = _mu(row["over_mu"])
                self.connection.execute(
                    "INSERT INTO entitlement_grants(grant_id,household_id,source,category,quantity_mu,"
                    "effective_from,expires_at,exception_id,reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant_id,
                        row["household_id"],
                        "exception-approval",
                        row["category"],
                        decimal_text(quantize_mu(over_mu)),
                        today,
                        expires_at,
                        exception_id,
                        f"超额复核批准：{reason.strip()}",
                        actor_id,
                        self._now(),
                    ),
                )
                self._entry(
                    household_id=row["household_id"],
                    grant_id=grant_id,
                    kind="grant",
                    amount_mu=over_mu,
                    application_id=row["application_id"],
                    exception_id=exception_id,
                    rule_id=row["rule_id"],
                    actor_id=actor_id,
                    reason="超额复核批准授予的专项额度",
                )
                self._freeze(
                    actor_id=actor_id,
                    household_id=row["household_id"],
                    grant_id=grant_id,
                    application_id=row["application_id"],
                    amount_mu=over_mu,
                )
                self.connection.execute(
                    "UPDATE exceptions SET state='approved',decided_by=?,decided_at=?,decision_reason=?,grant_id=? "
                    "WHERE exception_id=?",
                    (actor_id, self._now(), reason.strip(), grant_id, exception_id),
                )
                self.connection.execute(
                    "UPDATE applications SET state='submitted' WHERE application_id=? AND state='pending_review'",
                    (row["application_id"],),
                )
            else:
                self._release_application_freezes(actor_id, row["application_id"], "复核驳回，释放申请冻结额度")
                self.connection.execute(
                    "UPDATE exceptions SET state='rejected',decided_by=?,decided_at=?,decision_reason=? "
                    "WHERE exception_id=?",
                    (actor_id, self._now(), reason.strip(), exception_id),
                )
                self.connection.execute(
                    "UPDATE applications SET state='rejected' WHERE application_id=? AND state='pending_review'",
                    (row["application_id"],),
                )
            self._audit(
                "exception",
                exception_id,
                "exception.approved" if decision == "approve" else "exception.rejected",
                actor_id,
                {"application_id": row["application_id"], "decision": decision, "reason": reason.strip()},
            )
        return {"exception_id": exception_id, "state": "approved" if decision == "approve" else "rejected"}

    def list_exceptions(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "ledger.read")
        with transaction(self.connection, immediate=True):
            rows = self.connection.execute(
                "SELECT * FROM exceptions WHERE state='pending' ORDER BY exception_id"
            ).fetchall()
            for row in rows:
                if self.clock.now() > parse_utc(row["deadline_at"], "deadline_at"):
                    self._expire_exception(actor_id, row)
        rows = self.connection.execute(
            "SELECT * FROM exceptions ORDER BY exception_id"
        ).fetchall()
        return {"exceptions": [dict(row) for row in rows]}

    def cancel_application(self, actor_id: str, application_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "application.write")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.connection, immediate=True):
            application = self.connection.execute(
                "SELECT * FROM applications WHERE application_id=?", (application_id,)
            ).fetchone()
            if application is None:
                raise NotFound("申请不存在")
            if application["state"] not in {"submitted", "pending_review"}:
                raise InvalidState("申请不在可撤销状态")
            self._release_application_freezes(actor_id, application_id, "申请撤销，释放冻结额度")
            self.connection.execute(
                "UPDATE applications SET state='cancelled' WHERE application_id=?", (application_id,)
            )
            self.connection.execute(
                "UPDATE exceptions SET state='rejected',decided_by=?,decided_at=?,decision_reason=? "
                "WHERE application_id=? AND state='pending'",
                (actor_id, self._now(), f"申请撤销，复核关闭：{reason.strip()}", application_id),
            )
            self._audit("application", application_id, "application.cancelled", actor_id, {"reason": reason.strip()})
        return {"application_id": application_id, "state": "cancelled"}

    # ------------------------------------------------------------------
    # 分配确认、交付、退出与年度结算
    # ------------------------------------------------------------------
    def confirm_allocation(self, actor_id: str, application_id: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.confirm")
        with transaction(self.connection, immediate=True):
            application = self.connection.execute(
                "SELECT * FROM applications WHERE application_id=?", (application_id,)
            ).fetchone()
            if application is None:
                raise NotFound("申请不存在")
            if application["state"] != "submitted":
                raise InvalidState("申请不在可确认状态")
            self._sweep_household_expiries(actor_id, application["household_id"])
            freeze_rows = self.connection.execute(
                "SELECT amount_mu FROM freezes WHERE application_id=? AND state='active'",
                (application_id,),
            ).fetchall()
            frozen_total = sum((_mu(row["amount_mu"]) for row in freeze_rows), ZERO)
            requested = _mu(application["requested_mu"])
            if quantize_mu(frozen_total) != quantize_mu(requested):
                raise InvalidState("申请冻结额度与请求数量不一致，无法确认")
            cursor = self.connection.execute(
                "UPDATE plots SET state='reserved' WHERE plot_id=? AND state='available'",
                (application["plot_id"],),
            )
            if cursor.rowcount != 1:
                raise Conflict("地块已被其他申请预留或交付")
            today = date.fromisoformat(self._today())
            project_start = date.fromisoformat(application["project_start"])
            project_end = date.fromisoformat(application["project_end"])
            split_start = max(project_start, today)
            if project_end < split_start:
                raise InvalidState("项目结束日期早于当前日期，无法确认分配")
            slices = split_yearly_occupancy(split_start, project_end, requested)
            reservation_id = f"rsv-{application_id}"
            self.connection.execute(
                "INSERT INTO reservations(reservation_id,application_id,household_id,plot_id,category,"
                "total_mu,rule_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    reservation_id,
                    application_id,
                    application["household_id"],
                    application["plot_id"],
                    application["category"],
                    decimal_text(quantize_mu(requested)),
                    application["rule_id"],
                    actor_id,
                    self._now(),
                ),
            )
            for item in slices:
                self.connection.execute(
                    "INSERT INTO reservation_years(reservation_id,year,planned_mu) VALUES(?,?,?)",
                    (reservation_id, item.year, decimal_text(item.planned_mu)),
                )
            self.connection.execute(
                "UPDATE freezes SET reservation_id=? WHERE application_id=? AND state='active'",
                (reservation_id, application_id),
            )
            self.connection.execute(
                "UPDATE applications SET state='confirmed',confirmed_at=? WHERE application_id=?",
                (self._now(), application_id),
            )
            self._entry(
                household_id=application["household_id"],
                kind="reserve",
                amount_mu=requested,
                application_id=application_id,
                reservation_id=reservation_id,
                rule_id=application["rule_id"],
                actor_id=actor_id,
                reason="分配确认，额度与地块预留原子落账",
            )
            self._audit(
                "reservation",
                reservation_id,
                "reservation.confirmed",
                actor_id,
                {
                    "application_id": application_id,
                    "plot_id": application["plot_id"],
                    "years": [{"year": item.year, "planned_mu": decimal_text(item.planned_mu)} for item in slices],
                },
            )
        return {
            "reservation_id": reservation_id,
            "application_id": application_id,
            "state": "reserved",
            "years": [{"year": item.year, "planned_mu": decimal_text(item.planned_mu)} for item in slices],
        }

    def _reservation(self, reservation_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("地块预留不存在")
        return row

    def record_delivery(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "delivery.write")
        delivery = DeliveryInput.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM ledger_idempotency "
            "WHERE scope='delivery' AND idempotency_key=?",
            (delivery.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同交付内容")
            return json.loads(stored["response_json"])
        with transaction(self.connection, immediate=True):
            if self.connection.execute(
                "SELECT 1 FROM deliveries WHERE delivery_id=?", (delivery.delivery_id,)
            ).fetchone() is not None:
                raise Conflict("交付编号已经存在")
            reservation = self._reservation(delivery.reservation_id)
            if reservation["state"] not in {"reserved", "in_delivery"}:
                raise InvalidState("预留不在可交付状态")
            year_row = self.connection.execute(
                "SELECT * FROM reservation_years WHERE reservation_id=? AND year=?",
                (delivery.reservation_id, delivery.year),
            ).fetchone()
            if year_row is None:
                raise ValidationFailed("交付年度不属于该预留的预计占用切分")
            if year_row["settled"]:
                raise InvalidState("交付年度已经结算，不能回写")
            if delivery.year > self.clock.now().year:
                raise InvalidState("不能提前交付未来年度")
            amount = quantize_mu(delivery.amount_mu)
            total = _mu(reservation["total_mu"])
            delivered = _mu(reservation["delivered_mu"])
            if delivered + amount > total:
                raise Conflict("交付数量超过预留总量")
            remaining = amount
            freezes = self.connection.execute(
                "SELECT f.*,g.expires_at AS grant_expires_at,g.effective_from AS grant_effective_from "
                "FROM freezes f JOIN entitlement_grants g ON g.grant_id=f.grant_id "
                "WHERE f.reservation_id=? AND f.state='active' "
                "ORDER BY COALESCE(g.expires_at,'9999-12-31'),f.freeze_id",
                (delivery.reservation_id,),
            ).fetchall()
            for freeze in freezes:
                if remaining == ZERO:
                    break
                freeze_remaining = _mu(freeze["amount_mu"]) - _mu(freeze["consumed_mu"]) - _mu(freeze["released_mu"])
                if freeze_remaining <= ZERO:
                    continue
                take = min(remaining, freeze_remaining)
                grant = self.connection.execute(
                    "SELECT * FROM entitlement_grants WHERE grant_id=?", (freeze["grant_id"],)
                ).fetchone()
                self._update_grant(
                    freeze["grant_id"],
                    consumed=_mu(grant["consumed_mu"]) + take,
                    frozen=_mu(grant["frozen_mu"]) - take,
                    expired=_mu(grant["expired_mu"]),
                )
                new_consumed = _mu(freeze["consumed_mu"]) + take
                closed = new_consumed + _mu(freeze["released_mu"]) == _mu(freeze["amount_mu"])
                self.connection.execute(
                    "UPDATE freezes SET consumed_mu=?,state=?,closed_at=? WHERE freeze_id=?",
                    (
                        decimal_text(quantize_mu(new_consumed)),
                        "closed" if closed else "active",
                        self._now() if closed else None,
                        freeze["freeze_id"],
                    ),
                )
                self._entry(
                    household_id=reservation["household_id"],
                    grant_id=freeze["grant_id"],
                    kind="consume",
                    amount_mu=take,
                    application_id=reservation["application_id"],
                    reservation_id=delivery.reservation_id,
                    delivery_id=delivery.delivery_id,
                    rule_id=reservation["rule_id"],
                    actor_id=actor_id,
                    reason=f"{delivery.year} 年度实际交付消耗",
                )
                remaining = quantize_mu(remaining - take)
            if remaining != ZERO:
                raise InvalidState("预留冻结额度不足，无法完成交付")
            new_delivered = delivered + amount
            self.connection.execute(
                "UPDATE reservation_years SET delivered_mu=? WHERE reservation_id=? AND year=?",
                (
                    decimal_text(quantize_mu(_mu(year_row["delivered_mu"]) + amount)),
                    delivery.reservation_id,
                    delivery.year,
                ),
            )
            completed = quantize_mu(new_delivered) == quantize_mu(total)
            self.connection.execute(
                "UPDATE reservations SET delivered_mu=?,state=?,closed_at=? WHERE reservation_id=?",
                (
                    decimal_text(quantize_mu(new_delivered)),
                    "completed" if completed else "in_delivery",
                    self._now() if completed else None,
                    delivery.reservation_id,
                ),
            )
            if completed:
                self.connection.execute(
                    "UPDATE plots SET state='allocated' WHERE plot_id=?",
                    (reservation["plot_id"],),
                )
            self.connection.execute(
                "INSERT INTO deliveries(delivery_id,reservation_id,year,amount_mu,idempotency_key,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    delivery.delivery_id,
                    delivery.reservation_id,
                    delivery.year,
                    decimal_text(amount),
                    delivery.idempotency_key,
                    actor_id,
                    self._now(),
                ),
            )
            response = {
                "delivery_id": delivery.delivery_id,
                "reservation_id": delivery.reservation_id,
                "year": delivery.year,
                "amount_mu": decimal_text(amount),
                "delivered_mu": decimal_text(quantize_mu(new_delivered)),
                "state": "completed" if completed else "in_delivery",
            }
            self.connection.execute(
                "INSERT INTO ledger_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('delivery',?,?,?,?)",
                (delivery.idempotency_key, request_digest, canonical_json(response), self._now()),
            )
            self._audit(
                "reservation",
                delivery.reservation_id,
                "delivery.recorded",
                actor_id,
                {"delivery_id": delivery.delivery_id, "year": delivery.year, "amount_mu": decimal_text(amount)},
            )
        return response

    def exit_reservation(self, actor_id: str, reservation_id: str, reason: str, *, failed: bool = False) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("退出原因不能为空")
        with transaction(self.connection, immediate=True):
            reservation = self._reservation(reservation_id)
            if reservation["state"] not in {"reserved", "in_delivery"}:
                raise InvalidState("预留不在可退出状态")
            returned = ZERO
            expired_return = ZERO
            freezes = self.connection.execute(
                "SELECT * FROM freezes WHERE reservation_id=? AND state='active' ORDER BY freeze_id",
                (reservation_id,),
            ).fetchall()
            for freeze in freezes:
                remainder = _mu(freeze["amount_mu"]) - _mu(freeze["consumed_mu"]) - _mu(freeze["released_mu"])
                if remainder <= ZERO:
                    continue
                grant = self.connection.execute(
                    "SELECT * FROM entitlement_grants WHERE grant_id=?", (freeze["grant_id"],)
                ).fetchone()
                frozen = _mu(grant["frozen_mu"]) - remainder
                expired = _mu(grant["expired_mu"])
                if grant["expires_at"] is not None and grant["expires_at"] < self._today():
                    expired += remainder
                    expired_return += remainder
                    kind = "expire"
                    entry_reason = "退出时权益已过有效期，未交付部分核销"
                else:
                    returned += remainder
                    kind = "return"
                    entry_reason = "退出或失败，返还未实际交付的额度"
                self._update_grant(
                    freeze["grant_id"],
                    consumed=_mu(grant["consumed_mu"]),
                    frozen=frozen,
                    expired=expired,
                )
                self.connection.execute(
                    "UPDATE freezes SET released_mu=?,state='closed',closed_at=? WHERE freeze_id=?",
                    (
                        decimal_text(quantize_mu(_mu(freeze["released_mu"]) + remainder)),
                        self._now(),
                        freeze["freeze_id"],
                    ),
                )
                self._entry(
                    household_id=reservation["household_id"],
                    grant_id=freeze["grant_id"],
                    kind=kind,
                    amount_mu=remainder,
                    application_id=reservation["application_id"],
                    reservation_id=reservation_id,
                    rule_id=reservation["rule_id"],
                    actor_id=actor_id,
                    reason=entry_reason,
                )
            state = "failed" if failed else "exited"
            self.connection.execute(
                "UPDATE reservations SET state=?,closed_at=? WHERE reservation_id=?",
                (state, self._now(), reservation_id),
            )
            self.connection.execute(
                "UPDATE plots SET state='available' WHERE plot_id=? AND state='reserved'",
                (reservation["plot_id"],),
            )
            self._audit(
                "reservation",
                reservation_id,
                f"reservation.{state}",
                actor_id,
                {
                    "reason": reason.strip(),
                    "returned_mu": decimal_text(quantize_mu(returned)),
                    "expired_mu": decimal_text(quantize_mu(expired_return)),
                    "delivered_kept_mu": reservation["delivered_mu"],
                },
            )
        return {
            "reservation_id": reservation_id,
            "state": state,
            "returned_mu": decimal_text(quantize_mu(returned)),
            "expired_mu": decimal_text(quantize_mu(expired_return)),
            "delivered_kept_mu": reservation["delivered_mu"],
        }

    def settle_year(self, actor_id: str, year: int) -> dict[str, Any]:
        self._require(actor_id, "settlement.run")
        if isinstance(year, bool) or not isinstance(year, int):
            raise ValidationFailed("year 必须是整数年份")
        if year >= self.clock.now().year:
            raise InvalidState("只能结算已经结束的年度")
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT year FROM settled_years WHERE year=?", (year,)
            ).fetchone()
            if existing is not None:
                raise Conflict("该年度已经结算")
            rows = self.connection.execute(
                "SELECT * FROM reservation_years WHERE year=? AND settled=0 ORDER BY reservation_id",
                (year,),
            ).fetchall()
            planned = delivered = ZERO
            for row in rows:
                planned += _mu(row["planned_mu"])
                delivered += _mu(row["delivered_mu"])
                self.connection.execute(
                    "UPDATE reservation_years SET settled=1,settled_at=? WHERE reservation_id=? AND year=?",
                    (self._now(), row["reservation_id"], year),
                )
            self.connection.execute(
                "INSERT INTO settled_years(year,reservation_count,planned_mu,delivered_mu,settled_by,settled_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    year,
                    len(rows),
                    decimal_text(quantize_mu(planned)),
                    decimal_text(quantize_mu(delivered)),
                    actor_id,
                    self._now(),
                ),
            )
            self._audit(
                "settlement",
                str(year),
                "year.settled",
                actor_id,
                {
                    "reservation_count": len(rows),
                    "planned_mu": decimal_text(quantize_mu(planned)),
                    "delivered_mu": decimal_text(quantize_mu(delivered)),
                },
            )
        return {
            "year": year,
            "reservation_count": len(rows),
            "planned_mu": decimal_text(quantize_mu(planned)),
            "delivered_mu": decimal_text(quantize_mu(delivered)),
        }

    # ------------------------------------------------------------------
    # 查询：家庭可见性与角色解释能力
    # ------------------------------------------------------------------
    def _read_access(self, actor_id: str, household_id: str) -> None:
        user = self._user(actor_id)
        if user["role"] == "household":
            if user["household_id"] != household_id:
                raise Forbidden("家庭只能查看本户账本")
            return
        self._require(actor_id, "ledger.read")

    def household_summary(self, actor_id: str, household_id: str) -> dict[str, Any]:
        self._read_access(actor_id, household_id)
        self._household(household_id)
        with transaction(self.connection, immediate=True):
            self._sweep_household_expiries(actor_id, household_id)
        rows = self.connection.execute(
            "SELECT * FROM entitlement_grants WHERE household_id=? ORDER BY grant_id", (household_id,)
        ).fetchall()
        by_category: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_category.setdefault(row["category"], []).append(row)
        categories = {
            category: summarize_balances(grant_rows) for category, grant_rows in sorted(by_category.items())
        }
        today = self._today()
        horizon = (date.fromisoformat(today) + timedelta(days=30)).isoformat()
        expiring_soon = ZERO
        for row in rows:
            if row["expires_at"] is not None and today <= row["expires_at"] <= horizon:
                available = self._available(row)
                if available > ZERO:
                    expiring_soon += available
        pending = self.connection.execute(
            "SELECT COUNT(*) AS count FROM exceptions WHERE household_id=? AND state='pending'",
            (household_id,),
        ).fetchone()["count"]
        active_reservations = self.connection.execute(
            "SELECT COUNT(*) AS count FROM reservations WHERE household_id=? AND state IN ('reserved','in_delivery')",
            (household_id,),
        ).fetchone()["count"]
        return {
            "household_id": household_id,
            "totals": summarize_balances(rows),
            "categories": categories,
            "expiring_within_30d_mu": decimal_text(quantize_mu(expiring_soon)),
            "pending_exceptions": pending,
            "active_reservations": active_reservations,
        }

    def household_entries(self, actor_id: str, household_id: str) -> dict[str, Any]:
        self._read_access(actor_id, household_id)
        user = self._user(actor_id)
        if user["role"] == "household":
            rows = self.connection.execute(
                "SELECT * FROM ledger_entries WHERE household_id=? AND visibility='household' ORDER BY entry_id",
                (household_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM ledger_entries WHERE household_id=? ORDER BY entry_id", (household_id,)
            ).fetchall()
        return {"household_id": household_id, "entries": [dict(row) for row in rows]}

    def explain_entry(self, actor_id: str, entry_id: int) -> dict[str, Any]:
        self._require(actor_id, "explain.read")
        entry = self.connection.execute(
            "SELECT * FROM ledger_entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if entry is None:
            raise NotFound("账本流水不存在")
        result: dict[str, Any] = {"entry": dict(entry)}
        if entry["grant_id"] is not None:
            grant = self.connection.execute(
                "SELECT * FROM entitlement_grants WHERE grant_id=?", (entry["grant_id"],)
            ).fetchone()
            result["grant"] = None if grant is None else dict(grant)
        for key, table in (("application", "applications"), ("reservation", "reservations"), ("exception", "exceptions")):
            related_id = entry[f"{key}_id"]
            if related_id is not None:
                row = self.connection.execute(
                    f"SELECT * FROM {table} WHERE {key}_id=?", (related_id,)
                ).fetchone()
                result[key] = None if row is None else dict(row)
        if entry["rule_id"] is not None:
            rule = self.connection.execute(
                "SELECT * FROM allocation_rules WHERE rule_id=?", (entry["rule_id"],)
            ).fetchone()
            result["rule"] = None if rule is None else dict(rule)
        return result

    def explain_exception(self, actor_id: str, exception_id: str) -> dict[str, Any]:
        self._require(actor_id, "explain.read")
        row = self._load_exception(exception_id)
        entries = self.connection.execute(
            "SELECT * FROM ledger_entries WHERE exception_id=? ORDER BY entry_id", (exception_id,)
        ).fetchall()
        result: dict[str, Any] = {"exception": dict(row), "entries": [dict(entry) for entry in entries]}
        if row["grant_id"] is not None:
            grant = self.connection.execute(
                "SELECT * FROM entitlement_grants WHERE grant_id=?", (row["grant_id"],)
            ).fetchone()
            result["grant"] = None if grant is None else dict(grant)
        return result

    def reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        self._require(actor_id, "ledger.read")
        row = self._reservation(reservation_id)
        years = self.connection.execute(
            "SELECT * FROM reservation_years WHERE reservation_id=? ORDER BY year", (reservation_id,)
        ).fetchall()
        return {"reservation": dict(row), "years": [dict(item) for item in years]}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM ledger_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
