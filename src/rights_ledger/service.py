"""家庭土地权益额度账本的事务用例。

账本约定：
- 权益额度按家庭隔离，授予、冻结、实际消耗、返还与过期核销全部写入
  ``ledger_entries``，并记录写入时点的可用余额，供产权管理与审计角色
  解释每一次扣减、返还和超额决定；
- 跨年度项目在提交申请时按可注入时钟切分预计占用（``application_slices``），
  分配确认时在同一事务内重新选择可用额度、冻结权益并预留地块；
- 流水年度（``ledger_entries.year``）为入账年度，取写入时的时钟年份；
  年度结算后该年度关闭，规则版本与新的预计占用都不能回写已结算年度，
  只有结算操作本身可以在关闭前写入核销条目。
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
from .models import (
    AllocationConfirmation,
    ApplicationClosure,
    ApplicationRequest,
    DeliveryRecord,
    EntitlementGrant,
    ExceptionDecision,
    HouseholdRegistration,
    PlotRegistration,
    RulePublication,
)
from .quota import (
    EntitlementView,
    YearSlice,
    available_mu,
    canonical_json,
    decimal_text,
    digest,
    quantize_mu,
    select_quota,
    split_estimated_occupancy,
)
from .storage import initialize, transaction


ZERO = Decimal("0")

ROLE_PERMISSIONS = {
    "clerk": {
        "household.write",
        "plot.write",
        "entitlement.write",
        "application.write",
        "allocation.confirm",
        "delivery.write",
    },
    "manager": {
        "entitlement.write",
        "plot.write",
        "rule.write",
        "settlement.write",
        "report.read",
        "ledger.explain",
    },
    "reviewer": {"application.write", "review.decide", "report.read"},
    "auditor": {"audit.read", "ledger.explain", "report.read"},
    "household": {"household.read"},
}


class RightsLedgerService:
    """在单个 SQLite 连接上提供家庭土地权益额度账本的全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> date:
        return self.clock.now().date()

    # ------------------------------------------------------------------
    # 用户、权限与审计
    # ------------------------------------------------------------------

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
        if role == "household" and not household_id:
            raise ValidationFailed("家庭用户必须绑定家庭户")
        if role != "household" and household_id is not None:
            raise ValidationFailed("业务角色不能绑定家庭户")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role,household_id,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, household_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在或家庭户不存在") from exc
        return {"user_id": user_id.strip(), "role": role, "household_id": household_id}

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
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

    def _require_household_access(self, actor_id: str, household_id: str) -> sqlite3.Row:
        user = self._user(actor_id)
        if user["role"] == "household":
            if user["household_id"] != household_id:
                raise Forbidden("家庭只能查看本户汇总和流水")
            return user
        if "report.read" not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权查看家庭账本")
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
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
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
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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

    # ------------------------------------------------------------------
    # 基础档案
    # ------------------------------------------------------------------

    def _household(self, household_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM households WHERE household_id=?", (household_id,)
        ).fetchone()
        if row is None:
            raise NotFound("家庭户不存在")
        return row

    def register_household(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "household.write")
        household = HouseholdRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO households(household_id,name,village,member_count,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        household.household_id,
                        household.name,
                        household.village,
                        household.member_count,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("household", household.household_id, "household.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("家庭户编号已经存在") from exc
        return {"household_id": household.household_id, "name": household.name}

    def register_plot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plot.write")
        plot = PlotRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO plots(plot_id,village,land_category,area_mu,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        plot.plot_id,
                        plot.village,
                        plot.land_category,
                        decimal_text(plot.area_mu),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("plot", plot.plot_id, "plot.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("地块编号已经存在") from exc
        return {"plot_id": plot.plot_id, "land_category": plot.land_category, "area_mu": decimal_text(plot.area_mu)}

    # ------------------------------------------------------------------
    # 规则版本与年度结算
    # ------------------------------------------------------------------

    def _max_settled_year(self) -> int:
        row = self.connection.execute("SELECT MAX(year) AS year FROM settled_years").fetchone()
        return 0 if row is None or row["year"] is None else int(row["year"])

    def _check_year_open(self, year: int) -> None:
        settled = self._max_settled_year()
        if year <= settled:
            raise InvalidState(f"{year} 年度已经结算，不能回写")

    def _rule_for_year(self, year: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM rule_versions WHERE effective_year<=? ORDER BY effective_year DESC LIMIT 1",
            (year,),
        ).fetchone()
        if row is None:
            raise InvalidState(f"{year} 年度没有适用的分配规则")
        return row

    def publish_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        rule = RulePublication.from_dict(raw)
        settled = self._max_settled_year()
        if rule.effective_year <= settled:
            raise Conflict("规则更新不能回写已经结算的年度")
        latest = self.connection.execute(
            "SELECT MAX(effective_year) AS year FROM rule_versions"
        ).fetchone()["year"]
        if latest is not None and rule.effective_year <= int(latest):
            raise Conflict("规则生效年度必须晚于现有规则版本")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO rule_versions(effective_year,selection_strategy,review_window_hours,"
                "max_overshoot_percent,source_priority_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    rule.effective_year,
                    rule.selection_strategy,
                    rule.review_window_hours,
                    decimal_text(rule.max_overshoot_percent),
                    canonical_json(list(rule.source_priority)),
                    actor_id,
                    self._now(),
                ),
            )
            rule_version = int(cursor.lastrowid)
            self._audit(
                "rule",
                str(rule_version),
                "rule.published",
                actor_id,
                {"effective_year": rule.effective_year, "selection_strategy": rule.selection_strategy},
            )
        return {"rule_version": rule_version, "effective_year": rule.effective_year, "state": "published"}

    def settle_year(self, actor_id: str, year: int) -> dict[str, Any]:
        self._require(actor_id, "settlement.write")
        if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
            raise ValidationFailed("year 必须是 2000 到 2100 的整数")
        today = self._today()
        if year >= today.year:
            raise ValidationFailed("只能结算已经结束的年度")
        existing = self.connection.execute(
            "SELECT year FROM settled_years WHERE year=?", (year,)
        ).fetchone()
        if existing is not None:
            raise Conflict("该年度已经结算")
        expired_rows: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            candidates = self.connection.execute(
                "SELECT * FROM entitlements WHERE state='active' AND valid_until<=? ORDER BY entitlement_id",
                (f"{year}-12-31",),
            ).fetchall()
            for entitlement in candidates:
                remaining = available_mu(
                    Decimal(entitlement["granted_mu"]),
                    Decimal(entitlement["frozen_mu"]),
                    Decimal(entitlement["consumed_mu"]),
                    Decimal(entitlement["expired_mu"]),
                )
                if remaining > ZERO:
                    remaining = quantize_mu(remaining)
                    new_expired = Decimal(entitlement["expired_mu"]) + remaining
                    cursor = self.connection.execute(
                        "UPDATE entitlements SET expired_mu=?,revision=revision+1 "
                        "WHERE entitlement_id=? AND revision=?",
                        (decimal_text(new_expired), entitlement["entitlement_id"], entitlement["revision"]),
                    )
                    if cursor.rowcount != 1:
                        raise Conflict("权益额度并发变更，请重试")
                    self._entry(
                        household_id=entitlement["household_id"],
                        entitlement_id=entitlement["entitlement_id"],
                        kind="expire",
                        amount=remaining,
                        year=year,
                        balance_after=ZERO,
                        rule_version=int(entitlement["rule_version"]),
                        reason=f"{year} 年度结算核销过期权益",
                        actor_id=actor_id,
                    )
                    expired_rows.append(
                        {
                            "entitlement_id": entitlement["entitlement_id"],
                            "expired_mu": decimal_text(remaining),
                        }
                    )
                self._refresh_entitlement_state(entitlement["entitlement_id"])
            self.connection.execute(
                "INSERT INTO settled_years(year,settled_by,settled_at) VALUES(?,?,?)",
                (year, actor_id, self._now()),
            )
            self._audit(
                "settlement",
                str(year),
                "year.settled",
                actor_id,
                {"year": year, "expired": expired_rows},
            )
        return {"year": year, "state": "settled", "expired": expired_rows}

    # ------------------------------------------------------------------
    # 权益授予与账本流水
    # ------------------------------------------------------------------

    def _entry(
        self,
        *,
        household_id: str,
        entitlement_id: str,
        kind: str,
        amount: Decimal,
        year: int,
        balance_after: Decimal,
        rule_version: int,
        reason: str,
        actor_id: str,
        application_id: str | None = None,
        reservation_id: str | None = None,
        exception_id: str | None = None,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO ledger_entries(household_id,entitlement_id,kind,amount_mu,year,balance_after_mu,"
            "application_id,reservation_id,exception_id,rule_version,reason,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                household_id,
                entitlement_id,
                kind,
                decimal_text(amount),
                year,
                decimal_text(balance_after),
                application_id,
                reservation_id,
                exception_id,
                rule_version,
                reason,
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def _refresh_entitlement_state(self, entitlement_id: str) -> None:
        row = self.connection.execute(
            "SELECT * FROM entitlements WHERE entitlement_id=?", (entitlement_id,)
        ).fetchone()
        remaining = available_mu(
            Decimal(row["granted_mu"]),
            Decimal(row["frozen_mu"]),
            Decimal(row["consumed_mu"]),
            Decimal(row["expired_mu"]),
        )
        if Decimal(row["frozen_mu"]) == ZERO and remaining <= ZERO:
            state = "expired" if Decimal(row["expired_mu"]) > ZERO else "exhausted"
        else:
            state = "active"
        self.connection.execute(
            "UPDATE entitlements SET state=? WHERE entitlement_id=?", (state, entitlement_id)
        )

    def _entitlement_views(self, household_id: str) -> list[EntitlementView]:
        rows = self.connection.execute(
            "SELECT * FROM entitlements WHERE household_id=? AND state='active' ORDER BY entitlement_id",
            (household_id,),
        ).fetchall()
        views: list[EntitlementView] = []
        for row in rows:
            views.append(
                EntitlementView(
                    entitlement_id=row["entitlement_id"],
                    source=row["source"],
                    land_categories=frozenset(json.loads(row["land_categories_json"])),
                    available_mu=available_mu(
                        Decimal(row["granted_mu"]),
                        Decimal(row["frozen_mu"]),
                        Decimal(row["consumed_mu"]),
                        Decimal(row["expired_mu"]),
                    ),
                    valid_from=date.fromisoformat(row["valid_from"]),
                    valid_until=date.fromisoformat(row["valid_until"]),
                )
            )
        return views

    def grant_entitlement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "entitlement.write")
        grant = EntitlementGrant.from_dict(raw)
        self._household(grant.household_id)
        today = self._today()
        if date.fromisoformat(grant.valid_until) < today:
            raise ValidationFailed("权益有效期已经结束")
        rule = self._rule_for_year(today.year)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO entitlements(entitlement_id,household_id,source,land_categories_json,"
                    "granted_mu,valid_from,valid_until,rule_version,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant.entitlement_id,
                        grant.household_id,
                        grant.source,
                        canonical_json(list(grant.land_categories)),
                        decimal_text(grant.granted_mu),
                        grant.valid_from,
                        grant.valid_until,
                        int(rule["rule_version"]),
                        actor_id,
                        self._now(),
                    ),
                )
                self._entry(
                    household_id=grant.household_id,
                    entitlement_id=grant.entitlement_id,
                    kind="grant",
                    amount=grant.granted_mu,
                    year=today.year,
                    balance_after=grant.granted_mu,
                    rule_version=int(rule["rule_version"]),
                    reason=f"{grant.source} 权益授予",
                    actor_id=actor_id,
                )
                self._audit(
                    "entitlement",
                    grant.entitlement_id,
                    "entitlement.granted",
                    actor_id,
                    {"household_id": grant.household_id, "granted_mu": decimal_text(grant.granted_mu)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("权益编号已经存在") from exc
        return {
            "entitlement_id": grant.entitlement_id,
            "household_id": grant.household_id,
            "state": "active",
            "available_mu": decimal_text(grant.granted_mu),
        }

    # ------------------------------------------------------------------
    # 申请、超额复核与原子落账
    # ------------------------------------------------------------------

    def _application(self, application_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM applications WHERE application_id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFound("申请不存在")
        return row

    def _slice_objects(self, application_id: str) -> list[YearSlice]:
        rows = self.connection.execute(
            "SELECT * FROM application_slices WHERE application_id=? ORDER BY year",
            (application_id,),
        ).fetchall()
        return [
            YearSlice(
                year=int(row["year"]),
                segment_start=date.fromisoformat(row["segment_start"]),
                segment_end=date.fromisoformat(row["segment_end"]),
                days=int(row["days"]),
                planned_mu=Decimal(row["planned_mu"]),
            )
            for row in rows
        ]

    def submit_application(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "application.write")
        application = ApplicationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys "
            "WHERE scope='application' AND idempotency_key=?",
            (application.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同申请内容")
            return json.loads(stored["response_json"])
        self._household(application.household_id)
        today = self._today()
        try:
            slices = split_estimated_occupancy(
                date.fromisoformat(application.starts_on),
                date.fromisoformat(application.ends_on),
                application.requested_mu,
                today,
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        for year_slice in slices:
            self._check_year_open(year_slice.year)
        rule = self._rule_for_year(today.year)
        views = self._entitlement_views(application.household_id)
        plan = select_quota(
            views,
            slices,
            application.land_category,
            rule["selection_strategy"],
            json.loads(rule["source_priority_json"]),
            today,
        )
        overshoot = plan.shortfall_mu
        if overshoot > ZERO:
            overshoot_percent = overshoot / application.requested_mu * Decimal("100")
            if overshoot_percent > Decimal(rule["max_overshoot_percent"]):
                raise Conflict("超额幅度超出可复核上限，请调减申请数量")
        now = self._now()
        state = "accepted" if overshoot == ZERO else "under-review"
        exception_id = f"exc-{application.application_id}"
        expires_at = utc_text(self.clock.now() + timedelta(hours=int(rule["review_window_hours"])))
        response: dict[str, Any] = {
            "application_id": application.application_id,
            "state": state,
            "revision": 1,
            "slices": [year_slice.as_dict() for year_slice in slices],
            "covered_mu": decimal_text(plan.covered_mu),
            "overshoot_mu": decimal_text(overshoot),
            "exception": None
            if overshoot == ZERO
            else {"exception_id": exception_id, "state": "pending", "expires_at": expires_at},
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO applications(application_id,household_id,land_category,requested_mu,"
                    "starts_on,ends_on,state,rule_version,idempotency_key,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        application.application_id,
                        application.household_id,
                        application.land_category,
                        decimal_text(application.requested_mu),
                        application.starts_on,
                        application.ends_on,
                        state,
                        int(rule["rule_version"]),
                        application.idempotency_key,
                        actor_id,
                        now,
                    ),
                )
                for year_slice in slices:
                    self.connection.execute(
                        "INSERT INTO application_slices(application_id,year,segment_start,segment_end,"
                        "days,planned_mu) VALUES(?,?,?,?,?,?)",
                        (
                            application.application_id,
                            year_slice.year,
                            year_slice.segment_start.isoformat(),
                            year_slice.segment_end.isoformat(),
                            year_slice.days,
                            decimal_text(year_slice.planned_mu),
                        ),
                    )
                if overshoot > ZERO:
                    self.connection.execute(
                        "INSERT INTO exceptions(exception_id,application_id,household_id,overshoot_mu,"
                        "submitted_by,submitted_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            exception_id,
                            application.application_id,
                            application.household_id,
                            decimal_text(overshoot),
                            actor_id,
                            now,
                            expires_at,
                        ),
                    )
                    self._audit(
                        "exception",
                        exception_id,
                        "exception.queued",
                        actor_id,
                        {
                            "application_id": application.application_id,
                            "overshoot_mu": decimal_text(overshoot),
                            "expires_at": expires_at,
                        },
                    )
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('application',?,?,?,?)",
                    (application.idempotency_key, request_digest, canonical_json(response), now),
                )
                self._audit(
                    "application",
                    application.application_id,
                    "application.submitted",
                    actor_id,
                    {"household_id": application.household_id, "requested_mu": decimal_text(application.requested_mu)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("申请编号或幂等键冲突") from exc
        return response

    def decide_exception(self, actor_id: str, exception_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "review.decide")
        decision = ExceptionDecision.from_dict(raw)
        row = self.connection.execute(
            "SELECT * FROM exceptions WHERE exception_id=?", (exception_id,)
        ).fetchone()
        if row is None:
            raise NotFound("复核事项不存在")
        if row["state"] == "pending" and self.clock.now() > parse_utc(row["expires_at"], "expires_at"):
            with transaction(self.connection, immediate=True):
                current = self.connection.execute(
                    "SELECT state FROM exceptions WHERE exception_id=?", (exception_id,)
                ).fetchone()
                if current["state"] == "pending":
                    self.connection.execute(
                        "UPDATE exceptions SET state='expired',revision=revision+1 WHERE exception_id=?",
                        (exception_id,),
                    )
                    self.connection.execute(
                        "UPDATE applications SET state='rejected',revision=revision+1 WHERE application_id=?",
                        (row["application_id"],),
                    )
                    self._audit(
                        "exception",
                        exception_id,
                        "exception.expired",
                        actor_id,
                        {"application_id": row["application_id"]},
                    )
            raise InvalidState("复核已超期，申请按未通过处理")
        if row["state"] != "pending":
            raise InvalidState("复核事项已经处理")
        if row["submitted_by"] == actor_id:
            raise Forbidden("复核人不得批准自己提交的例外")
        application = self._application(row["application_id"])
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE exceptions SET state=?,decided_by=?,decided_at=?,decision_reason=?,"
                "revision=revision+1 WHERE exception_id=? AND state='pending' AND revision=?",
                (
                    "approved" if decision.approve else "rejected",
                    actor_id,
                    now,
                    decision.reason,
                    exception_id,
                    row["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise Conflict("复核事项并发变更，请重试")
            if decision.approve:
                rule = self._rule_for_year(self._today().year)
                grant_id = f"excgrant-{exception_id}"
                overshoot = Decimal(row["overshoot_mu"])
                self.connection.execute(
                    "INSERT INTO entitlements(entitlement_id,household_id,source,land_categories_json,"
                    "granted_mu,valid_from,valid_until,rule_version,exception_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant_id,
                        row["household_id"],
                        "exception-grant",
                        canonical_json([application["land_category"]]),
                        decimal_text(overshoot),
                        application["starts_on"],
                        application["ends_on"],
                        int(rule["rule_version"]),
                        exception_id,
                        actor_id,
                        now,
                    ),
                )
                self._entry(
                    household_id=row["household_id"],
                    entitlement_id=grant_id,
                    kind="grant",
                    amount=overshoot,
                    year=self._today().year,
                    balance_after=overshoot,
                    rule_version=int(rule["rule_version"]),
                    reason=f"复核批准超额授予：{decision.reason}",
                    actor_id=actor_id,
                    application_id=row["application_id"],
                    exception_id=exception_id,
                )
                self.connection.execute(
                    "UPDATE applications SET state='accepted',revision=revision+1 WHERE application_id=?",
                    (row["application_id"],),
                )
            else:
                self.connection.execute(
                    "UPDATE applications SET state='rejected',revision=revision+1 WHERE application_id=?",
                    (row["application_id"],),
                )
            self._audit(
                "exception",
                exception_id,
                "exception.decided",
                actor_id,
                {
                    "application_id": row["application_id"],
                    "approve": decision.approve,
                    "reason": decision.reason,
                },
            )
        return {
            "exception_id": exception_id,
            "application_id": row["application_id"],
            "state": "approved" if decision.approve else "rejected",
        }

    def confirm_allocation(self, actor_id: str, application_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """分配确认：按规则选择可用额度并与地块预留在同一事务内落账。"""
        self._require(actor_id, "allocation.confirm")
        confirmation = AllocationConfirmation.from_dict(raw)
        today = self._today()
        with transaction(self.connection, immediate=True):
            application = self._application(application_id)
            if application["state"] != "accepted":
                raise InvalidState("申请当前不可确认分配")
            if int(application["revision"]) != confirmation.expected_revision:
                raise InvalidState("申请版本已变化，请重新读取后再确认")
            if date.fromisoformat(application["ends_on"]) < today:
                raise InvalidState("项目期间已结束，无法落账")
            slices = self._slice_objects(application_id)
            for year_slice in slices:
                self._check_year_open(year_slice.year)
            plot = self.connection.execute(
                "SELECT * FROM plots WHERE plot_id=?", (confirmation.plot_id,)
            ).fetchone()
            if plot is None:
                raise NotFound("地块不存在")
            if plot["state"] != "available":
                raise InvalidState("地块当前不可预留")
            if plot["land_category"] != application["land_category"]:
                raise Conflict("地块地类与申请地类不匹配")
            requested = Decimal(application["requested_mu"])
            plot_available = Decimal(plot["area_mu"]) - Decimal(plot["reserved_mu"])
            if plot_available < requested:
                raise Conflict("地块可预留面积不足")
            rule = self._rule_for_year(today.year)
            views = self._entitlement_views(application["household_id"])
            plan = select_quota(
                views,
                slices,
                application["land_category"],
                rule["selection_strategy"],
                json.loads(rule["source_priority_json"]),
                today,
            )
            if plan.shortfall_mu > ZERO:
                raise Conflict(
                    "可用额度不足，缺口 "
                    f"{decimal_text(plan.shortfall_mu)} 亩：权益可能已过期或被其他申请冻结"
                )
            self.connection.execute(
                "INSERT INTO reservations(reservation_id,application_id,plot_id,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (confirmation.reservation_id, application_id, confirmation.plot_id, actor_id, self._now()),
            )
            balances = {view.entitlement_id: view.available_mu for view in views}
            for line in plan.lines:
                entitlement = self.connection.execute(
                    "SELECT * FROM entitlements WHERE entitlement_id=?", (line.entitlement_id,)
                ).fetchone()
                new_frozen = Decimal(entitlement["frozen_mu"]) + line.amount_mu
                cursor = self.connection.execute(
                    "UPDATE entitlements SET frozen_mu=?,revision=revision+1 "
                    "WHERE entitlement_id=? AND revision=?",
                    (decimal_text(new_frozen), line.entitlement_id, entitlement["revision"]),
                )
                if cursor.rowcount != 1:
                    raise Conflict("权益额度并发变更，请重试")
                balances[line.entitlement_id] -= line.amount_mu
                self.connection.execute(
                    "INSERT INTO reservation_lines(reservation_id,entitlement_id,year,amount_mu) "
                    "VALUES(?,?,?,?)",
                    (confirmation.reservation_id, line.entitlement_id, line.year, decimal_text(line.amount_mu)),
                )
                self._entry(
                    household_id=application["household_id"],
                    entitlement_id=line.entitlement_id,
                    kind="freeze",
                    amount=line.amount_mu,
                    year=today.year,
                    balance_after=balances[line.entitlement_id],
                    rule_version=int(rule["rule_version"]),
                    reason=f"申请 {application_id} 确认分配，冻结 {line.year} 年度额度",
                    actor_id=actor_id,
                    application_id=application_id,
                    reservation_id=confirmation.reservation_id,
                )
            self.connection.execute(
                "UPDATE plots SET reserved_mu=?,revision=revision+1 WHERE plot_id=? AND revision=?",
                (
                    decimal_text(Decimal(plot["reserved_mu"]) + requested),
                    confirmation.plot_id,
                    plot["revision"],
                ),
            )
            cursor = self.connection.execute(
                "UPDATE applications SET state='reserved',revision=revision+1 "
                "WHERE application_id=? AND revision=?",
                (application_id, confirmation.expected_revision),
            )
            if cursor.rowcount != 1:
                raise Conflict("申请并发变更，请重试")
            self._audit(
                "application",
                application_id,
                "allocation.confirmed",
                actor_id,
                {
                    "reservation_id": confirmation.reservation_id,
                    "plot_id": confirmation.plot_id,
                    "lines": [line.as_dict() for line in plan.lines],
                },
            )
        return {
            "application_id": application_id,
            "reservation_id": confirmation.reservation_id,
            "state": "reserved",
            "revision": confirmation.expected_revision + 1,
            "lines": [line.as_dict() for line in plan.lines],
        }

    def record_delivery(self, actor_id: str, application_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记实际交付：冻结额度转为实际消耗。"""
        self._require(actor_id, "delivery.write")
        record = DeliveryRecord.from_dict(raw)
        today = self._today()
        try:
            with transaction(self.connection, immediate=True):
                application = self._application(application_id)
                if application["state"] not in ("reserved", "delivering"):
                    raise InvalidState("申请不在可交付状态")
                slice_row = self.connection.execute(
                    "SELECT * FROM application_slices WHERE application_id=? AND year=?",
                    (application_id, record.year),
                ).fetchone()
                if slice_row is None:
                    raise ValidationFailed("交付年度不在预计占用切分中")
                planned_left = Decimal(slice_row["planned_mu"]) - Decimal(slice_row["delivered_mu"])
                if record.amount_mu > planned_left:
                    raise InvalidState("交付数量超过该年度预计占用")
                reservation = self.connection.execute(
                    "SELECT * FROM reservations WHERE application_id=? AND state='active'",
                    (application_id,),
                ).fetchone()
                if reservation is None:
                    raise InvalidState("申请没有生效中的地块预留")
                lines = self.connection.execute(
                    "SELECT * FROM reservation_lines WHERE reservation_id=? AND year=? ORDER BY line_id",
                    (reservation["reservation_id"], record.year),
                ).fetchall()
                remaining = record.amount_mu
                for line in lines:
                    open_amount = Decimal(line["amount_mu"]) - Decimal(line["consumed_mu"])
                    if open_amount <= ZERO:
                        continue
                    take = min(remaining, open_amount)
                    entitlement = self.connection.execute(
                        "SELECT * FROM entitlements WHERE entitlement_id=?", (line["entitlement_id"],)
                    ).fetchone()
                    new_frozen = Decimal(entitlement["frozen_mu"]) - take
                    new_consumed = Decimal(entitlement["consumed_mu"]) + take
                    cursor = self.connection.execute(
                        "UPDATE entitlements SET frozen_mu=?,consumed_mu=?,revision=revision+1 "
                        "WHERE entitlement_id=? AND revision=?",
                        (
                            decimal_text(new_frozen),
                            decimal_text(new_consumed),
                            line["entitlement_id"],
                            entitlement["revision"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise Conflict("权益额度并发变更，请重试")
                    self.connection.execute(
                        "UPDATE reservation_lines SET consumed_mu=? WHERE line_id=?",
                        (decimal_text(Decimal(line["consumed_mu"]) + take), line["line_id"]),
                    )
                    balance = available_mu(
                        Decimal(entitlement["granted_mu"]),
                        new_frozen,
                        new_consumed,
                        Decimal(entitlement["expired_mu"]),
                    )
                    self._entry(
                        household_id=application["household_id"],
                        entitlement_id=line["entitlement_id"],
                        kind="consume",
                        amount=take,
                        year=today.year,
                        balance_after=balance,
                        rule_version=int(application["rule_version"]),
                        reason=f"交付 {record.delivery_id} 实际消耗 {record.year} 年度额度",
                        actor_id=actor_id,
                        application_id=application_id,
                        reservation_id=reservation["reservation_id"],
                    )
                    self._refresh_entitlement_state(line["entitlement_id"])
                    remaining -= take
                    if remaining == ZERO:
                        break
                if remaining > ZERO:
                    raise InvalidState("预留额度不足以完成本次交付")
                self.connection.execute(
                    "UPDATE application_slices SET delivered_mu=? WHERE slice_id=?",
                    (
                        decimal_text(Decimal(slice_row["delivered_mu"]) + record.amount_mu),
                        slice_row["slice_id"],
                    ),
                )
                self.connection.execute(
                    "INSERT INTO deliveries(delivery_id,application_id,year,amount_mu,actor_id,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        record.delivery_id,
                        application_id,
                        record.year,
                        decimal_text(record.amount_mu),
                        actor_id,
                        self._now(),
                    ),
                )
                totals = self.connection.execute(
                    "SELECT planned_mu,delivered_mu FROM application_slices WHERE application_id=?",
                    (application_id,),
                ).fetchall()
                delivered_total = sum((Decimal(row["delivered_mu"]) for row in totals), ZERO)
                planned_total = sum((Decimal(row["planned_mu"]) for row in totals), ZERO)
                new_state = "completed" if delivered_total >= planned_total else "delivering"
                self.connection.execute(
                    "UPDATE applications SET state=?,revision=revision+1 WHERE application_id=?",
                    (new_state, application_id),
                )
                self._audit(
                    "application",
                    application_id,
                    "delivery.recorded",
                    actor_id,
                    {
                        "delivery_id": record.delivery_id,
                        "year": record.year,
                        "amount_mu": decimal_text(record.amount_mu),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("交付编号冲突") from exc
        return {
            "delivery_id": record.delivery_id,
            "application_id": application_id,
            "state": new_state,
            "year": record.year,
            "delivered_mu": decimal_text(quantize_mu(delivered_total)),
            "planned_mu": decimal_text(quantize_mu(planned_total)),
        }

    def close_application(self, actor_id: str, application_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """退出或失败：只返还尚未实际交付的冻结额度。"""
        self._require(actor_id, "application.write")
        closure = ApplicationClosure.from_dict(raw)
        today = self._today()
        returned_total = ZERO
        consumed_total = ZERO
        with transaction(self.connection, immediate=True):
            application = self._application(application_id)
            if application["state"] in ("reserved", "delivering"):
                reservation = self.connection.execute(
                    "SELECT * FROM reservations WHERE application_id=? AND state='active'",
                    (application_id,),
                ).fetchone()
                if reservation is None:
                    raise InvalidState("申请没有生效中的地块预留")
                lines = self.connection.execute(
                    "SELECT * FROM reservation_lines WHERE reservation_id=? ORDER BY line_id",
                    (reservation["reservation_id"],),
                ).fetchall()
                for line in lines:
                    undelivered = Decimal(line["amount_mu"]) - Decimal(line["consumed_mu"])
                    consumed_total += Decimal(line["consumed_mu"])
                    if undelivered <= ZERO:
                        continue
                    entitlement = self.connection.execute(
                        "SELECT * FROM entitlements WHERE entitlement_id=?", (line["entitlement_id"],)
                    ).fetchone()
                    new_frozen = Decimal(entitlement["frozen_mu"]) - undelivered
                    cursor = self.connection.execute(
                        "UPDATE entitlements SET frozen_mu=?,revision=revision+1 "
                        "WHERE entitlement_id=? AND revision=?",
                        (decimal_text(new_frozen), line["entitlement_id"], entitlement["revision"]),
                    )
                    if cursor.rowcount != 1:
                        raise Conflict("权益额度并发变更，请重试")
                    balance = available_mu(
                        Decimal(entitlement["granted_mu"]),
                        new_frozen,
                        Decimal(entitlement["consumed_mu"]),
                        Decimal(entitlement["expired_mu"]),
                    )
                    self._entry(
                        household_id=application["household_id"],
                        entitlement_id=line["entitlement_id"],
                        kind="return",
                        amount=undelivered,
                        year=today.year,
                        balance_after=balance,
                        rule_version=int(application["rule_version"]),
                        reason=f"申请{closure.outcome}，返还 {line['year']} 年度未交付额度：{closure.reason}",
                        actor_id=actor_id,
                        application_id=application_id,
                        reservation_id=reservation["reservation_id"],
                    )
                    self._refresh_entitlement_state(line["entitlement_id"])
                    returned_total += undelivered
                self.connection.execute(
                    "UPDATE reservations SET state='closed' WHERE reservation_id=?",
                    (reservation["reservation_id"],),
                )
                release = Decimal(application["requested_mu"]) - consumed_total
                plot = self.connection.execute(
                    "SELECT * FROM plots WHERE plot_id=?", (reservation["plot_id"],)
                ).fetchone()
                self.connection.execute(
                    "UPDATE plots SET reserved_mu=?,revision=revision+1 WHERE plot_id=? AND revision=?",
                    (
                        decimal_text(Decimal(plot["reserved_mu"]) - release),
                        plot["plot_id"],
                        plot["revision"],
                    ),
                )
            elif application["state"] == "under-review":
                self.connection.execute(
                    "UPDATE exceptions SET state='cancelled',revision=revision+1 "
                    "WHERE application_id=? AND state='pending'",
                    (application_id,),
                )
            elif application["state"] != "accepted":
                raise InvalidState("申请当前状态不可退出或失败")
            self.connection.execute(
                "UPDATE applications SET state=?,revision=revision+1 WHERE application_id=?",
                (closure.outcome, application_id),
            )
            self._audit(
                "application",
                application_id,
                "application.closed",
                actor_id,
                {
                    "outcome": closure.outcome,
                    "reason": closure.reason,
                    "returned_mu": decimal_text(returned_total),
                    "consumed_mu": decimal_text(consumed_total),
                },
            )
        return {
            "application_id": application_id,
            "state": closure.outcome,
            "returned_mu": decimal_text(quantize_mu(returned_total)),
            "consumed_mu": decimal_text(quantize_mu(consumed_total)),
        }

    # ------------------------------------------------------------------
    # 家庭可见性与账本解释
    # ------------------------------------------------------------------

    def _entitlement_item(self, row: sqlite3.Row) -> dict[str, Any]:
        available = available_mu(
            Decimal(row["granted_mu"]),
            Decimal(row["frozen_mu"]),
            Decimal(row["consumed_mu"]),
            Decimal(row["expired_mu"]),
        )
        return {
            "entitlement_id": row["entitlement_id"],
            "source": row["source"],
            "land_categories": json.loads(row["land_categories_json"]),
            "granted_mu": row["granted_mu"],
            "frozen_mu": row["frozen_mu"],
            "consumed_mu": row["consumed_mu"],
            "expired_mu": row["expired_mu"],
            "available_mu": decimal_text(available),
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "state": row["state"],
        }

    def household_summary(self, actor_id: str, household_id: str) -> dict[str, Any]:
        self._require_household_access(actor_id, household_id)
        household = self._household(household_id)
        rows = self.connection.execute(
            "SELECT * FROM entitlements WHERE household_id=? ORDER BY entitlement_id",
            (household_id,),
        ).fetchall()
        items = [self._entitlement_item(row) for row in rows]
        totals = {
            key: decimal_text(quantize_mu(sum((Decimal(item[key]) for item in items), ZERO)))
            for key in ("granted_mu", "frozen_mu", "consumed_mu", "expired_mu", "available_mu")
        }
        applications = self.connection.execute(
            "SELECT application_id,land_category,requested_mu,state FROM applications "
            "WHERE household_id=? ORDER BY submitted_at,application_id",
            (household_id,),
        ).fetchall()
        exceptions = self.connection.execute(
            "SELECT exception_id,application_id,overshoot_mu,state,expires_at FROM exceptions "
            "WHERE household_id=? ORDER BY submitted_at,exception_id",
            (household_id,),
        ).fetchall()
        return {
            "household_id": household_id,
            "name": household["name"],
            "as_of": self._now(),
            "entitlements": items,
            "totals": totals,
            "applications": [dict(row) for row in applications],
            "exceptions": [dict(row) for row in exceptions],
        }

    def household_entries(self, actor_id: str, household_id: str, limit: int = 50) -> dict[str, Any]:
        self._require_household_access(actor_id, household_id)
        self._household(household_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationFailed("limit 必须是 1 到 200 的整数")
        rows = self.connection.execute(
            "SELECT e.*,t.source FROM ledger_entries e "
            "JOIN entitlements t ON t.entitlement_id=e.entitlement_id "
            "WHERE e.household_id=? ORDER BY e.entry_id DESC LIMIT ?",
            (household_id, limit),
        ).fetchall()
        return {
            "household_id": household_id,
            "entries": [
                {
                    "entry_id": row["entry_id"],
                    "entitlement_id": row["entitlement_id"],
                    "source": row["source"],
                    "kind": row["kind"],
                    "amount_mu": row["amount_mu"],
                    "year": row["year"],
                    "balance_after_mu": row["balance_after_mu"],
                    "application_id": row["application_id"],
                    "reason": row["reason"],
                    "actor_id": row["actor_id"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    def explain_entry(self, actor_id: str, entry_id: int) -> dict[str, Any]:
        self._require(actor_id, "ledger.explain")
        entry = self.connection.execute(
            "SELECT * FROM ledger_entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if entry is None:
            raise NotFound("账本流水不存在")
        entitlement = self.connection.execute(
            "SELECT * FROM entitlements WHERE entitlement_id=?", (entry["entitlement_id"],)
        ).fetchone()
        rule = self.connection.execute(
            "SELECT * FROM rule_versions WHERE rule_version=?", (entry["rule_version"],)
        ).fetchone()
        result: dict[str, Any] = {
            "entry": dict(entry),
            "entitlement": self._entitlement_item(entitlement),
            "rule": None
            if rule is None
            else {
                "rule_version": rule["rule_version"],
                "effective_year": rule["effective_year"],
                "selection_strategy": rule["selection_strategy"],
                "source_priority": json.loads(rule["source_priority_json"]),
            },
        }
        if entry["application_id"] is not None:
            result["application"] = dict(self._application(entry["application_id"]))
        if entry["exception_id"] is not None:
            exception = self.connection.execute(
                "SELECT * FROM exceptions WHERE exception_id=?", (entry["exception_id"],)
            ).fetchone()
            result["exception"] = None if exception is None else dict(exception)
        return result

    def explain_application(self, actor_id: str, application_id: str) -> dict[str, Any]:
        self._require(actor_id, "ledger.explain")
        application = self._application(application_id)
        slices = self.connection.execute(
            "SELECT * FROM application_slices WHERE application_id=? ORDER BY year",
            (application_id,),
        ).fetchall()
        exception = self.connection.execute(
            "SELECT * FROM exceptions WHERE application_id=?", (application_id,)
        ).fetchone()
        reservation = self.connection.execute(
            "SELECT * FROM reservations WHERE application_id=?", (application_id,)
        ).fetchone()
        lines: list[dict[str, Any]] = []
        if reservation is not None:
            line_rows = self.connection.execute(
                "SELECT l.*,t.source FROM reservation_lines l "
                "JOIN entitlements t ON t.entitlement_id=l.entitlement_id "
                "WHERE l.reservation_id=? ORDER BY l.line_id",
                (reservation["reservation_id"],),
            ).fetchall()
            lines = [dict(row) for row in line_rows]
        deliveries = self.connection.execute(
            "SELECT * FROM deliveries WHERE application_id=? ORDER BY created_at,delivery_id",
            (application_id,),
        ).fetchall()
        entries = self.connection.execute(
            "SELECT * FROM ledger_entries WHERE application_id=? ORDER BY entry_id",
            (application_id,),
        ).fetchall()
        return {
            "application": dict(application),
            "slices": [dict(row) for row in slices],
            "exception": None if exception is None else dict(exception),
            "reservation": None if reservation is None else dict(reservation),
            "lines": lines,
            "deliveries": [dict(row) for row in deliveries],
            "entries": [dict(row) for row in entries],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM audit_events ORDER BY event_id").fetchall()
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
