"""MCP Streamable HTTP JSON-RPC 2.0 server over the deterministic ERP tool registry.

Wire contract (protocol version ``2025-03-26``)::

    initialize                -> protocolVersion / capabilities.tools / serverInfo
    notifications/initialized -> no-op (notifications carry no id)
    ping                      -> {}
    tools/list                -> permitted tool descriptors + autonomy_level
    tools/call                -> {"content", "structuredContent", "isError"}

Every ``tools/call`` runs inside ``tenant_session(session_factory, tenant_id)`` so
PostgreSQL ``FORCE ROW LEVEL SECURITY`` — not application filtering — decides
visibility. A token that has only ``mcp:read`` can never call
``create_l1_proposal_draft``, and a token with an ``allowed_tools`` whitelist can
never call anything else. No mutating financial, shipping, inventory or channel
operation is reachable: the catalog contains read tools plus a *draft* tool that
writes a PENDING ``ai_proposals`` row for human review.

Each successful call writes an ``audit_events`` row with
``actor_type = "ai_agent"`` and ``actor_id = "mcp:<token_id>"``; the proposal
draft tool additionally verifies that a cited ``source_tool_call_id`` really came
from an earlier audited call of the same token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.capabilities import CAPABILITY_SPECS
from erp_core.ai_engine.mcp_token_service import ResolvedMcpToken
from erp_core.ai_engine.mcp_tools import (
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    McpToolSpec,
    permitted_tool_specs,
    tool_descriptor,
    tool_is_allowed,
    tool_spec,
)
from erp_core.ai_engine.tools import ErpToolRegistry, ToolResult
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import AiCapability, Incoterm, ProposalType
from erp_core.tenancy.audit import record_audit_event

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: Application-level denial: the token's scope or tool whitelist forbids the call.
FORBIDDEN = -32003

_CONFIDENCE_Q = Decimal("0.001")
_MARGIN_Q = Decimal("0.01")
_VALID_CAPABILITIES = frozenset(capability.value for capability in AiCapability)
_VALID_PROPOSAL_TYPES = frozenset(proposal_type.value for proposal_type in ProposalType)


class McpJsonRpcError(Exception):
    """A JSON-RPC level failure: returned to the client instead of a tool result."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class McpArgumentError(ValueError):
    """Invalid `tools/call` arguments (mapped to JSON-RPC -32602)."""


@dataclass(frozen=True, slots=True)
class McpToolOutcome:
    """Result of one executed tool call, before JSON-RPC envelope wrapping."""

    structured: dict[str, Any]
    is_error: bool
    tool_call_id: str | None = None
    proposal_id: UUID | None = None


def _error_response(
    request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


# --------------------------------------------------------------------------------------
# Argument coercion. JSON floats are rejected wherever an exact decimal is required so
# the ERP's Decimal/NUMERIC money invariants cannot be violated by an MCP client.
# --------------------------------------------------------------------------------------


def _reject_unknown(arguments: dict[str, Any], allowed: frozenset[str], tool: str) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise McpArgumentError(f"Unknown argument(s) for {tool}: {', '.join(unknown)}.")


def _str_arg(
    arguments: dict[str, Any], key: str, *, required: bool, max_length: int = 256
) -> str | None:
    value = arguments.get(key)
    if value is None:
        if required:
            raise McpArgumentError(f"Argument '{key}' is required.")
        return None
    if not isinstance(value, str):
        raise McpArgumentError(f"Argument '{key}' must be a string.")
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise McpArgumentError(f"Argument '{key}' must not be blank.")
        return None
    if len(cleaned) > max_length:
        raise McpArgumentError(f"Argument '{key}' exceeds {max_length} characters.")
    return cleaned


def _required_str(arguments: dict[str, Any], key: str, max_length: int = 256) -> str:
    value = _str_arg(arguments, key, required=True, max_length=max_length)
    if value is None:  # pragma: no cover - required=True already raises
        raise McpArgumentError(f"Argument '{key}' is required.")
    return value


def _int_arg(arguments: dict[str, Any], key: str, *, default: int) -> int:
    value = arguments.get(key, default)
    if type(value) is not int or value < 1:
        raise McpArgumentError(f"Argument '{key}' must be a positive integer.")
    return value


def _decimal_arg(
    arguments: dict[str, Any], key: str, *, required: bool, quantize: Decimal | None = None
) -> Decimal | None:
    value = arguments.get(key)
    if value is None:
        if required:
            raise McpArgumentError(f"Argument '{key}' is required.")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise McpArgumentError(
            f"Argument '{key}' must be an exact decimal string (never an IEEE-754 float)."
        )
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise McpArgumentError(f"Argument '{key}' is not a valid decimal.") from exc
    if not number.is_finite():
        raise McpArgumentError(f"Argument '{key}' is not a finite decimal.")
    return (
        number.quantize(quantize, rounding=ROUND_HALF_UP) if quantize is not None else number
    )


def _uuid_arg(arguments: dict[str, Any], key: str) -> UUID | None:
    raw = _str_arg(arguments, key, required=False, max_length=36)
    if raw is None:
        return None
    try:
        return UUID(raw)
    except ValueError as exc:
        raise McpArgumentError(f"Argument '{key}' must be a UUID.") from exc


def _provenance_uuid(raw: Any, field: str) -> UUID:
    """Map an `ErpToolRegistry` `tc_<hex>` trace id (or a bare UUID) onto its UUID value."""
    if not isinstance(raw, str) or not raw.strip():
        raise McpArgumentError(f"'{field}' must be a tool_call_id string.")
    candidate = raw.strip()
    if candidate.startswith("tc_"):
        candidate = candidate[3:]
    try:
        return UUID(candidate)
    except ValueError as exc:
        raise McpArgumentError(f"'{field}' must be a tool_call_id (tc_<uuid> or <uuid>).") from exc


def _provenance_arg(arguments: dict[str, Any], key: str) -> UUID | None:
    raw = arguments.get(key)
    if raw is None:
        return None
    return _provenance_uuid(raw, key)


def _provenance_refs(tool_call_id: UUID) -> tuple[str, str]:
    """Both accepted on-the-wire spellings of one registry trace id."""
    return f"tc_{tool_call_id.hex}", str(tool_call_id)


def _str_list_arg(arguments: dict[str, Any], key: str) -> list[str] | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise McpArgumentError(f"Argument '{key}' must be an array of strings.")
    cleaned = [item.strip() for item in value if item.strip()]
    return cleaned or None


def _incoterm_arg(arguments: dict[str, Any]) -> Incoterm:
    raw = _str_arg(arguments, "incoterm", required=False, max_length=3)
    if raw is None:
        return Incoterm.FOB
    try:
        return Incoterm(raw.upper())
    except ValueError as exc:
        valid = ", ".join(term.value for term in Incoterm)
        raise McpArgumentError(
            f"Argument 'incoterm' must be one of: {valid}."
        ) from exc


def _quote_items_arg(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = arguments.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise McpArgumentError("Argument 'items' must be a non-empty array.")
    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise McpArgumentError(f"items[{index}] must be an object.")
        _reject_unknown(
            raw_item, frozenset({"sku", "qty", "requested_unit_price"}), f"items[{index}]"
        )
        sku = _required_str(raw_item, "sku", max_length=64)
        item: dict[str, Any] = {"sku": sku, "qty": _int_arg(raw_item, "qty", default=1)}
        requested = _decimal_arg(raw_item, "requested_unit_price", required=False)
        if requested is not None:
            if requested <= 0:
                raise McpArgumentError(f"items[{index}].requested_unit_price must be positive.")
            item["requested_unit_price"] = str(requested)
        items.append(item)
    return items


class McpServer:
    """Stateless JSON-RPC dispatcher; tenant/scope context comes from the token."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ErpToolRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry or ErpToolRegistry()

    # -- JSON-RPC plumbing -------------------------------------------------------------

    async def handle(
        self, ctx: ResolvedMcpToken, payload: Any
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Handle a single request object or a JSON-RPC batch (``None`` = 202, no body)."""
        if isinstance(payload, list):
            if not payload:
                return _error_response(None, INVALID_REQUEST, "Invalid Request")
            responses: list[dict[str, Any]] = []
            for item in payload:
                response = await self.handle_message(ctx, item)
                if response is not None:
                    responses.append(response)
            return responses or None
        return await self.handle_message(ctx, payload)

    async def handle_message(
        self, ctx: ResolvedMcpToken, message: Any
    ) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _error_response(None, INVALID_REQUEST, "Invalid Request")
        has_id = "id" in message
        request_id = message.get("id")
        if message.get("jsonrpc") != JSONRPC_VERSION:
            return (
                _error_response(request_id, INVALID_REQUEST, "Invalid Request")
                if has_id
                else None
            )
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return (
                _error_response(request_id, INVALID_REQUEST, "Invalid Request")
                if has_id
                else None
            )
        params = message.get("params")
        if params is not None and not isinstance(params, dict):
            return (
                _error_response(request_id, INVALID_PARAMS, "Invalid params") if has_id else None
            )
        arguments: dict[str, Any] = params or {}
        try:
            result = await self._dispatch(ctx, method, arguments)
        except McpJsonRpcError as exc:
            return _error_response(request_id, exc.code, exc.message, exc.data) if has_id else None
        except Exception:  # noqa: BLE001 - a transport fault must never leak a stack trace
            return _error_response(request_id, INTERNAL_ERROR, "Internal error") if has_id else None
        if not has_id:
            return None
        return _success_response(request_id, result)

    async def _dispatch(
        self, ctx: ResolvedMcpToken, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
            }
        if method == "notifications/initialized":
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    tool_descriptor(spec)
                    for spec in permitted_tool_specs(ctx.scopes, ctx.allowed_tools)
                ]
            }
        if method == "tools/call":
            return await self._call_tool(ctx, params)
        raise McpJsonRpcError(METHOD_NOT_FOUND, "Method not found", {"method": method})

    # -- tools/call --------------------------------------------------------------------

    async def _call_tool(
        self, ctx: ResolvedMcpToken, params: dict[str, Any]
    ) -> dict[str, Any]:
        name = params.get("name")
        spec = tool_spec(name)
        if spec is None:
            raise McpJsonRpcError(INVALID_PARAMS, "Unknown tool", {"tool": name})
        if not tool_is_allowed(spec, ctx.scopes, ctx.allowed_tools):
            raise McpJsonRpcError(
                FORBIDDEN,
                "MCP token is not permitted to call this tool",
                {"tool": spec.name, "required_scope": spec.required_scope},
            )
        raw_arguments = params.get("arguments")
        if raw_arguments is None:
            raw_arguments = {}
        if not isinstance(raw_arguments, dict):
            raise McpJsonRpcError(INVALID_PARAMS, "Tool arguments must be an object")
        arguments: dict[str, Any] = raw_arguments
        try:
            outcome = await self._execute(ctx, spec, arguments)
        except McpArgumentError as exc:
            raise McpJsonRpcError(INVALID_PARAMS, str(exc), {"tool": spec.name}) from exc
        payload = json.dumps(outcome.structured, ensure_ascii=False, default=str)
        return {
            "content": [{"type": "text", "text": payload}],
            "structuredContent": outcome.structured,
            "isError": outcome.is_error,
        }

    async def _execute(
        self, ctx: ResolvedMcpToken, spec: McpToolSpec, arguments: dict[str, Any]
    ) -> McpToolOutcome:
        async with tenant_session(self._session_factory, ctx.tenant_id) as session:
            try:
                outcome = await self._invoke(session, ctx, spec, arguments)
            except McpArgumentError:
                raise
            except Exception:
                await record_audit_event(
                    session,
                    tenant_id=ctx.tenant_id,
                    actor_type="ai_agent",
                    actor_id=ctx.actor_id(),
                    action="mcp.tool.called",
                    resource_type="mcp_tool",
                    resource_id=ctx.token_id,
                    after_state={"tool": spec.name, "ok": False, "error": "internal"},
                )
                raise
            await record_audit_event(
                session,
                tenant_id=ctx.tenant_id,
                actor_type="ai_agent",
                actor_id=ctx.actor_id(),
                action="mcp.tool.called",
                resource_type="mcp_tool",
                resource_id=ctx.token_id,
                proposal_id=outcome.proposal_id,
                after_state={
                    "tool": spec.name,
                    "ok": not outcome.is_error,
                    "tool_call_id": outcome.tool_call_id,
                    "proposal_id": str(outcome.proposal_id) if outcome.proposal_id else None,
                    "argument_keys": sorted(arguments),
                },
            )
            return outcome

    async def _invoke(
        self,
        session: AsyncSession,
        ctx: ResolvedMcpToken,
        spec: McpToolSpec,
        arguments: dict[str, Any],
    ) -> McpToolOutcome:
        if spec.name == "get_live_sku_pricing_and_stock":
            return await self._sku_pricing(session, ctx, arguments)
        if spec.name == "get_live_order_tracking":
            return await self._order_tracking(session, ctx, arguments)
        if spec.name == "calc_b2b_quote_matrix":
            return await self._quote_matrix(session, ctx, arguments)
        if spec.name == "match_open_receivables":
            return await self._receivables(session, ctx, arguments)
        if spec.name == "get_live_channel_sales_analytics":
            return await self._channel_analytics(session, ctx, arguments)
        if spec.name == "query_readonly_analytics_sql":
            return await self._analytics_sql(session, ctx, arguments)
        if spec.name == "create_l1_proposal_draft":
            return await self._proposal_draft(session, ctx, arguments)
        raise McpJsonRpcError(INVALID_PARAMS, "Unknown tool", {"tool": spec.name})

    async def _sku_pricing(
        self, session: AsyncSession, ctx: ResolvedMcpToken, arguments: dict[str, Any]
    ) -> McpToolOutcome:
        _reject_unknown(
            arguments, frozenset({"sku", "qty", "incoterm"}), "get_live_sku_pricing_and_stock"
        )
        sku = _required_str(arguments, "sku", max_length=64)
        result = await self._registry.get_live_sku_pricing_and_stock(
            session,
            ctx.tenant_id,
            sku=sku,
            qty=_int_arg(arguments, "qty", default=1),
            incoterm=_incoterm_arg(arguments),
        )
        return _outcome("get_live_sku_pricing_and_stock", result)

    async def _order_tracking(
        self, session: AsyncSession, ctx: ResolvedMcpToken, arguments: dict[str, Any]
    ) -> McpToolOutcome:
        _reject_unknown(
            arguments, frozenset({"order_no", "customer_id"}), "get_live_order_tracking"
        )
        order_no = _required_str(arguments, "order_no", max_length=64)
        result = await self._registry.get_live_order_tracking(
            session,
            ctx.tenant_id,
            order_no=order_no,
            customer_id=_uuid_arg(arguments, "customer_id"),
        )
        return _outcome("get_live_order_tracking", result)

    async def _quote_matrix(
        self, session: AsyncSession, ctx: ResolvedMcpToken, arguments: dict[str, Any]
    ) -> McpToolOutcome:
        _reject_unknown(
            arguments,
            frozenset({"items", "currency", "incoterm"}),
            "calc_b2b_quote_matrix",
        )
        currency = _required_str(arguments, "currency", max_length=3)
        if not currency.isalpha():
            raise McpArgumentError("Argument 'currency' must be a 3-letter ISO code.")
        result = await self._registry.calc_b2b_quote_matrix(
            session,
            ctx.tenant_id,
            items=_quote_items_arg(arguments),
            currency=currency.upper(),
            incoterm=_incoterm_arg(arguments),
        )
        return _outcome("calc_b2b_quote_matrix", result)

    async def _receivables(
        self, session: AsyncSession, ctx: ResolvedMcpToken, arguments: dict[str, Any]
    ) -> McpToolOutcome:
        _reject_unknown(
            arguments,
            frozenset({"amount", "currency", "customer_id", "reference_hint"}),
            "match_open_receivables",
        )
        amount = _decimal_arg(arguments, "amount", required=True)
        if amount is None:  # pragma: no cover - required=True already raises
            raise McpArgumentError("Argument 'amount' is required.")
        currency = _required_str(arguments, "currency", max_length=3)
        result = await self._registry.match_open_receivables(
            session,
            ctx.tenant_id,
            amount=amount,
            currency=currency.upper(),
            customer_id=_uuid_arg(arguments, "customer_id"),
            reference_hint=_str_arg(arguments, "reference_hint", required=False, max_length=256),
        )
        return _outcome("match_open_receivables", result)

    async def _channel_analytics(
        self, session: AsyncSession, ctx: ResolvedMcpToken, arguments: dict[str, Any]
    ) -> McpToolOutcome:
        _reject_unknown(
            arguments, frozenset({"window", "channel_types"}), "get_live_channel_sales_analytics"
        )
        window = _str_arg(arguments, "window", required=False, max_length=3) or "30d"
        result = await self._registry.get_live_channel_sales_analytics(
            session,
            ctx.tenant_id,
            window=window,
            channel_types=_str_list_arg(arguments, "channel_types"),
        )
        return _outcome("get_live_channel_sales_analytics", result)

    async def _analytics_sql(
        self, session: AsyncSession, ctx: ResolvedMcpToken, arguments: dict[str, Any]
    ) -> McpToolOutcome:
        _reject_unknown(arguments, frozenset({"sql"}), "query_readonly_analytics_sql")
        sql = _required_str(arguments, "sql", max_length=2048)
        result = await self._registry.query_readonly_analytics_sql(
            session, ctx.tenant_id, sql=sql
        )
        return _outcome("query_readonly_analytics_sql", result)

    # -- L1 proposal drafting ----------------------------------------------------------

    async def _proposal_draft(
        self, session: AsyncSession, ctx: ResolvedMcpToken, arguments: dict[str, Any]
    ) -> McpToolOutcome:
        _reject_unknown(
            arguments,
            frozenset(
                {
                    "capability",
                    "proposal_type",
                    "proposed_payload",
                    "citations",
                    "target_entity_type",
                    "target_entity_id",
                    "source_tool_call_id",
                    "confidence_score",
                    "estimated_margin_pct",
                }
            ),
            "create_l1_proposal_draft",
        )
        capability = _required_str(arguments, "capability", max_length=32)
        if capability not in _VALID_CAPABILITIES:
            raise McpArgumentError(
                "Argument 'capability' must be one of: "
                + ", ".join(sorted(_VALID_CAPABILITIES))
                + "."
            )
        proposal_type = _required_str(arguments, "proposal_type", max_length=32)
        if proposal_type not in _VALID_PROPOSAL_TYPES:
            raise McpArgumentError(
                "Argument 'proposal_type' must be one of: "
                + ", ".join(sorted(_VALID_PROPOSAL_TYPES))
                + "."
            )
        if proposal_type not in _allowed_proposal_types(capability):
            raise McpArgumentError(
                f"Capability '{capability}' does not produce '{proposal_type}' proposals."
            )
        proposed_payload = arguments.get("proposed_payload")
        if not isinstance(proposed_payload, dict) or not proposed_payload:
            raise McpArgumentError("Argument 'proposed_payload' must be a non-empty object.")
        target_entity_type = _str_arg(
            arguments, "target_entity_type", required=False, max_length=64
        )
        target_entity_id = _uuid_arg(arguments, "target_entity_id")
        if target_entity_id is not None and target_entity_type is None:
            raise McpArgumentError(
                "Argument 'target_entity_type' is required when 'target_entity_id' is given."
            )
        confidence = _decimal_arg(
            arguments, "confidence_score", required=False, quantize=_CONFIDENCE_Q
        )
        if confidence is None:
            confidence = Decimal("0.500")
        if not Decimal("0") <= confidence <= Decimal("1"):
            raise McpArgumentError("Argument 'confidence_score' must be between 0 and 1.")
        margin = _decimal_arg(
            arguments, "estimated_margin_pct", required=False, quantize=_MARGIN_Q
        )
        if margin is not None and not Decimal("-100") <= margin <= Decimal("100"):
            raise McpArgumentError("Argument 'estimated_margin_pct' must be between -100 and 100.")

        source_tool_call_id = _provenance_arg(arguments, "source_tool_call_id")
        citations = await self._verified_citations(session, ctx, arguments, source_tool_call_id)

        row = (
            (
                await session.execute(
                    text(
                        """
                        INSERT INTO ai_proposals (
                            tenant_id, capability, proposal_type, target_entity_type,
                            target_entity_id, proposed_payload, citations,
                            source_tool_call_id, confidence_score, estimated_margin_pct, status
                        ) VALUES (
                            :tid, :capability, :proposal_type, :target_type,
                            :target_id, CAST(:payload AS jsonb), CAST(:citations AS jsonb),
                            :source_tool_call_id, CAST(:confidence AS NUMERIC(4, 3)),
                            CAST(:margin AS NUMERIC(5, 2)), 'pending'
                        )
                        RETURNING id, status, created_at
                        """
                    ),
                    {
                        "tid": ctx.tenant_id,
                        "capability": capability,
                        "proposal_type": proposal_type,
                        "target_type": target_entity_type,
                        "target_id": target_entity_id,
                        "payload": json.dumps(proposed_payload, ensure_ascii=False, default=str),
                        "citations": json.dumps(citations, ensure_ascii=False, default=str),
                        "source_tool_call_id": source_tool_call_id,
                        "confidence": confidence,
                        "margin": margin,
                    },
                )
            )
            .mappings()
            .one()
        )
        proposal_id: UUID = row["id"]
        structured = {
            "tool": "create_l1_proposal_draft",
            "ok": True,
            "tool_call_id": None,
            "proposal_id": str(proposal_id),
            "status": str(row["status"]),
            "capability": capability,
            "proposal_type": proposal_type,
            "requires_human_review": True,
            "executed": False,
            "source_tool_call_id": str(source_tool_call_id) if source_tool_call_id else None,
            "citations": citations,
            "confidence_score": str(confidence),
            "created_at": row["created_at"].isoformat(),
            "autonomy_level": "L1_proposal",
        }
        return McpToolOutcome(structured=structured, is_error=False, proposal_id=proposal_id)

    async def _verified_citations(
        self,
        session: AsyncSession,
        ctx: ResolvedMcpToken,
        arguments: dict[str, Any],
        source_tool_call_id: UUID | None,
    ) -> list[dict[str, Any]]:
        """Require every cited tool_call_id to be a real audited call of this token."""
        raw_citations = arguments.get("citations")
        if raw_citations is None:
            raw_citations = []
        if not isinstance(raw_citations, list):
            raise McpArgumentError("Argument 'citations' must be an array of objects.")
        citations: list[dict[str, Any]] = []
        cited_ids: set[UUID] = set()
        for index, raw_citation in enumerate(raw_citations):
            if not isinstance(raw_citation, dict):
                raise McpArgumentError(f"citations[{index}] must be an object.")
            citation = {str(key): value for key, value in raw_citation.items()}
            cited_tool_call_id = citation.get("tool_call_id")
            if cited_tool_call_id is not None:
                cited_ids.add(
                    _provenance_uuid(cited_tool_call_id, f"citations[{index}].tool_call_id")
                )
            citations.append(citation)
        if source_tool_call_id is not None:
            cited_ids.add(source_tool_call_id)
        for tool_call_id in sorted(cited_ids):
            if not await self._tool_call_is_audited(session, ctx, tool_call_id):
                raise McpArgumentError(
                    "Provenance check failed: source_tool_call_id "
                    f"{tool_call_id} is not an audited tool call of this MCP token."
                )
        return citations

    async def _tool_call_is_audited(
        self, session: AsyncSession, ctx: ResolvedMcpToken, tool_call_id: UUID
    ) -> bool:
        prefixed, bare = _provenance_refs(tool_call_id)
        found = await session.scalar(
            text(
                """
                SELECT 1
                FROM audit_events
                WHERE tenant_id = :tid
                  AND action = 'mcp.tool.called'
                  AND actor_id = :actor_id
                  AND after_state ->> 'tool_call_id' IN (:prefixed, :bare)
                LIMIT 1
                """
            ),
            {
                "tid": ctx.tenant_id,
                "actor_id": ctx.actor_id(),
                "prefixed": prefixed,
                "bare": bare,
            },
        )
        return found is not None


def _allowed_proposal_types(capability: str) -> frozenset[str]:
    spec = CAPABILITY_SPECS[AiCapability(capability)]
    return frozenset(proposal_type.value for proposal_type in spec.allowed_proposal_types)


def _outcome(tool_name: str, result: ToolResult) -> McpToolOutcome:
    structured: dict[str, Any] = {
        "tool": tool_name,
        "ok": result.ok,
        "data": result.data,
        "tool_call_id": result.tool_call_id,
    }
    if not result.ok:
        structured["error_code"] = result.error_code
        structured["error_message"] = result.error_message
    return McpToolOutcome(
        structured=structured,
        is_error=not result.ok,
        tool_call_id=result.tool_call_id,
    )
