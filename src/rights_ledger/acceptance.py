"""贯通权益授予、申请冻结、分配确认、交付、退出、结算与复核的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RightsLedgerService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
    service = RightsLedgerService(connection, clock)
    service.create_user("clerk", "经办员", "clerk")
    service.create_user("manager", "产权管理", "manager")
    service.create_user("reviewer", "复核员", "reviewer")
    service.create_user("auditor", "审计员", "auditor")
    service.register_household("clerk", "household-east", "陈东户", "东岗村")
    service.create_user("hh-east", "陈东", "household", household_id="household-east")
    service.register_plot("manager", "plot-h1", "东岗村", "homestead", "2.0")
    service.register_plot("manager", "plot-h2", "东岗村", "homestead", "1.5")
    service.create_rule("manager", {"rule_id": "rule-2026", "effective_year": 2026, "selection_policy": "expiry_first", "source_priority": ["contracted-land-merge", "homestead-eligibility", "relocation-bonus"], "review_deadline_days": 30})
    service.grant_entitlement("manager", {"grant_id": "g-contract", "household_id": "household-east", "source": "contracted-land-merge", "category": "cultivated-land", "quantity_mu": "4.0", "effective_from": "2026-01-01", "expires_at": "2028-12-31", "reason": "家庭承包地合并"})
    service.grant_entitlement("manager", {"grant_id": "g-home", "household_id": "household-east", "source": "homestead-eligibility", "category": "homestead", "quantity_mu": "1.2", "effective_from": "2026-01-01", "expires_at": "2026-12-31", "reason": "宅基地资格"})
    service.grant_entitlement("manager", {"grant_id": "g-bonus", "household_id": "household-east", "source": "relocation-bonus", "category": "homestead", "quantity_mu": "0.8", "effective_from": "2026-01-01", "expires_at": "2027-12-31", "reason": "搬迁奖励"})
    first = service.submit_application("clerk", {"application_id": "app-1", "household_id": "household-east", "plot_id": "plot-h1", "category": "homestead", "requested_mu": "1.5", "project_start": "2026-10-01", "project_end": "2028-03-31", "idempotency_key": "app-key-1"})
    confirmed = service.confirm_allocation("manager", "app-1")
    service.record_delivery("manager", {"delivery_id": "dlv-1", "reservation_id": "rsv-app-1", "year": 2026, "amount_mu": "0.5", "idempotency_key": "dlv-key-1"})
    clock.advance(days=111)  # 2027-01-15
    settled = service.settle_year("manager", 2026)
    over = service.submit_application("clerk", {"application_id": "app-2", "household_id": "household-east", "plot_id": "plot-h2", "category": "homestead", "requested_mu": "2.0", "project_start": "2027-02-01", "project_end": "2028-06-30", "idempotency_key": "app-key-2"})
    service.review_exception("reviewer", over["exception_id"], "approve", "搬迁过渡特殊困难，同意超额")
    service.confirm_allocation("manager", "app-2")
    exited = service.exit_reservation("manager", "rsv-app-1", "家庭退出安置", failed=False)
    summary = service.household_summary("hh-east", "household-east")
    entries = service.household_entries("hh-east", "household-east")
    explained = service.explain_exception("auditor", over["exception_id"])
    result = {
        "status": "ok",
        "first_application": first,
        "confirmed_years": confirmed["years"],
        "settled": settled,
        "over_application": over,
        "exited": exited,
        "household_summary": summary,
        "household_visible_entries": len(entries["entries"]),
        "exception_explain_state": explained["exception"]["state"],
        "audit": service.audit_chain("auditor"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行权益额度账本服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
