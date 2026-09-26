from __future__ import annotations

import sqlite3
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from rights_ledger.api import JsonApplication
from rights_ledger.clock import FrozenClock
from rights_ledger.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from rights_ledger.quota import (
    EntitlementView,
    select_quota,
    split_estimated_occupancy,
)
from rights_ledger.service import RightsLedgerService


TODAY = date(2026, 9, 26)


def view(
    entitlement_id: str,
    source: str = "contract-merge",
    categories: frozenset[str] = frozenset({"cultivated-land"}),
    available: str = "10",
    valid_from: date = date(2026, 1, 1),
    valid_until: date = date(2027, 12, 31),
) -> EntitlementView:
    return EntitlementView(entitlement_id, source, categories, Decimal(available), valid_from, valid_until)


class QuotaTests(unittest.TestCase):
    def test_split_cross_year_sums_exactly(self) -> None:
        slices = split_estimated_occupancy(date(2026, 10, 1), date(2027, 9, 30), Decimal("30"), TODAY)
        self.assertEqual([item.year for item in slices], [2026, 2027])
        self.assertEqual(slices[0].planned_mu, Decimal("7.562"))
        self.assertEqual(slices[1].planned_mu, Decimal("22.438"))
        self.assertEqual(sum((item.planned_mu for item in slices), Decimal("0")), Decimal("30.000"))

    def test_split_single_year_keeps_total(self) -> None:
        slices = split_estimated_occupancy(date(2026, 10, 1), date(2026, 12, 31), Decimal("12.5"), TODAY)
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0].planned_mu, Decimal("12.500"))

    def test_split_rejects_ended_project(self) -> None:
        with self.assertRaises(ValueError):
            split_estimated_occupancy(date(2026, 1, 1), date(2026, 9, 25), Decimal("5"), TODAY)

    def test_select_quota_prefers_earliest_expiry(self) -> None:
        slices = split_estimated_occupancy(date(2026, 10, 1), date(2026, 12, 31), Decimal("8"), TODAY)
        entitlements = [
            view("ent-late", available="50", valid_until=date(2028, 12, 31)),
            view("ent-early", available="10", valid_until=date(2026, 12, 31)),
        ]
        plan = select_quota(entitlements, slices, "cultivated-land", "earliest_expiry_first", [], TODAY)
        self.assertEqual(plan.shortfall_mu, Decimal("0"))
        self.assertEqual(plan.lines[0].entitlement_id, "ent-early")
        self.assertEqual(plan.lines[0].amount_mu, Decimal("8.000"))

    def test_select_quota_source_priority_beats_expiry(self) -> None:
        slices = split_estimated_occupancy(date(2026, 10, 1), date(2026, 12, 31), Decimal("8"), TODAY)
        entitlements = [
            view("ent-reward", source="relocation-reward", available="10", valid_until=date(2026, 12, 31)),
            view("ent-merge", source="contract-merge", available="50", valid_until=date(2028, 12, 31)),
        ]
        plan = select_quota(
            entitlements,
            slices,
            "cultivated-land",
            "source_priority",
            ["contract-merge", "relocation-reward"],
            TODAY,
        )
        self.assertEqual(plan.lines[0].entitlement_id, "ent-merge")

    def test_select_quota_filters_category_and_reports_shortfall(self) -> None:
        slices = split_estimated_occupancy(date(2026, 10, 1), date(2026, 12, 31), Decimal("20"), TODAY)
        entitlements = [
            view("ent-homestead", categories=frozenset({"homestead"}), available="100"),
            view("ent-small", available="6"),
        ]
        plan = select_quota(entitlements, slices, "cultivated-land", "earliest_expiry_first", [], TODAY)
        self.assertEqual(plan.covered_mu, Decimal("6.000"))
        self.assertEqual(plan.shortfall_mu, Decimal("14.000"))

    def test_select_quota_requires_full_segment_coverage(self) -> None:
        slices = split_estimated_occupancy(date(2026, 10, 1), date(2026, 12, 31), Decimal("5"), TODAY)
        entitlements = [view("ent-mid", available="100", valid_until=date(2026, 11, 30))]
        plan = select_quota(entitlements, slices, "cultivated-land", "earliest_expiry_first", [], TODAY)
        self.assertEqual(plan.shortfall_mu, Decimal("5.000"))
        self.assertEqual(plan.lines, ())


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = RightsLedgerService(self.connection, self.clock)
        for user_id, role in (
            ("clerk", "clerk"),
            ("manager", "manager"),
            ("reviewer", "reviewer"),
            ("reviewer2", "reviewer"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_household("clerk", {"household_id": "hh-1", "name": "张家", "village": "北村"})
        self.service.register_household("clerk", {"household_id": "hh-2", "name": "李家", "village": "北村"})
        self.service.create_user("fam-1", "张家户主", "household", "hh-1")
        self.service.publish_rule(
            "manager",
            {
                "effective_year": 2026,
                "selection_strategy": "earliest_expiry_first",
                "review_window_hours": 72,
                "max_overshoot_percent": "50",
            },
        )
        self.service.register_plot(
            "clerk",
            {"plot_id": "plot-1", "village": "北村", "land_category": "cultivated-land", "area_mu": "100"},
        )
        self.service.register_plot(
            "clerk",
            {"plot_id": "plot-2", "village": "北村", "land_category": "homestead", "area_mu": "20"},
        )

    def tearDown(self) -> None:
        self.connection.close()

    def grant(
        self,
        entitlement_id: str = "ent-1",
        household_id: str = "hh-1",
        source: str = "contract-merge",
        categories: tuple[str, ...] = ("cultivated-land",),
        amount: str = "80",
        valid_from: str = "2026-01-01",
        valid_until: str = "2028-12-31",
    ) -> dict[str, object]:
        return self.service.grant_entitlement(
            "manager",
            {
                "entitlement_id": entitlement_id,
                "household_id": household_id,
                "source": source,
                "land_categories": list(categories),
                "granted_mu": amount,
                "valid_from": valid_from,
                "valid_until": valid_until,
            },
        )

    def submit(
        self,
        application_id: str = "app-1",
        household_id: str = "hh-1",
        category: str = "cultivated-land",
        amount: str = "30",
        starts_on: str = "2026-10-01",
        ends_on: str = "2027-09-30",
        key: str = "key-1",
        actor: str = "clerk",
    ) -> dict[str, object]:
        return self.service.submit_application(
            actor,
            {
                "application_id": application_id,
                "household_id": household_id,
                "land_category": category,
                "requested_mu": amount,
                "starts_on": starts_on,
                "ends_on": ends_on,
                "idempotency_key": key,
            },
        )

    def confirm(self, application_id: str = "app-1", plot_id: str = "plot-1", revision: int = 1) -> dict[str, object]:
        return self.service.confirm_allocation(
            "clerk",
            application_id,
            {"reservation_id": f"rsv-{application_id}", "plot_id": plot_id, "expected_revision": revision},
        )

    def test_grant_and_household_summary(self) -> None:
        self.grant()
        summary = self.service.household_summary("fam-1", "hh-1")
        self.assertEqual(summary["totals"]["available_mu"], "80.000")
        self.assertEqual(summary["entitlements"][0]["source"], "contract-merge")
        entries = self.service.household_entries("fam-1", "hh-1")
        self.assertEqual([entry["kind"] for entry in entries["entries"]], ["grant"])
        self.assertEqual(entries["entries"][0]["balance_after_mu"], "80")

    def test_household_isolation_for_summary_and_entries(self) -> None:
        self.grant()
        with self.assertRaises(Forbidden):
            self.service.household_summary("fam-1", "hh-2")
        with self.assertRaises(Forbidden):
            self.service.household_entries("fam-1", "hh-2")
        with self.assertRaises(Forbidden):
            self.service.household_summary("clerk", "hh-1")
        self.assertEqual(self.service.household_summary("auditor", "hh-1")["household_id"], "hh-1")

    def test_household_user_requires_bound_household(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_user("fam-x", "无户用户", "household")

    def test_submit_accepted_and_idempotent_replay(self) -> None:
        self.grant()
        first = self.submit()
        self.assertEqual(first["state"], "accepted")
        self.assertEqual([item["year"] for item in first["slices"]], [2026, 2027])
        replay = self.submit()
        self.assertEqual(first, replay)
        changed = {
            "application_id": "app-1",
            "household_id": "hh-1",
            "land_category": "cultivated-land",
            "requested_mu": "31",
            "starts_on": "2026-10-01",
            "ends_on": "2027-09-30",
            "idempotency_key": "key-1",
        }
        with self.assertRaises(Conflict):
            self.service.submit_application("clerk", changed)

    def test_submit_without_quota_queues_timed_exception(self) -> None:
        self.grant(entitlement_id="ent-li", household_id="hh-2", source="homestead-eligibility",
                   categories=("homestead",), amount="5", valid_until="2027-12-31")
        result = self.submit(application_id="app-li", household_id="hh-2", category="homestead",
                             amount="8", ends_on="2027-06-30", key="key-li")
        self.assertEqual(result["state"], "under-review")
        self.assertEqual(result["overshoot_mu"], "3.000")
        self.assertEqual(result["exception"]["exception_id"], "exc-app-li")
        self.assertEqual(result["exception"]["expires_at"], "2026-09-29T08:00:00Z")

    def test_submit_overshoot_beyond_cap_rejected(self) -> None:
        self.grant(entitlement_id="ent-li", household_id="hh-2", source="homestead-eligibility",
                   categories=("homestead",), amount="5", valid_until="2027-12-31")
        with self.assertRaises(Conflict):
            self.submit(application_id="app-li", household_id="hh-2", category="homestead",
                        amount="12", ends_on="2027-06-30", key="key-li")

    def test_reviewer_cannot_approve_own_exception(self) -> None:
        self.grant(entitlement_id="ent-li", household_id="hh-2", source="homestead-eligibility",
                   categories=("homestead",), amount="5", valid_until="2027-12-31")
        result = self.submit(application_id="app-li", household_id="hh-2", category="homestead",
                             amount="8", ends_on="2027-06-30", key="key-li", actor="reviewer")
        exception_id = result["exception"]["exception_id"]
        with self.assertRaises(Forbidden):
            self.service.decide_exception("reviewer", exception_id, {"approve": True, "reason": "自审自批"})
        decided = self.service.decide_exception("reviewer2", exception_id, {"approve": True, "reason": "情况属实"})
        self.assertEqual(decided["state"], "approved")
        summary = self.service.household_summary("manager", "hh-2")
        granted = {item["source"]: item["granted_mu"] for item in summary["entitlements"]}
        self.assertEqual(granted["exception-grant"], "3.000")
        application = self.service.explain_application("auditor", "app-li")["application"]
        self.assertEqual(application["state"], "accepted")

    def test_exception_expires_after_review_window(self) -> None:
        self.grant(entitlement_id="ent-li", household_id="hh-2", source="homestead-eligibility",
                   categories=("homestead",), amount="5", valid_until="2027-12-31")
        result = self.submit(application_id="app-li", household_id="hh-2", category="homestead",
                             amount="8", ends_on="2027-06-30", key="key-li")
        self.clock.advance(hours=73)
        with self.assertRaises(InvalidState):
            self.service.decide_exception("reviewer", result["exception"]["exception_id"],
                                          {"approve": True, "reason": "超期"})
        explained = self.service.explain_application("auditor", "app-li")
        self.assertEqual(explained["exception"]["state"], "expired")
        self.assertEqual(explained["application"]["state"], "rejected")

    def test_exception_rejection_blocks_confirmation(self) -> None:
        self.grant(entitlement_id="ent-li", household_id="hh-2", source="homestead-eligibility",
                   categories=("homestead",), amount="5", valid_until="2027-12-31")
        result = self.submit(application_id="app-li", household_id="hh-2", category="homestead",
                             amount="8", ends_on="2027-06-30", key="key-li")
        self.service.decide_exception("reviewer", result["exception"]["exception_id"],
                                      {"approve": False, "reason": "不符合政策"})
        with self.assertRaises(InvalidState):
            self.confirm(application_id="app-li", plot_id="plot-2", revision=2)

    def test_confirm_freezes_quota_and_reserves_plot_atomically(self) -> None:
        self.grant(entitlement_id="ent-contract", amount="80", valid_until="2028-12-31")
        self.grant(entitlement_id="ent-move", source="relocation-reward",
                   categories=("cultivated-land", "homestead"), amount="15",
                   valid_from="2026-06-01", valid_until="2027-12-31")
        self.submit()
        result = self.confirm()
        self.assertEqual(result["state"], "reserved")
        self.assertEqual(
            [(line["entitlement_id"], line["year"], line["amount_mu"]) for line in result["lines"]],
            [("ent-move", 2026, "7.562"), ("ent-move", 2027, "7.438"), ("ent-contract", 2027, "15.000")],
        )
        summary = self.service.household_summary("manager", "hh-1")
        frozen = {item["entitlement_id"]: item["frozen_mu"] for item in summary["entitlements"]}
        self.assertEqual(frozen["ent-move"], "15.000")
        self.assertEqual(frozen["ent-contract"], "15.000")
        plot = self.connection.execute("SELECT * FROM plots WHERE plot_id='plot-1'").fetchone()
        self.assertEqual(plot["reserved_mu"], "30")
        entries = self.service.household_entries("manager", "hh-1")["entries"]
        self.assertEqual([entry["kind"] for entry in entries], ["freeze", "freeze", "freeze", "grant", "grant"])
        with self.assertRaises(InvalidState):
            self.confirm()

    def test_confirm_detects_quota_expired_since_submit(self) -> None:
        self.grant(entitlement_id="ent-long", amount="50", valid_until="2028-12-31")
        self.grant(entitlement_id="ent-short", amount="30", valid_until="2026-12-31")
        self.submit(amount="70", ends_on="2027-03-31")
        self.clock.advance(days=107)  # 2027-01-11
        with self.assertRaises(Conflict) as caught:
            self.confirm()
        self.assertIn("过期", str(caught.exception))

    def test_confirm_detects_quota_frozen_by_other_application(self) -> None:
        self.grant(amount="50", valid_until="2027-12-31")
        self.submit(application_id="app-1", amount="40", ends_on="2027-06-30", key="key-1")
        self.submit(application_id="app-2", amount="40", ends_on="2027-06-30", key="key-2")
        self.confirm(application_id="app-1")
        with self.assertRaises(Conflict) as caught:
            self.confirm(application_id="app-2")
        self.assertIn("冻结", str(caught.exception))

    def test_confirm_requires_matching_plot_category(self) -> None:
        self.grant()
        self.submit()
        with self.assertRaises(Conflict):
            self.confirm(plot_id="plot-2")

    def test_delivery_consumes_frozen_quota_until_completion(self) -> None:
        self.grant()
        self.submit()
        self.confirm()
        first = self.service.record_delivery(
            "clerk", "app-1", {"delivery_id": "dlv-1", "year": 2026, "amount_mu": "7.562"}
        )
        self.assertEqual(first["state"], "delivering")
        with self.assertRaises(InvalidState):
            self.service.record_delivery(
                "clerk", "app-1", {"delivery_id": "dlv-1b", "year": 2026, "amount_mu": "0.001"}
            )
        second = self.service.record_delivery(
            "clerk", "app-1", {"delivery_id": "dlv-2", "year": 2027, "amount_mu": "22.438"}
        )
        self.assertEqual(second["state"], "completed")
        summary = self.service.household_summary("manager", "hh-1")
        self.assertEqual(summary["totals"]["consumed_mu"], "30.000")
        self.assertEqual(summary["totals"]["frozen_mu"], "0.000")
        kinds = [entry["kind"] for entry in self.service.household_entries("manager", "hh-1")["entries"]]
        self.assertEqual(kinds.count("consume"), 2)

    def test_close_returns_only_undelivered_quota(self) -> None:
        self.grant()
        self.submit()
        self.confirm()
        self.service.record_delivery("clerk", "app-1", {"delivery_id": "dlv-1", "year": 2026, "amount_mu": "7.562"})
        closed = self.service.close_application(
            "clerk", "app-1", {"outcome": "exited", "reason": "项目调整退出"}
        )
        self.assertEqual(closed["returned_mu"], "22.438")
        self.assertEqual(closed["consumed_mu"], "7.562")
        summary = self.service.household_summary("manager", "hh-1")
        self.assertEqual(summary["totals"]["available_mu"], "72.438")
        self.assertEqual(summary["totals"]["consumed_mu"], "7.562")
        self.assertEqual(summary["totals"]["frozen_mu"], "0.000")
        plot = self.connection.execute("SELECT * FROM plots WHERE plot_id='plot-1'").fetchone()
        self.assertEqual(plot["reserved_mu"], "7.562")
        entries = self.service.household_entries("manager", "hh-1")["entries"]
        returns = [entry for entry in entries if entry["kind"] == "return"]
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]["amount_mu"], "22.438")

    def test_failed_delivery_keeps_consumed_quota(self) -> None:
        self.grant()
        self.submit()
        self.confirm()
        self.service.record_delivery("clerk", "app-1", {"delivery_id": "dlv-1", "year": 2026, "amount_mu": "7.562"})
        closed = self.service.close_application(
            "clerk", "app-1", {"outcome": "failed", "reason": "地块验收未通过"}
        )
        self.assertEqual(closed["state"], "failed")
        self.assertEqual(closed["consumed_mu"], "7.562")

    def test_settlement_expires_entitlements_and_blocks_rule_backwrite(self) -> None:
        self.grant(entitlement_id="ent-home", source="homestead-eligibility",
                   categories=("homestead",), amount="10", valid_until="2026-12-31")
        self.clock.advance(days=112)  # 2027-01-16
        result = self.service.settle_year("manager", 2026)
        self.assertEqual(result["expired"], [{"entitlement_id": "ent-home", "expired_mu": "10.000"}])
        summary = self.service.household_summary("manager", "hh-1")
        self.assertEqual(summary["entitlements"][0]["state"], "expired")
        self.assertEqual(summary["totals"]["available_mu"], "0.000")
        with self.assertRaises(Conflict):
            self.service.publish_rule(
                "manager",
                {"effective_year": 2026, "selection_strategy": "source_priority", "review_window_hours": 48},
            )
        with self.assertRaises(Conflict):
            self.service.settle_year("manager", 2026)
        with self.assertRaises(ValidationFailed):
            self.service.settle_year("manager", 2027)

    def test_settled_year_blocks_new_occupancy_and_confirmation(self) -> None:
        self.grant(amount="80", valid_until="2028-12-31")
        self.submit(ends_on="2027-06-30")
        self.clock.advance(days=128)  # 2027-02-01
        self.service.settle_year("manager", 2026)
        with self.assertRaises(InvalidState):
            self.confirm()
        with self.assertRaises(InvalidState):
            self.submit(application_id="app-2", key="key-2", starts_on="2026-10-01", ends_on="2027-06-30")

    def test_rule_versions_must_move_forward(self) -> None:
        with self.assertRaises(Conflict):
            self.service.publish_rule(
                "manager",
                {"effective_year": 2026, "selection_strategy": "source_priority", "review_window_hours": 48},
            )
        published = self.service.publish_rule(
            "manager",
            {"effective_year": 2027, "selection_strategy": "source_priority", "review_window_hours": 48},
        )
        self.assertEqual(published["effective_year"], 2027)

    def test_explain_traces_deduction_return_and_overshoot(self) -> None:
        self.grant()
        self.submit()
        self.confirm()
        self.service.record_delivery("clerk", "app-1", {"delivery_id": "dlv-1", "year": 2026, "amount_mu": "7.562"})
        self.service.close_application("clerk", "app-1", {"outcome": "exited", "reason": "退出"})
        explained = self.service.explain_application("auditor", "app-1")
        kinds = [entry["kind"] for entry in explained["entries"]]
        self.assertEqual(kinds, ["freeze", "freeze", "consume", "return"])
        self.assertEqual(explained["lines"][0]["entitlement_id"], "ent-1")
        with self.assertRaises(Forbidden):
            self.service.explain_application("clerk", "app-1")
        entry_id = explained["entries"][0]["entry_id"]
        detail = self.service.explain_entry("manager", entry_id)
        self.assertEqual(detail["entry"]["kind"], "freeze")
        self.assertEqual(detail["rule"]["selection_strategy"], "earliest_expiry_first")
        self.assertEqual(detail["entitlement"]["entitlement_id"], "ent-1")

    def test_explain_shows_overshoot_decision(self) -> None:
        self.grant(entitlement_id="ent-li", household_id="hh-2", source="homestead-eligibility",
                   categories=("homestead",), amount="5", valid_until="2027-12-31")
        result = self.submit(application_id="app-li", household_id="hh-2", category="homestead",
                             amount="8", ends_on="2027-06-30", key="key-li")
        self.service.decide_exception("reviewer", result["exception"]["exception_id"],
                                      {"approve": True, "reason": "情况属实"})
        explained = self.service.explain_application("manager", "app-li")
        self.assertEqual(explained["exception"]["state"], "approved")
        self.assertEqual(explained["exception"]["decided_by"], "reviewer")
        self.assertEqual(explained["exception"]["submitted_by"], "clerk")
        grant_entries = [entry for entry in explained["entries"] if entry["kind"] == "grant"]
        self.assertEqual(grant_entries[0]["exception_id"], "exc-app-li")

    def test_audit_chain_detects_tampering(self) -> None:
        self.grant()
        self.assertTrue(self.service.audit_chain("auditor")["valid"])
        self.connection.execute("UPDATE audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("auditor")["valid"])

    def test_api_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("GET", "/households/hh-1/summary")
        self.assertEqual(missing_actor.status, 422)
        self.grant()
        own = app.handle("GET", "/households/hh-1/summary", {"X-Actor-Id": "fam-1"})
        self.assertEqual(own.status, 200)
        other = app.handle("GET", "/households/hh-2/summary", {"X-Actor-Id": "fam-1"})
        self.assertEqual(other.status, 403)
        self.assertEqual(other.body["error"]["code"], "forbidden")
        unknown = app.handle("GET", "/no-such-route", {"X-Actor-Id": "fam-1"})
        self.assertEqual(unknown.status, 404)


if __name__ == "__main__":
    unittest.main()
