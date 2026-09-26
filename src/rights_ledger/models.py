"""家庭土地权益额度账本的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed
from .ledger import SELECTION_POLICIES


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
RIGHTS_SOURCES = {
    "contracted-land-merge",
    "homestead-eligibility",
    "relocation-bonus",
    "exception-approval",
}
GRANTABLE_SOURCES = RIGHTS_SOURCES - {"exception-approval"}
LAND_CATEGORIES = {"cultivated-land", "homestead", "resettlement-home"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def land_category(value: object, field: str = "category") -> str:
    result = required_text(value, field, 32)
    if result not in LAND_CATEGORIES:
        raise ValidationFailed(f"{field} 不是受支持的地类")
    return result


@dataclass(frozen=True, slots=True)
class GrantInput:
    grant_id: str
    household_id: str
    source: str
    category: str
    quantity_mu: Decimal
    effective_from: str
    expires_at: str | None
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GrantInput":
        source = required_text(raw.get("source"), "source", 40)
        if source not in GRANTABLE_SOURCES:
            raise ValidationFailed("source 必须是 contracted-land-merge、homestead-eligibility 或 relocation-bonus")
        effective_from = date_text(raw.get("effective_from"), "effective_from")
        expires_raw = raw.get("expires_at")
        expires_at = None if expires_raw in (None, "") else date_text(expires_raw, "expires_at")
        if expires_at is not None and expires_at < effective_from:
            raise ValidationFailed("expires_at 不能早于 effective_from")
        return cls(
            grant_id=identifier(raw.get("grant_id"), "grant_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            source=source,
            category=land_category(raw.get("category")),
            quantity_mu=decimal_value(raw.get("quantity_mu"), "quantity_mu", minimum=Decimal("0.001")),
            effective_from=effective_from,
            expires_at=expires_at,
            reason=required_text(raw.get("reason"), "reason"),
        )


@dataclass(frozen=True, slots=True)
class ApplicationInput:
    application_id: str
    household_id: str
    plot_id: str
    category: str
    requested_mu: Decimal
    project_start: str
    project_end: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApplicationInput":
        project_start = date_text(raw.get("project_start"), "project_start")
        project_end = date_text(raw.get("project_end"), "project_end")
        if project_end < project_start:
            raise ValidationFailed("project_end 不能早于 project_start")
        return cls(
            application_id=identifier(raw.get("application_id"), "application_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            plot_id=identifier(raw.get("plot_id"), "plot_id"),
            category=land_category(raw.get("category")),
            requested_mu=decimal_value(raw.get("requested_mu"), "requested_mu", minimum=Decimal("0.001")),
            project_start=project_start,
            project_end=project_end,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class RuleInput:
    rule_id: str
    effective_year: int
    selection_policy: str
    source_priority: Sequence[str]
    review_deadline_days: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleInput":
        year = raw.get("effective_year")
        if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
            raise ValidationFailed("effective_year 必须是 2000 到 2100 的整数年份")
        policy = required_text(raw.get("selection_policy"), "selection_policy", 32)
        if policy not in SELECTION_POLICIES:
            raise ValidationFailed("selection_policy 必须是 expiry_first 或 source_priority")
        priority_raw = raw.get("source_priority", [])
        if not isinstance(priority_raw, Sequence) or isinstance(priority_raw, str):
            raise ValidationFailed("source_priority 必须是权益来源数组")
        priority: list[str] = []
        for item in priority_raw:
            source = required_text(item, "source_priority 元素", 40)
            if source not in RIGHTS_SOURCES:
                raise ValidationFailed(f"source_priority 包含未知来源 {source}")
            if source in priority:
                raise ValidationFailed("source_priority 不能重复")
            priority.append(source)
        days = raw.get("review_deadline_days")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValidationFailed("review_deadline_days 必须是 1 到 365 的整数")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            effective_year=year,
            selection_policy=policy,
            source_priority=tuple(priority),
            review_deadline_days=days,
        )


@dataclass(frozen=True, slots=True)
class DeliveryInput:
    delivery_id: str
    reservation_id: str
    year: int
    amount_mu: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeliveryInput":
        year = raw.get("year")
        if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
            raise ValidationFailed("year 必须是 2000 到 2100 的整数年份")
        return cls(
            delivery_id=identifier(raw.get("delivery_id"), "delivery_id"),
            reservation_id=identifier(raw.get("reservation_id"), "reservation_id"),
            year=year,
            amount_mu=decimal_value(raw.get("amount_mu"), "amount_mu", minimum=Decimal("0.001")),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
