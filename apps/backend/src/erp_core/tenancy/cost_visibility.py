from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from erp_core.shared.enums import UserRole

SENSITIVE_COST_FIELDS: tuple[str, ...] = (
    "cost_price_base",
    "cogs_base",
    "net_profit_base",
    "net_margin_pct",
)


def can_view_cost_and_profit(role: UserRole) -> bool:
    """Agent role cannot view purchase cost, COGS, or contribution net profit."""
    return role in {
        UserRole.OWNER,
        UserRole.ADMIN,
        UserRole.MANAGER,
        UserRole.FINANCE,
    }


def mask_cost_fields(
    payload: dict[str, Any],
    role: UserRole,
    sensitive_keys: tuple[str, ...] = SENSITIVE_COST_FIELDS,
) -> dict[str, Any]:
    if can_view_cost_and_profit(role):
        return {**payload, "cost_masked": False}
    masked = dict(payload)
    for key in sensitive_keys:
        if key in masked:
            masked[key] = None
    masked["cost_masked"] = True
    return masked


def apply_cost_visibility[TModel: BaseModel](model: TModel, role: UserRole) -> TModel:
    updates: dict[str, Any] = {}
    if can_view_cost_and_profit(role):
        if hasattr(model, "cost_masked"):
            updates["cost_masked"] = False
        return model.model_copy(update=updates)
    for field_name in SENSITIVE_COST_FIELDS:
        if hasattr(model, field_name):
            updates[field_name] = None
    if hasattr(model, "cost_masked"):
        updates["cost_masked"] = True
    return model.model_copy(update=updates)
