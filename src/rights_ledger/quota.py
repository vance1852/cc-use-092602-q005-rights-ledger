"""家庭土地权益额度的确定性计算：跨年度切分、额度选择与余额。

本模块不访问数据库，所有函数对相同输入返回相同结果，便于在
服务层事务内重放并写入可审计的流水。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Sequence


ZERO = Decimal("0")
MU_QUANTUM = Decimal("0.001")
HUNDRED = Decimal("100")

SELECTION_STRATEGIES = {"earliest_expiry_first", "source_priority"}
DEFAULT_SOURCE_PRIORITY = (
    "contract-merge",
    "homestead-eligibility",
    "relocation-reward",
    "policy-adjustment",
    "exception-grant",
)


def quantize_mu(value: Decimal) -> Decimal:
    return value.quantize(MU_QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def available_mu(granted: Decimal, frozen: Decimal, consumed: Decimal, expired: Decimal) -> Decimal:
    """权益当前可用余额：授予减冻结、实际消耗与已核销。"""
    return granted - frozen - consumed - expired


@dataclass(frozen=True, slots=True)
class YearSlice:
    """一个日历年度内的预计占用。"""

    year: int
    segment_start: date
    segment_end: date
    days: int
    planned_mu: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "year": self.year,
            "segment_start": self.segment_start.isoformat(),
            "segment_end": self.segment_end.isoformat(),
            "days": self.days,
            "planned_mu": decimal_text(self.planned_mu),
        }


def split_estimated_occupancy(
    starts_on: date,
    ends_on: date,
    total_mu: Decimal,
    today: date,
) -> list[YearSlice]:
    """把项目预计占用按日历年度切分。

    每个年度切片按项目在该年度内的天数占比分摊总量，并用最大余数法
    保证各切片之和精确等于总量。``today`` 来自可注入时钟，用于拒绝
    已经完全结束的项目，使跨年度切分在测试中可复现。
    """
    if total_mu <= ZERO:
        raise ValueError("预计占用总量必须为正数")
    if ends_on < starts_on:
        raise ValueError("结束日期不能早于开始日期")
    if ends_on < today:
        raise ValueError("项目期间已经结束")
    total_mu = quantize_mu(total_mu)
    segments: list[tuple[int, date, date, int]] = []
    cursor = starts_on
    while cursor <= ends_on:
        segment_end = min(date(cursor.year, 12, 31), ends_on)
        segments.append((cursor.year, cursor, segment_end, (segment_end - cursor).days + 1))
        cursor = segment_end + timedelta(days=1)
    total_days = sum(item[3] for item in segments)
    exact = [total_mu * Decimal(days) / Decimal(total_days) for _, _, _, days in segments]
    floors = [value.quantize(MU_QUANTUM, rounding=ROUND_DOWN) for value in exact]
    remainder_units = int((total_mu - sum(floors, ZERO)) / MU_QUANTUM)
    order = sorted(
        range(len(segments)),
        key=lambda index: (-(exact[index] - floors[index]), segments[index][0]),
    )
    for index in order[:remainder_units]:
        floors[index] += MU_QUANTUM
    return [
        YearSlice(
            year=segments[index][0],
            segment_start=segments[index][1],
            segment_end=segments[index][2],
            days=segments[index][3],
            planned_mu=floors[index],
        )
        for index in range(len(segments))
    ]


@dataclass(frozen=True, slots=True)
class EntitlementView:
    """参与额度选择的权益快照。"""

    entitlement_id: str
    source: str
    land_categories: frozenset[str]
    available_mu: Decimal
    valid_from: date
    valid_until: date


@dataclass(frozen=True, slots=True)
class SelectionLine:
    entitlement_id: str
    year: int
    amount_mu: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "entitlement_id": self.entitlement_id,
            "year": self.year,
            "amount_mu": decimal_text(self.amount_mu),
        }


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    lines: tuple[SelectionLine, ...]
    covered_mu: Decimal
    shortfall_mu: Decimal


def select_quota(
    entitlements: Sequence[EntitlementView],
    slices: Sequence[YearSlice],
    land_category: str,
    strategy: str,
    source_priority: Sequence[str],
    today: date,
) -> SelectionPlan:
    """按规则为每个年度切片选择可用额度。

    候选权益必须：适用地类包含申请地类、剩余可用为正、有效期完整
    覆盖该切片的项目区间且当前未过期。``earliest_expiry_first``
    先消耗最早到期的权益；``source_priority`` 先按权益来源优先级、
    再按到期日排序。同一权益可跨年切片连续消耗，函数返回逐切片
    选择结果与总缺口。
    """
    if strategy not in SELECTION_STRATEGIES:
        raise ValueError("未知的额度选择策略")
    remaining = {item.entitlement_id: item.available_mu for item in entitlements}
    priority_index = {source: index for index, source in enumerate(source_priority)}
    lines: list[SelectionLine] = []
    shortfall = ZERO
    for year_slice in slices:
        candidates = [
            item
            for item in entitlements
            if land_category in item.land_categories
            and remaining[item.entitlement_id] > ZERO
            and item.valid_from <= year_slice.segment_start
            and item.valid_until >= year_slice.segment_end
            and item.valid_until >= today
        ]
        if strategy == "source_priority":
            candidates.sort(
                key=lambda item: (
                    priority_index.get(item.source, len(priority_index)),
                    item.valid_until,
                    item.entitlement_id,
                )
            )
        else:
            candidates.sort(key=lambda item: (item.valid_until, item.entitlement_id))
        need = year_slice.planned_mu
        for candidate in candidates:
            if need <= ZERO:
                break
            take = min(need, remaining[candidate.entitlement_id])
            remaining[candidate.entitlement_id] -= take
            need -= take
            lines.append(SelectionLine(candidate.entitlement_id, year_slice.year, take))
        shortfall += need
    covered = sum((line.amount_mu for line in lines), ZERO)
    return SelectionPlan(lines=tuple(lines), covered_mu=covered, shortfall_mu=shortfall)
