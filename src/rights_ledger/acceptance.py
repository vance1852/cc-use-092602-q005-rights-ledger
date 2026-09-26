"""贯通权益授予、跨年度申请、原子落账、交付、退出、复核与结算的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import LedgerError
from .service import RightsLedgerService


def _probe(service_call, *args) -> str:
    try:
        service_call(*args)
    except LedgerError as exc:
        return exc.code
    return "unexpected-ok"


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
    service = RightsLedgerService(connection, clock)
    for user_id, role in (
        ("clerk", "clerk"),
        ("manager", "manager"),
        ("reviewer", "reviewer"),
        ("reviewer2", "reviewer"),
        ("auditor", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    service.register_household("clerk", {"household_id": "hh-zhang", "name": "张家庄户", "village": "北部示范村", "member_count": 4})
    service.register_household("clerk", {"household_id": "hh-li", "name": "李家台户", "village": "北部示范村", "member_count": 3})
    service.create_user("zhang-user", "张家户主", "household", "hh-zhang")
    service.publish_rule("manager", {"effective_year": 2026, "selection_strategy": "earliest_expiry_first", "review_window_hours": 72, "max_overshoot_percent": "50"})
    service.register_plot("clerk", {"plot_id": "plot-cultivated-01", "village": "北部示范村", "land_category": "cultivated-land", "area_mu": "100"})
    service.register_plot("clerk", {"plot_id": "plot-homestead-01", "village": "北部示范村", "land_category": "homestead", "area_mu": "20"})
    service.grant_entitlement("manager", {"entitlement_id": "ent-contract-zhang", "household_id": "hh-zhang", "source": "contract-merge", "land_categories": ["cultivated-land"], "granted_mu": "80", "valid_from": "2026-01-01", "valid_until": "2028-12-31"})
    service.grant_entitlement("manager", {"entitlement_id": "ent-move-zhang", "household_id": "hh-zhang", "source": "relocation-reward", "land_categories": ["cultivated-land", "homestead"], "granted_mu": "15", "valid_from": "2026-06-01", "valid_until": "2027-12-31"})
    service.grant_entitlement("manager", {"entitlement_id": "ent-home-zhang", "household_id": "hh-zhang", "source": "homestead-eligibility", "land_categories": ["homestead"], "granted_mu": "10", "valid_from": "2026-01-01", "valid_until": "2026-12-31"})
    submitted = service.submit_application("clerk", {"application_id": "app-zhang-1", "household_id": "hh-zhang", "land_category": "cultivated-land", "requested_mu": "30", "starts_on": "2026-10-01", "ends_on": "2027-09-30", "idempotency_key": "app-zhang-key-1"})
    confirmed = service.confirm_allocation("clerk", "app-zhang-1", {"reservation_id": "rsv-zhang-1", "plot_id": "plot-cultivated-01", "expected_revision": 1})
    delivered = service.record_delivery("clerk", "app-zhang-1", {"delivery_id": "dlv-zhang-1", "year": 2026, "amount_mu": "7.562"})
    closed = service.close_application("clerk", "app-zhang-1", {"outcome": "exited", "reason": "项目调整，未交付部分退出"})
    service.grant_entitlement("manager", {"entitlement_id": "ent-home-li", "household_id": "hh-li", "source": "homestead-eligibility", "land_categories": ["homestead"], "granted_mu": "5", "valid_from": "2026-01-01", "valid_until": "2027-12-31"})
    overshoot = service.submit_application("clerk", {"application_id": "app-li-1", "household_id": "hh-li", "land_category": "homestead", "requested_mu": "8", "starts_on": "2026-10-01", "ends_on": "2027-06-30", "idempotency_key": "app-li-key-1"})
    decided = service.decide_exception("reviewer", "exc-app-li-1", {"approve": True, "reason": "搬迁安置过渡期确有困难"})
    confirmed_li = service.confirm_allocation("clerk", "app-li-1", {"reservation_id": "rsv-li-1", "plot_id": "plot-homestead-01", "expected_revision": 2})
    summary = service.household_summary("zhang-user", "hh-zhang")
    entries = service.household_entries("zhang-user", "hh-zhang")
    checks = {
        "household_other_summary": _probe(service.household_summary, "zhang-user", "hh-li"),
        "clerk_explain": _probe(service.explain_application, "clerk", "app-zhang-1"),
    }
    clock.advance(days=112)  # 2027-01-15
    settlement = service.settle_year("manager", 2026)
    checks["rule_backwrite_settled_year"] = _probe(
        service.publish_rule,
        "manager",
        {"effective_year": 2026, "selection_strategy": "source_priority", "review_window_hours": 48, "max_overshoot_percent": "30"},
    )
    rule_v2 = service.publish_rule("manager", {"effective_year": 2027, "selection_strategy": "source_priority", "review_window_hours": 48, "max_overshoot_percent": "30"})
    explanation = service.explain_application("auditor", "app-zhang-1")
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "submitted_slices": submitted["slices"],
        "confirmed_lines": confirmed["lines"],
        "delivered": delivered,
        "closed": closed,
        "overshoot_exception": overshoot["exception"],
        "decision": decided,
        "confirmed_li_lines": confirmed_li["lines"],
        "household_totals": summary["totals"],
        "household_entries": len(entries["entries"]),
        "checks": checks,
        "settlement": settlement,
        "rule_v2": rule_v2,
        "explanation_entries": [entry["kind"] for entry in explanation["entries"]],
        "audit": service.audit_chain("auditor"),
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行家庭土地权益额度账本离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
