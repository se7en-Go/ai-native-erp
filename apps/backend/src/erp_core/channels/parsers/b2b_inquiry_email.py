from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from erp_core.shared.enums import ChannelType

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_QTY_NUM_RE = re.compile(r"(\d[\d,]*)")


class ParsedB2BInquiry(BaseModel):
    platform: Literal["made_in_china", "alibaba"]
    channel_type: ChannelType
    buyer_name: str | None = None
    buyer_email: str | None = None
    company_name: str | None = None
    country: str | None = None
    product_sku_or_title: str | None = None
    target_quantity: str | None = None
    target_quantity_numeric: int | None = None
    inquiry_message: str | None = None
    inquiry_id: str | None = None


class B2BInquiryEmailParser:
    """Extracts structured B2B buyer and SKU intent from Made-in-China and 
    Alibaba inquiry emails."""

    def _extract_field(self, text_body: str, labels: tuple[str, ...]) -> str | None:
        for label in labels:
            pattern = re.compile(
                rf"^\s*{re.escape(label)}\s*[:：]\s*(.+?)\s*$",
                re.IGNORECASE | re.MULTILINE,
            )
            match = pattern.search(text_body)
            if match:
                val = match.group(1).strip()
                if val:
                    return val
        return None

    def _extract_message_block(self, text_body: str) -> str | None:
        pattern = re.compile(
            r"(?:Message|Inquiry Details|Content)\s*[:：]\s*(.+)",
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(text_body)
        if match:
            return match.group(1).strip()
        return None

    def parse(
        self,
        sender_email: str,
        subject: str,
        body_text: str,
        reply_to: str | None = None,
    ) -> ParsedB2BInquiry | None:
        sender_lower = sender_email.strip().lower()
        subj_lower = subject.strip().lower()

        platform: Literal["made_in_china", "alibaba"] | None = None
        if (
            "en_notification@made-in-china.com" in sender_lower
            or "made-in-china.com" in sender_lower
        ):
            platform = "made_in_china"
        elif any(
            dom in sender_lower for dom in ("service.alibaba.com", "alibaba-inc.com", "alibaba.com")
        ) or ("alibaba.com" in subj_lower and "inquiry" in subj_lower):
            platform = "alibaba"

        if platform is None:
            return None

        channel_type = (
            ChannelType.MADE_IN_CHINA_EMAIL
            if platform == "made_in_china"
            else ChannelType.ALIBABA_EMAIL
        )

        buyer_name = self._extract_field(
            body_text, ("Buyer Name", "Contact Name", "Sender", "From")
        )
        raw_email = self._extract_field(body_text, ("Buyer Email", "Email", "E-mail"))
        buyer_email: str | None = None
        if raw_email:
            email_match = _EMAIL_RE.search(raw_email)
            if email_match:
                buyer_email = email_match.group(0).lower()
        if buyer_email is None and reply_to:
            rt_match = _EMAIL_RE.search(reply_to)
            if rt_match and not any(
                d in rt_match.group(0).lower() for d in ("made-in-china.com", "alibaba.com")
            ):
                buyer_email = rt_match.group(0).lower()

        company_name = self._extract_field(body_text, ("Company Name", "Company", "Buyer Company"))
        country = self._extract_field(body_text, ("Country/Region", "Country", "Region"))
        product = self._extract_field(body_text, ("Product Name", "Product", "Subject"))
        if not product and "inquiry about " in subj_lower:
            product = subject.split("about ", 1)[-1].strip()

        target_qty = self._extract_field(
            body_text,
            ("Purchase Quantity", "Target Quantity", "Order Quantity", "Quantity", "Qty"),
        )
        qty_numeric: int | None = None
        if target_qty:
            num_match = _QTY_NUM_RE.search(target_qty)
            if num_match:
                qty_numeric = int(num_match.group(1).replace(",", ""))

        inquiry_id = self._extract_field(body_text, ("Inquiry ID", "Ref No", "Order/RFQ ID"))
        inquiry_msg = self._extract_message_block(body_text) or body_text.strip()

        return ParsedB2BInquiry(
            platform=platform,
            channel_type=channel_type,
            buyer_name=buyer_name,
            buyer_email=buyer_email,
            company_name=company_name,
            country=country,
            product_sku_or_title=product,
            target_quantity=target_qty,
            target_quantity_numeric=qty_numeric,
            inquiry_message=inquiry_msg,
            inquiry_id=inquiry_id,
        )
