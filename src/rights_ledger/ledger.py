"""额度选择、年度切分等确定性纯函数。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
MU_PLACES = Decimal("0.001")


def quantize_mu(value: Decimal) -> Decimal:
    return value.quantize(MU_PLACES, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


SELECTION_POLICIES = {"expiry_first", "source_priority"}


@dataclass(frozen=True, slots=True)
class QuotaCandidate:
    """参与额度选择的权益来源批次视图。"""

    grant_id: str
    source: str
    available_mu: Decimal
    effective_from: str
    expires_at: str | None


@dataclass(frozen=True, slots=True)
class QuotaTake:
    grant_id: str
    amount_mu: Decimal


@dataclass(frozen=True, slots=True)
class QuotaSelection:
    takes: tuple[QuotaTake, ...]
    shortfall_mu: Decimal


def _expiry_sort_key(candidate: QuotaCandidate) -> tuple[str, str, str]:
    # 无到期日的批次排在最后，先到期先使用，避免权益过期浪费。
    return (
        candidate.expires_at if candidate.expires_at is not None else "9999-12-31",
        candidate.effective_from,
        candidate.grant_id,
    )


def order_candidates(
    candidates: Iterable[QuotaCandidate],
    policy: str,
    source_priority: Sequence[str],
) -> list[QuotaCandidate]:
    if policy not in SELECTION_POLICIES:
        raise ValueError("未知的额度选择策略")
    usable = [item for item in candidates if item.available_mu > ZERO]
    if policy == "expiry_first":
        return sorted(usable, key=_expiry_sort_key)
    rank = {source: index for index, source in enumerate(source_priority)}
    return sorted(
        usable,
        key=lambda item: (
            rank.get(item.source, len(rank)),
            _expiry_sort_key(item),
        ),
    )


def select_quota(
    candidates: Iterable[QuotaCandidate],
    amount_mu: Decimal,
    policy: str,
    source_priority: Sequence[str],
) -> QuotaSelection:
    """按规则从可用批次中贪心选择额度，返回占用明细与缺口。"""
    if amount_mu <= ZERO:
        raise ValueError("选择额度必须为正数")
    takes: list[QuotaTake] = []
    remaining = quantize_mu(amount_mu)
    for candidate in order_candidates(candidates, policy, source_priority):
        if remaining == ZERO:
            break
        take = min(remaining, quantize_mu(candidate.available_mu))
        takes.append(QuotaTake(candidate.grant_id, take))
        remaining = quantize_mu(remaining - take)
    return QuotaSelection(tuple(takes), remaining)


@dataclass(frozen=True, slots=True)
class YearSlice:
    year: int
    planned_mu: Decimal


def split_yearly_occupancy(start: date, end: date, total_mu: Decimal) -> list[YearSlice]:
    """把跨年度项目的预计占用按自然年天数比例切分，末段承担舍入差额，合计恒等于总量。"""
    if total_mu <= ZERO:
        raise ValueError("预计占用必须为正数")
    if end < start:
        raise ValueError("项目结束日期不能早于开始日期")
    total_days = (end - start).days + 1
    slices: list[YearSlice] = []
    remaining = quantize_mu(total_mu)
    for year in range(start.year, end.year + 1):
        segment_start = max(start, date(year, 1, 1))
        segment_end = min(end, date(year, 12, 31))
        days = (segment_end - segment_start).days + 1
        if year == end.year:
            planned = remaining
        else:
            planned = quantize_mu(total_mu * Decimal(days) / Decimal(total_days))
            remaining = quantize_mu(remaining - planned)
        slices.append(YearSlice(year, planned))
    return slices


def summarize_balances(grants: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """按户汇总授予、消耗、冻结、过期与可用额度。"""
    granted = consumed = frozen = expired = ZERO
    for row in grants:
        granted += Decimal(str(row["quantity_mu"]))
        consumed += Decimal(str(row["consumed_mu"]))
        frozen += Decimal(str(row["frozen_mu"]))
        expired += Decimal(str(row["expired_mu"]))
    available = granted - consumed - frozen - expired
    return {
        "granted_mu": decimal_text(quantize_mu(granted)),
        "consumed_mu": decimal_text(quantize_mu(consumed)),
        "frozen_mu": decimal_text(quantize_mu(frozen)),
        "expired_mu": decimal_text(quantize_mu(expired)),
        "available_mu": decimal_text(quantize_mu(available)),
    }
