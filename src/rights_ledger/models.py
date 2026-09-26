"""家庭土地权益额度账本的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed
from .quota import DEFAULT_SOURCE_PRIORITY, SELECTION_STRATEGIES


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

LAND_CATEGORIES = {
    "cultivated-land",
    "homestead",
    "resettlement-home",
    "facility-land",
    "forest-land",
    "reserve-land",
}

ENTITLEMENT_SOURCES = {
    "contract-merge",
    "homestead-eligibility",
    "relocation-reward",
    "policy-adjustment",
    "exception-grant",
}
MANUAL_SOURCES = ENTITLEMENT_SOURCES - {"exception-grant"}

CLOSURE_OUTCOMES = {"exited", "failed"}


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


def integer_value(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationFailed(f"{field} 必须是 {minimum} 到 {maximum} 的整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def land_category(value: object, field: str = "land_category") -> str:
    result = required_text(value, field, 32)
    if result not in LAND_CATEGORIES:
        raise ValidationFailed(f"{field} 不是受支持的地类")
    return result


def land_category_list(value: object, field: str = "land_categories") -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str) or not value:
        raise ValidationFailed(f"{field} 必须是非空地类数组")
    result: list[str] = []
    for item in value:
        category = land_category(item, field)
        if category in result:
            raise ValidationFailed(f"{field} 存在重复地类")
        result.append(category)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class HouseholdRegistration:
    household_id: str
    name: str
    village: str
    member_count: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HouseholdRegistration":
        return cls(
            household_id=identifier(raw.get("household_id"), "household_id"),
            name=required_text(raw.get("name"), "name"),
            village=required_text(raw.get("village"), "village"),
            member_count=integer_value(raw.get("member_count", 1), "member_count", minimum=1, maximum=99),
        )


@dataclass(frozen=True, slots=True)
class PlotRegistration:
    plot_id: str
    village: str
    land_category: str
    area_mu: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlotRegistration":
        return cls(
            plot_id=identifier(raw.get("plot_id"), "plot_id"),
            village=required_text(raw.get("village"), "village"),
            land_category=land_category(raw.get("land_category")),
            area_mu=decimal_value(raw.get("area_mu"), "area_mu", minimum=Decimal("0.001")),
        )


@dataclass(frozen=True, slots=True)
class RulePublication:
    effective_year: int
    selection_strategy: str
    review_window_hours: int
    max_overshoot_percent: Decimal
    source_priority: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RulePublication":
        strategy = required_text(raw.get("selection_strategy"), "selection_strategy", 32)
        if strategy not in SELECTION_STRATEGIES:
            raise ValidationFailed("selection_strategy 不是受支持的额度选择策略")
        priority_raw = raw.get("source_priority", list(DEFAULT_SOURCE_PRIORITY))
        if not isinstance(priority_raw, Sequence) or isinstance(priority_raw, str) or not priority_raw:
            raise ValidationFailed("source_priority 必须是非空来源数组")
        priority: list[str] = []
        for item in priority_raw:
            source = required_text(item, "source_priority", 32)
            if source not in ENTITLEMENT_SOURCES:
                raise ValidationFailed("source_priority 包含未知权益来源")
            if source in priority:
                raise ValidationFailed("source_priority 存在重复来源")
            priority.append(source)
        return cls(
            effective_year=integer_value(raw.get("effective_year"), "effective_year", minimum=2000, maximum=2100),
            selection_strategy=strategy,
            review_window_hours=integer_value(
                raw.get("review_window_hours"), "review_window_hours", minimum=1, maximum=720
            ),
            max_overshoot_percent=decimal_value(
                raw.get("max_overshoot_percent", "50"),
                "max_overshoot_percent",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
            source_priority=tuple(priority),
        )


@dataclass(frozen=True, slots=True)
class EntitlementGrant:
    entitlement_id: str
    household_id: str
    source: str
    land_categories: tuple[str, ...]
    granted_mu: Decimal
    valid_from: str
    valid_until: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EntitlementGrant":
        source = required_text(raw.get("source"), "source", 32)
        if source not in MANUAL_SOURCES:
            raise ValidationFailed("source 不是可手工登记的权益来源")
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_until = date_text(raw.get("valid_until"), "valid_until")
        if valid_until < valid_from:
            raise ValidationFailed("valid_until 不能早于 valid_from")
        return cls(
            entitlement_id=identifier(raw.get("entitlement_id"), "entitlement_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            source=source,
            land_categories=land_category_list(raw.get("land_categories")),
            granted_mu=decimal_value(raw.get("granted_mu"), "granted_mu", minimum=Decimal("0.001")),
            valid_from=valid_from,
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class ApplicationRequest:
    application_id: str
    household_id: str
    land_category: str
    requested_mu: Decimal
    starts_on: str
    ends_on: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApplicationRequest":
        starts_on = date_text(raw.get("starts_on"), "starts_on")
        ends_on = date_text(raw.get("ends_on"), "ends_on")
        if ends_on < starts_on:
            raise ValidationFailed("ends_on 不能早于 starts_on")
        return cls(
            application_id=identifier(raw.get("application_id"), "application_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            land_category=land_category(raw.get("land_category")),
            requested_mu=decimal_value(raw.get("requested_mu"), "requested_mu", minimum=Decimal("0.001")),
            starts_on=starts_on,
            ends_on=ends_on,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ExceptionDecision:
    approve: bool
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExceptionDecision":
        approve = raw.get("approve")
        if not isinstance(approve, bool):
            raise ValidationFailed("approve 必须是布尔值")
        return cls(approve=approve, reason=required_text(raw.get("reason"), "reason"))


@dataclass(frozen=True, slots=True)
class AllocationConfirmation:
    reservation_id: str
    plot_id: str
    expected_revision: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AllocationConfirmation":
        return cls(
            reservation_id=identifier(raw.get("reservation_id"), "reservation_id"),
            plot_id=identifier(raw.get("plot_id"), "plot_id"),
            expected_revision=integer_value(
                raw.get("expected_revision"), "expected_revision", minimum=1, maximum=1_000_000
            ),
        )


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: str
    year: int
    amount_mu: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeliveryRecord":
        return cls(
            delivery_id=identifier(raw.get("delivery_id"), "delivery_id"),
            year=integer_value(raw.get("year"), "year", minimum=2000, maximum=2100),
            amount_mu=decimal_value(raw.get("amount_mu"), "amount_mu", minimum=Decimal("0.001")),
        )


@dataclass(frozen=True, slots=True)
class ApplicationClosure:
    outcome: str
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApplicationClosure":
        outcome = required_text(raw.get("outcome"), "outcome", 16)
        if outcome not in CLOSURE_OUTCOMES:
            raise ValidationFailed("outcome 必须是 exited 或 failed")
        return cls(outcome=outcome, reason=required_text(raw.get("reason"), "reason"))
