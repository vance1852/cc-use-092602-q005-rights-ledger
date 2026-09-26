from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from rights_ledger.api import JsonApplication
from rights_ledger.clock import FrozenClock
from rights_ledger.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from rights_ledger.ledger import QuotaCandidate, select_quota, split_yearly_occupancy
from rights_ledger.service import RightsLedgerService


class LedgerPureTests(unittest.TestCase):
    def test_split_yearly_occupancy_proportional_and_exact(self) -> None:
        from datetime import date

        slices = split_yearly_occupancy(date(2026, 10, 1), date(2028, 3, 31), Decimal("1.5"))
        self.assertEqual([item.year for item in slices], [2026, 2027, 2028])
        self.assertEqual(sum((item.planned_mu for item in slices), Decimal("0")), Decimal("1.500"))
        # 2028 为闰年，全程 548 天：92 / 365 / 91。
        self.assertEqual(slices[0].planned_mu, Decimal("0.252"))
        self.assertEqual(slices[1].planned_mu, Decimal("0.999"))
        self.assertEqual(slices[2].planned_mu, Decimal("0.249"))

    def test_split_rejects_inverted_range(self) -> None:
        from datetime import date

        with self.assertRaises(ValueError):
            split_yearly_occupancy(date(2027, 5, 1), date(2027, 4, 1), Decimal("1"))

    def test_select_quota_expiry_first_and_shortfall(self) -> None:
        candidates = [
            QuotaCandidate("g-late", "relocation-bonus", Decimal("1.0"), "2026-01-01", "2027-12-31"),
            QuotaCandidate("g-early", "homestead-eligibility", Decimal("1.0"), "2026-01-01", "2026-12-31"),
            QuotaCandidate("g-open", "contracted-land-merge", Decimal("1.0"), "2026-01-01", None),
        ]
        selection = select_quota(candidates, Decimal("2.5"), "expiry_first", [])
        self.assertEqual([(take.grant_id, take.amount_mu) for take in selection.takes], [
            ("g-early", Decimal("1.000")),
            ("g-late", Decimal("1.000")),
            ("g-open", Decimal("0.500")),
        ])
        self.assertEqual(selection.shortfall_mu, Decimal("0.000"))
        short = select_quota(candidates, Decimal("4"), "expiry_first", [])
        self.assertEqual(short.shortfall_mu, Decimal("1.000"))

    def test_select_quota_source_priority(self) -> None:
        candidates = [
            QuotaCandidate("g-bonus", "relocation-bonus", Decimal("1.0"), "2026-01-01", "2026-06-30"),
            QuotaCandidate("g-contract", "contracted-land-merge", Decimal("1.0"), "2026-01-01", "2026-03-31"),
        ]
        selection = select_quota(
            candidates,
            Decimal("1.5"),
            "source_priority",
            ["relocation-bonus", "contracted-land-merge"],
        )
        self.assertEqual(selection.takes[0].grant_id, "g-bonus")
        self.assertEqual(selection.takes[1].grant_id, "g-contract")


class RightsLedgerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = RightsLedgerService(self.connection, self.clock)
        self.service.create_user("clerk", "经办员", "clerk")
        self.service.create_user("manager", "产权管理", "manager")
        self.service.create_user("manager2", "产权管理乙", "manager")
        self.service.create_user("reviewer", "复核员", "reviewer")
        self.service.create_user("auditor", "审计员", "auditor")
        self.service.register_household("clerk", "household-east", "陈东户", "东岗村")
        self.service.register_household("clerk", "household-west", "李西户", "东岗村")
        self.service.create_user("hh-east", "陈东", "household", household_id="household-east")
        self.service.register_plot("manager", "plot-h1", "东岗村", "homestead", "2.0")
        self.service.register_plot("manager", "plot-h2", "东岗村", "homestead", "1.5")
        self.service.create_rule("manager", {
            "rule_id": "rule-2026",
            "effective_year": 2026,
            "selection_policy": "expiry_first",
            "source_priority": ["contracted-land-merge", "homestead-eligibility", "relocation-bonus"],
            "review_deadline_days": 30,
        })

    def tearDown(self) -> None:
        self.connection.close()

    def grant(self, grant_id: str, household: str, source: str, category: str, quantity: str, expires: str | None) -> None:
        self.service.grant_entitlement("manager", {
            "grant_id": grant_id,
            "household_id": household,
            "source": source,
            "category": category,
            "quantity_mu": quantity,
            "effective_from": "2026-01-01",
            "expires_at": expires,
            "reason": "测试授予",
        })

    def application_payload(self, application_id: str, plot: str, requested: str, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "application_id": application_id,
            "household_id": "household-east",
            "plot_id": plot,
            "category": "homestead",
            "requested_mu": requested,
            "project_start": "2026-10-01",
            "project_end": "2027-06-30",
            "idempotency_key": f"key-{application_id}",
        }
        payload.update(overrides)
        return payload

    def test_grant_and_summary_balances(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "1.2", "2026-12-31")
        self.grant("g-2", "household-east", "relocation-bonus", "homestead", "0.8", None)
        summary = self.service.household_summary("manager", "household-east")
        self.assertEqual(summary["totals"]["granted_mu"], "2.000")
        self.assertEqual(summary["totals"]["available_mu"], "2.000")
        self.assertEqual(summary["categories"]["homestead"]["available_mu"], "2.000")

    def test_submit_freezes_by_expiry_first(self) -> None:
        self.grant("g-late", "household-east", "relocation-bonus", "homestead", "1.0", "2027-12-31")
        self.grant("g-early", "household-east", "homestead-eligibility", "homestead", "1.0", "2026-12-31")
        response = self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.5"))
        self.assertEqual(response["state"], "submitted")
        rows = self.connection.execute(
            "SELECT grant_id,frozen_mu FROM entitlement_grants ORDER BY grant_id"
        ).fetchall()
        frozen = {row["grant_id"]: row["frozen_mu"] for row in rows}
        self.assertEqual(frozen["g-early"], "1.000")
        self.assertEqual(frozen["g-late"], "0.500")

    def test_submit_is_idempotent_and_payload_conflict(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "2.0", None)
        payload = self.application_payload("app-1", "plot-h1", "1.0")
        first = self.service.submit_application("clerk", payload)
        self.assertEqual(first, self.service.submit_application("clerk", payload))
        with self.assertRaises(Conflict):
            self.service.submit_application("clerk", dict(payload, requested_mu="1.2"))

    def test_expired_rights_are_swept_before_selection(self) -> None:
        self.grant("g-old", "household-east", "homestead-eligibility", "homestead", "1.0", "2026-12-31")
        self.grant("g-new", "household-east", "relocation-bonus", "homestead", "1.0", "2028-06-30")
        self.clock.advance(days=120)  # 2027-01-24
        response = self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.5"))
        self.assertEqual(response["state"], "pending_review")
        self.assertEqual(response["frozen_mu"], "1.000")
        self.assertEqual(response["shortfall_mu"], "0.500")
        grant = self.connection.execute("SELECT * FROM entitlement_grants WHERE grant_id='g-old'").fetchone()
        self.assertEqual(grant["expired_mu"], "1.000")
        self.assertEqual(grant["state"], "expired")
        entries = self.service.household_entries("manager", "household-east")["entries"]
        self.assertTrue(any(entry["kind"] == "expire" and entry["grant_id"] == "g-old" for entry in entries))

    def test_over_quota_queues_exception_with_deadline(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "1.0", None)
        response = self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "2.5"))
        self.assertEqual(response["state"], "pending_review")
        self.assertEqual(response["shortfall_mu"], "1.500")
        self.assertEqual(response["review_deadline_at"], "2026-10-26T08:00:00Z")
        exception = self.connection.execute("SELECT * FROM exceptions WHERE exception_id='exc-app-1'").fetchone()
        self.assertEqual(exception["state"], "pending")
        self.assertEqual(exception["over_mu"], "1.500")

    def test_reviewer_cannot_approve_own_exception(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "1.0", None)
        self.service.submit_application("manager", self.application_payload("app-1", "plot-h1", "2.0", idempotency_key="key-own"))
        with self.assertRaises(Forbidden):
            self.service.review_exception("manager", "exc-app-1", "approve", "自审自批")
        decided = self.service.review_exception("manager2", "exc-app-1", "approve", "特殊情况批准")
        self.assertEqual(decided["state"], "approved")

    def test_approval_creates_grant_and_enables_confirm(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "1.0", None)
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "2.0"))
        self.service.review_exception("reviewer", "exc-app-1", "approve", "搬迁过渡困难")
        grant = self.connection.execute("SELECT * FROM entitlement_grants WHERE grant_id='excgrant-exc-app-1'").fetchone()
        self.assertEqual(grant["source"], "exception-approval")
        self.assertEqual(grant["quantity_mu"], "1.000")
        self.assertEqual(grant["expires_at"], "2026-12-31")
        confirmed = self.service.confirm_allocation("manager", "app-1")
        self.assertEqual(confirmed["state"], "reserved")

    def test_rejection_releases_frozen_quota(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "1.0", None)
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "2.0"))
        self.service.review_exception("reviewer", "exc-app-1", "reject", "不符合政策")
        grant = self.connection.execute("SELECT * FROM entitlement_grants WHERE grant_id='g-1'").fetchone()
        self.assertEqual(grant["frozen_mu"], "0.000")
        application = self.connection.execute("SELECT * FROM applications WHERE application_id='app-1'").fetchone()
        self.assertEqual(application["state"], "rejected")

    def test_exception_expires_after_deadline(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "1.0", None)
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "2.0"))
        self.clock.advance(days=31)
        with self.assertRaises(InvalidState):
            self.service.review_exception("reviewer", "exc-app-1", "approve", "超期审批")
        exception = self.connection.execute("SELECT * FROM exceptions WHERE exception_id='exc-app-1'").fetchone()
        self.assertEqual(exception["state"], "expired")
        grant = self.connection.execute("SELECT * FROM entitlement_grants WHERE grant_id='g-1'").fetchone()
        self.assertEqual(grant["frozen_mu"], "0.000")

    def test_confirm_reserves_plot_atomically_and_splits_years(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "2.0", None)
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.5"))
        confirmed = self.service.confirm_allocation("manager", "app-1")
        self.assertEqual([item["year"] for item in confirmed["years"]], [2026, 2027])
        total = sum(Decimal(item["planned_mu"]) for item in confirmed["years"])
        self.assertEqual(total, Decimal("1.500"))
        plot = self.connection.execute("SELECT * FROM plots WHERE plot_id='plot-h1'").fetchone()
        self.assertEqual(plot["state"], "reserved")
        self.grant("g-2", "household-west", "homestead-eligibility", "homestead", "2.0", None)
        self.service.submit_application("clerk", self.application_payload(
            "app-2", "plot-h1", "1.0", household_id="household-west",
        ))
        with self.assertRaises(Conflict):
            self.service.confirm_allocation("manager", "app-2")

    def test_cross_year_split_uses_injected_clock(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "3.0", None)
        self.service.submit_application("clerk", self.application_payload(
            "app-1", "plot-h1", "2.0", project_start="2026-01-01", project_end="2027-06-30",
        ))
        confirmed = self.service.confirm_allocation("manager", "app-1")
        years = {item["year"]: item["planned_mu"] for item in confirmed["years"]}
        # 时钟为 2026-09-26，2026 年只切分 9-26 到年末的 97 天。
        self.assertEqual(years[2026], "0.698")
        self.assertEqual(Decimal(years[2026]) + Decimal(years[2027]), Decimal("2.000"))

    def test_delivery_consumes_freeze_and_is_idempotent(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "2.0", None)
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.5"))
        self.service.confirm_allocation("manager", "app-1")
        payload = {"delivery_id": "dlv-1", "reservation_id": "rsv-app-1", "year": 2026, "amount_mu": "0.5", "idempotency_key": "dlv-key-1"}
        first = self.service.record_delivery("manager", payload)
        self.assertEqual(first["state"], "in_delivery")
        self.assertEqual(first, self.service.record_delivery("manager", payload))
        with self.assertRaises(Conflict):
            self.service.record_delivery("manager", dict(payload, amount_mu="0.6"))
        grant = self.connection.execute("SELECT * FROM entitlement_grants WHERE grant_id='g-1'").fetchone()
        self.assertEqual(grant["consumed_mu"], "0.500")
        self.assertEqual(grant["frozen_mu"], "1.000")
        with self.assertRaises(InvalidState):
            self.service.record_delivery("manager", dict(payload, delivery_id="dlv-2", year=2027, idempotency_key="dlv-key-2"))

    def test_exit_returns_only_undelivered_portion(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "2.0", "2027-12-31")
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.5"))
        self.service.confirm_allocation("manager", "app-1")
        self.service.record_delivery("manager", {"delivery_id": "dlv-1", "reservation_id": "rsv-app-1", "year": 2026, "amount_mu": "0.5", "idempotency_key": "dlv-key-1"})
        exited = self.service.exit_reservation("manager", "rsv-app-1", "家庭退出安置")
        self.assertEqual(exited["returned_mu"], "1.000")
        self.assertEqual(exited["delivered_kept_mu"], "0.500")
        grant = self.connection.execute("SELECT * FROM entitlement_grants WHERE grant_id='g-1'").fetchone()
        self.assertEqual(grant["consumed_mu"], "0.500")
        self.assertEqual(grant["frozen_mu"], "0.000")
        plot = self.connection.execute("SELECT * FROM plots WHERE plot_id='plot-h1'").fetchone()
        self.assertEqual(plot["state"], "available")

    def test_exit_after_grant_expiry_writes_off_return(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "1.5", "2026-12-31")
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.5"))
        self.service.confirm_allocation("manager", "app-1")
        self.clock.advance(days=120)  # 2027-01-24，权益已过期
        exited = self.service.exit_reservation("manager", "rsv-app-1", "项目失败", failed=True)
        self.assertEqual(exited["state"], "failed")
        self.assertEqual(exited["returned_mu"], "0.000")
        self.assertEqual(exited["expired_mu"], "1.500")
        grant = self.connection.execute("SELECT * FROM entitlement_grants WHERE grant_id='g-1'").fetchone()
        self.assertEqual(grant["expired_mu"], "1.500")

    def test_settlement_locks_year_against_delivery_and_rules(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "2.0", None)
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.5"))
        self.service.confirm_allocation("manager", "app-1")
        with self.assertRaises(InvalidState):
            self.service.settle_year("manager", 2026)
        self.clock.advance(days=120)  # 2027-01-24
        settled = self.service.settle_year("manager", 2026)
        self.assertEqual(settled["reservation_count"], 1)
        with self.assertRaises(Conflict):
            self.service.settle_year("manager", 2026)
        with self.assertRaises(InvalidState):
            self.service.record_delivery("manager", {"delivery_id": "dlv-1", "reservation_id": "rsv-app-1", "year": 2026, "amount_mu": "0.5", "idempotency_key": "dlv-key-1"})
        with self.assertRaises(Conflict):
            self.service.create_rule("manager", {"rule_id": "rule-retro", "effective_year": 2026, "selection_policy": "expiry_first", "source_priority": [], "review_deadline_days": 15})
        created = self.service.create_rule("manager", {"rule_id": "rule-2027", "effective_year": 2027, "selection_policy": "source_priority", "source_priority": ["relocation-bonus"], "review_deadline_days": 15})
        self.assertEqual(created["effective_year"], 2027)

    def test_household_visibility_is_scoped(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "2.0", None)
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.0"))
        summary = self.service.household_summary("hh-east", "household-east")
        self.assertEqual(summary["totals"]["frozen_mu"], "1.000")
        with self.assertRaises(Forbidden):
            self.service.household_summary("hh-east", "household-west")
        own_entries = self.service.household_entries("hh-east", "household-east")["entries"]
        self.assertTrue(own_entries)
        self.assertTrue(all(entry["visibility"] == "household" for entry in own_entries))
        self.assertFalse(any(entry["kind"] == "freeze" for entry in own_entries))
        full_entries = self.service.household_entries("manager", "household-east")["entries"]
        self.assertTrue(any(entry["kind"] == "freeze" for entry in full_entries))
        with self.assertRaises(Forbidden):
            self.service.household_entries("hh-east", "household-west")

    def test_explain_traces_deduction_return_and_exception(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "3.0", None)
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.0"))
        self.service.confirm_allocation("manager", "app-1")
        self.service.record_delivery("manager", {"delivery_id": "dlv-1", "reservation_id": "rsv-app-1", "year": 2026, "amount_mu": "0.4", "idempotency_key": "dlv-key-1"})
        self.service.exit_reservation("manager", "rsv-app-1", "部分退出")
        entries = self.service.household_entries("auditor", "household-east")["entries"]
        consume = next(entry for entry in entries if entry["kind"] == "consume")
        explained = self.service.explain_entry("auditor", consume["entry_id"])
        self.assertEqual(explained["entry"]["delivery_id"], "dlv-1")
        self.assertEqual(explained["reservation"]["state"], "exited")
        self.assertEqual(explained["grant"]["consumed_mu"], "0.400")
        returned = next(entry for entry in entries if entry["kind"] == "return")
        explained_return = self.service.explain_entry("auditor", returned["entry_id"])
        self.assertEqual(explained_return["entry"]["amount_mu"], "0.600")
        self.service.submit_application("clerk", self.application_payload("app-2", "plot-h2", "5.0"))
        self.service.review_exception("reviewer", "exc-app-2", "approve", "特殊批准")
        explained_exception = self.service.explain_exception("auditor", "exc-app-2")
        self.assertEqual(explained_exception["exception"]["state"], "approved")
        self.assertEqual(explained_exception["grant"]["source"], "exception-approval")
        with self.assertRaises(Forbidden):
            self.service.explain_entry("clerk", consume["entry_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "1.0", None)
        self.assertTrue(self.service.audit_chain("auditor")["valid"])
        self.connection.execute("UPDATE ledger_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("auditor")["valid"])

    def test_cancel_application_releases_freezes(self) -> None:
        self.grant("g-1", "household-east", "homestead-eligibility", "homestead", "2.0", None)
        self.service.submit_application("clerk", self.application_payload("app-1", "plot-h1", "1.0"))
        cancelled = self.service.cancel_application("clerk", "app-1", "家庭撤回申请")
        self.assertEqual(cancelled["state"], "cancelled")
        grant = self.connection.execute("SELECT * FROM entitlement_grants WHERE grant_id='g-1'").fetchone()
        self.assertEqual(grant["frozen_mu"], "0.000")
        with self.assertRaises(InvalidState):
            self.service.confirm_allocation("manager", "app-1")
        self.service.submit_application("clerk", self.application_payload("app-2", "plot-h1", "1.0"))
        self.service.confirm_allocation("manager", "app-2")
        with self.assertRaises(InvalidState):
            self.service.cancel_application("clerk", "app-2", "已确认不可撤销")

    def test_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.grant_entitlement("clerk", {"grant_id": "g-x", "household_id": "household-east", "source": "relocation-bonus", "category": "homestead", "quantity_mu": "1", "effective_from": "2026-01-01", "reason": "越权"})
        with self.assertRaises(Forbidden):
            self.service.create_rule("clerk", {"rule_id": "r-x", "effective_year": 2026, "selection_policy": "expiry_first", "source_priority": [], "review_deadline_days": 10})
        with self.assertRaises(Forbidden):
            self.service.settle_year("reviewer", 2026)
        with self.assertRaises(NotFound):
            self.service.household_summary("manager", "household-none")

    def test_api_routes_and_error_shape(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        created = app.handle("POST", "/grants", {"X-Actor-Id": "manager"}, json.dumps({
            "grant_id": "g-1", "household_id": "household-east", "source": "homestead-eligibility",
            "category": "homestead", "quantity_mu": "1.0", "effective_from": "2026-01-01", "reason": "接口授予",
        }).encode())
        self.assertEqual(created.status, 201)
        summary = app.handle("GET", "/households/household-east/summary", {"X-Actor-Id": "hh-east"})
        self.assertEqual(summary.status, 200)
        forbidden = app.handle("GET", "/households/household-west/summary", {"X-Actor-Id": "hh-east"})
        self.assertEqual(forbidden.status, 403)
        missing_actor = app.handle("GET", "/households/household-east/summary")
        self.assertEqual(missing_actor.status, 422)
        unknown = app.handle("GET", "/no-such-route", {"X-Actor-Id": "manager"})
        self.assertEqual(unknown.status, 404)


if __name__ == "__main__":
    unittest.main()
