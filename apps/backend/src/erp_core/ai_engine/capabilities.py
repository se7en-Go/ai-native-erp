from __future__ import annotations

from dataclasses import dataclass

from erp_core.shared.enums import AiCapability, AutonomyLevel, ProposalType


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    capability: AiCapability
    display_name: str
    default_autonomy: AutonomyLevel
    max_allowed_autonomy: AutonomyLevel
    allowed_proposal_types: tuple[ProposalType, ...]
    system_prompt: str


_L0 = AutonomyLevel.L0_COPILOT
_L1 = AutonomyLevel.L1_PROPOSAL
_L2 = AutonomyLevel.L2_AUTO
CAPABILITY_SPECS = {
    cap: CapabilitySpec(cap, title, level, level, kinds, prompt)
    for cap, title, level, kinds, prompt in (
        (
            AiCapability.CS_SUPPORT,
            "Customer support",
            _L2,
            (ProposalType.REPLY_DRAFT, ProposalType.REFUND_OR_RESHIP),
            "Answer technical and policy FAQs by copying a cited evidence line exactly.",
        ),
        (
            AiCapability.SALES_QUOTING,
            "Sales and quoting",
            _L1,
            (
                ProposalType.QUOTE_DRAFT,
                ProposalType.CRM_ENTITY_EXTRACT,
                ProposalType.IDENTITY_MERGE,
            ),
            "Extract B2B requirements. Never promise a price, contract, or discount.",
        ),
        (
            AiCapability.ORDER_LOGISTICS,
            "Order logistics",
            _L2,
            (ProposalType.REPLY_DRAFT, ProposalType.FOLLOW_UP_SUGGESTION),
            "Use verified order and tracking facts; escalate shipment exceptions.",
        ),
        (
            AiCapability.CONTENT_STUDIO,
            "Content studio",
            _L1,
            (ProposalType.REPLY_DRAFT,),
            "Translate or summarize faithfully for internal review. Do not add new claims.",
        ),
        (
            AiCapability.BIZ_ANALYTICS,
            "Business analytics",
            _L2,
            (),
            "Use approved read-only ERP reports.",
        ),
        (
            AiCapability.FINANCE_RECON,
            "Finance reconciliation",
            _L0,
            (ProposalType.PAYMENT_MATCH,),
            "Suggest receivable matches; never confirm settlement.",
        ),
    )
}

_AUTONOMY_RANK = {_L0: 0, _L1: 1, _L2: 2}


def min_autonomy(*levels: AutonomyLevel) -> AutonomyLevel:
    return min(levels, key=_AUTONOMY_RANK.__getitem__)
