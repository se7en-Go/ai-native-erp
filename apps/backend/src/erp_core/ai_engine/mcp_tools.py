"""Static catalog of the ERP tools exposed over MCP Streamable HTTP.

The catalog is deliberately data-only: it declares each tool's JSON Schema, the
OAuth-style scope it needs (`mcp:read` for deterministic SQL reads, `mcp:propose`
for human-reviewable proposal drafts) and the autonomy level the tool runs at.
Dispatch lives in :mod:`erp_core.ai_engine.mcp_server`.

Everything below is read-only or draft-only. Financial settlement, inventory
mutation, shipment creation and channel publishing are **permanently absent**:
they are listed in :data:`BLOCKED_L0_TOOLS` so the operator console can show the
red line instead of silently omitting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_SERVER_NAME = "ai-native-erp-mcp"
MCP_SERVER_VERSION = "1.0.0"

MCP_SCOPE_READ = "mcp:read"
MCP_SCOPE_PROPOSE = "mcp:propose"
MCP_VALID_SCOPES: frozenset[str] = frozenset({MCP_SCOPE_READ, MCP_SCOPE_PROPOSE})

_READ_AUTONOMY = "L2_auto"
_DRAFT_AUTONOMY = "L1_proposal"

_INCOTERM_VALUES: tuple[str, ...] = ("EXW", "FOB", "CIF", "DDP", "DAP")

_SKU_PRICING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sku": {"type": "string", "minLength": 1, "maxLength": 64},
        "qty": {"type": "integer", "minimum": 1, "default": 1},
        "incoterm": {"type": "string", "enum": list(_INCOTERM_VALUES), "default": "FOB"},
    },
    "required": ["sku"],
    "additionalProperties": False,
}

_ORDER_TRACKING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order_no": {"type": "string", "minLength": 1, "maxLength": 64},
        "customer_id": {"type": "string", "format": "uuid"},
    },
    "required": ["order_no"],
    "additionalProperties": False,
}

_QUOTE_MATRIX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "minLength": 1, "maxLength": 64},
                    "qty": {"type": "integer", "minimum": 1},
                    "requested_unit_price": {
                        "type": "string",
                        "description": "Exact decimal string; never an IEEE-754 float.",
                    },
                },
                "required": ["sku"],
                "additionalProperties": False,
            },
        },
        "currency": {"type": "string", "pattern": "^[A-Za-z]{3}$", "default": "USD"},
        "incoterm": {"type": "string", "enum": list(_INCOTERM_VALUES), "default": "FOB"},
    },
    "required": ["items", "currency"],
    "additionalProperties": False,
}

_RECEIVABLES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "amount": {
            "type": "string",
            "description": "Exact decimal string for the received amount.",
        },
        "currency": {"type": "string", "pattern": "^[A-Za-z]{3}$"},
        "customer_id": {"type": "string", "format": "uuid"},
        "reference_hint": {"type": "string", "maxLength": 256},
    },
    "required": ["amount", "currency"],
    "additionalProperties": False,
}

_CHANNEL_ANALYTICS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "window": {"type": "string", "enum": ["7d", "30d", "90d"], "default": "30d"},
        "channel_types": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 40},
        },
    },
    "additionalProperties": False,
}

_ANALYTICS_SQL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2048,
            "description": "Minimal SELECT grammar over the three approved analytics views.",
        }
    },
    "required": ["sql"],
    "additionalProperties": False,
}

_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "capability": {
            "type": "string",
            "enum": [
                "cs_support",
                "sales_quoting",
                "order_logistics",
                "content_studio",
                "biz_analytics",
                "finance_recon",
            ],
        },
        "proposal_type": {
            "type": "string",
            "enum": [
                "reply_draft",
                "crm_entity_extract",
                "quote_draft",
                "follow_up_suggestion",
                "identity_merge",
                "refund_or_reship",
                "payment_match",
            ],
        },
        "proposed_payload": {"type": "object"},
        "citations": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "object"},
        },
        "target_entity_type": {"type": "string", "maxLength": 64},
        "target_entity_id": {"type": "string", "format": "uuid"},
        "source_tool_call_id": {
            "type": "string",
            "maxLength": 40,
            "description": (
                "A tool_call_id (tc_<uuid> or <uuid>) returned by an earlier mcp:read call."
            ),
        },
        "confidence_score": {"type": "string", "description": "0..1 exact decimal string."},
        "estimated_margin_pct": {"type": "string", "description": "Exact decimal string."},
    },
    "required": ["capability", "proposal_type", "proposed_payload"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    """One externally callable tool: schema, required scope and autonomy ceiling."""

    name: str
    required_scope: str
    autonomy_level: str
    description: str
    input_schema: dict[str, Any]


MCP_TOOL_SPECS: tuple[McpToolSpec, ...] = (
    McpToolSpec(
        name="get_live_sku_pricing_and_stock",
        required_scope=MCP_SCOPE_READ,
        autonomy_level=_READ_AUTONOMY,
        description=(
            "Deterministic live SKU price, MOQ tier, Incoterm surcharge and available "
            "stock from the ERP SQL registry."
        ),
        input_schema=_SKU_PRICING_SCHEMA,
    ),
    McpToolSpec(
        name="get_live_order_tracking",
        required_scope=MCP_SCOPE_READ,
        autonomy_level=_READ_AUTONOMY,
        description=(
            "Live order status, payment status and 17TRACK shipment events for one order."
        ),
        input_schema=_ORDER_TRACKING_SCHEMA,
    ),
    McpToolSpec(
        name="calc_b2b_quote_matrix",
        required_scope=MCP_SCOPE_READ,
        autonomy_level=_READ_AUTONOMY,
        description=(
            "Decimal-exact B2B quote matrix with MOQ tiers, FX snapshot and margin-floor flags."
        ),
        input_schema=_QUOTE_MATRIX_SCHEMA,
    ),
    McpToolSpec(
        name="match_open_receivables",
        required_scope=MCP_SCOPE_READ,
        autonomy_level=_READ_AUTONOMY,
        description=(
            "Suggest open receivable milestones matching a received amount. Never settles."
        ),
        input_schema=_RECEIVABLES_SCHEMA,
    ),
    McpToolSpec(
        name="get_live_channel_sales_analytics",
        required_scope=MCP_SCOPE_READ,
        autonomy_level=_READ_AUTONOMY,
        description=(
            "Cross-channel GMV, refunds, commission, ad spend, net margin and ROAS for 7/30/90d."
        ),
        input_schema=_CHANNEL_ANALYTICS_SCHEMA,
    ),
    McpToolSpec(
        name="query_readonly_analytics_sql",
        required_scope=MCP_SCOPE_READ,
        autonomy_level=_READ_AUTONOMY,
        description=(
            "Run a bounded read-only SELECT over the approved analytics views with tenant binding."
        ),
        input_schema=_ANALYTICS_SQL_SCHEMA,
    ),
    McpToolSpec(
        name="create_l1_proposal_draft",
        required_scope=MCP_SCOPE_PROPOSE,
        autonomy_level=_DRAFT_AUTONOMY,
        description=(
            "Insert a PENDING AI proposal that a human must review. Drafts never execute."
        ),
        input_schema=_PROPOSAL_SCHEMA,
    ),
)

MCP_TOOL_NAMES: frozenset[str] = frozenset(spec.name for spec in MCP_TOOL_SPECS)
_SPECS_BY_NAME: dict[str, McpToolSpec] = {spec.name: spec for spec in MCP_TOOL_SPECS}

#: Mutating financial / shipping / inventory operations that MCP can never expose.
BLOCKED_L0_TOOLS: tuple[dict[str, str], ...] = (
    {
        "name": "settle_receivable",
        "description": "Cash settlement of a receivable or payable milestone.",
        "reason": "Financial settlement is L0 human-only (no MCP surface).",
    },
    {
        "name": "record_payment_receipt",
        "description": "Confirm an inbound payment against an invoice.",
        "reason": "Money movement is L0 human-only (no MCP surface).",
    },
    {
        "name": "approve_quote",
        "description": "Approve and send a B2B quote or proforma invoice.",
        "reason": "Commercial commitments stay with a human reviewer.",
    },
    {
        "name": "create_shipment_label",
        "description": "Buy a carrier label and register the tracking number.",
        "reason": "Shipping spend is L0 human-only (no MCP surface).",
    },
    {
        "name": "adjust_inventory",
        "description": "Adjust on-hand inventory or release reservations.",
        "reason": "Stock mutation is L0 human-only (no MCP surface).",
    },
    {
        "name": "publish_listing",
        "description": "Push a listing or price change to a sales channel.",
        "reason": "External publishing is L0 human-only (no MCP surface).",
    },
)


def tool_spec(name: object) -> McpToolSpec | None:
    """Return the catalog entry for `name`, or None when the tool does not exist."""
    if not isinstance(name, str):
        return None
    return _SPECS_BY_NAME.get(name)


def has_scope(scopes: list[str] | tuple[str, ...], required_scope: str) -> bool:
    return required_scope in scopes


def tool_is_allowed(
    spec: McpToolSpec,
    scopes: list[str] | tuple[str, ...],
    allowed_tools: list[str] | tuple[str, ...] | None,
) -> bool:
    """A tool is callable when the token has its scope and the whitelist (if any) allows it."""
    if not has_scope(scopes, spec.required_scope):
        return False
    return not allowed_tools or spec.name in allowed_tools


def permitted_tool_specs(
    scopes: list[str] | tuple[str, ...],
    allowed_tools: list[str] | tuple[str, ...] | None,
) -> list[McpToolSpec]:
    return [spec for spec in MCP_TOOL_SPECS if tool_is_allowed(spec, scopes, allowed_tools)]


def tool_descriptor(spec: McpToolSpec) -> dict[str, Any]:
    """MCP `tools/list` descriptor with the ERP autonomy annotation attached."""
    return {
        "name": spec.name,
        "description": spec.description,
        "inputSchema": spec.input_schema,
        "annotations": {
            "title": spec.name,
            "autonomy_level": spec.autonomy_level,
            "required_scope": spec.required_scope,
            "readOnlyHint": spec.autonomy_level == _READ_AUTONOMY,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    }
