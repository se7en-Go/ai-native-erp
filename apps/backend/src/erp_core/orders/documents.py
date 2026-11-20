from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.lib.styles import (  # type: ignore[import-untyped]
    ParagraphStyle,
    getSampleStyleSheet,
)
from reportlab.lib.units import mm  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.errors import AppError
from erp_core.tenancy.schemas import PiTemplateSettings, parse_pi_template

DocumentLanguage = Literal["en", "zh"]

_CENT = Decimal("0.01")
_MEASURE = Decimal("0.0001")

DOC_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "ci_title": "COMMERCIAL INVOICE",
        "pl_title": "PACKING LIST",
        "seller": "Seller",
        "buyer": "Buyer",
        "invoice_no": "Invoice No.",
        "packing_no": "Packing List No.",
        "order_no": "Order No.",
        "issue_date": "Issue Date",
        "currency": "Currency",
        "incoterm": "Incoterm",
        "sku": "SKU",
        "description": "Description",
        "hs_code": "HS Code",
        "qty": "Qty",
        "unit_price": "Unit Price",
        "line_total": "Amount",
        "subtotal": "Goods Subtotal",
        "shipping": "Freight",
        "discount": "Discount",
        "tax": "Tax",
        "grand_total": "Grand Total",
        "bank_info": "T/T Bank Wire Details",
        "beneficiary": "Beneficiary",
        "bank_name": "Bank Name",
        "account_no": "Account No.",
        "swift": "SWIFT / BIC",
        "bank_address": "Bank Address",
        "units_per_carton": "Units/Carton",
        "cartons": "Cartons",
        "net_weight": "N.W. (kg)",
        "gross_weight": "G.W. (kg)",
        "volume": "Volume (CBM)",
        "total_row": "TOTAL",
        "total_qty": "Total Qty",
        "total_cartons": "Total Cartons",
        "total_net_weight": "Total Net Weight",
        "total_gross_weight": "Total Gross Weight",
        "total_volume": "Total Volume",
        "shipping_marks": "Shipping Marks",
        "signature": "Authorized Signature & Company Stamp",
        "seller_signature": "For and on behalf of the Seller",
        "declaration": "We certify that the information on this document is true and correct.",
        "tax_no": "Tax / Reg. No.",
        "no_packing_specs": "Packing specifications are not configured for these SKUs.",
    },
    "zh": {
        "ci_title": "商业发票 (COMMERCIAL INVOICE)",
        "pl_title": "装箱单 (PACKING LIST)",
        "seller": "卖方",
        "buyer": "买方",
        "invoice_no": "发票号",
        "packing_no": "装箱单号",
        "order_no": "订单号",
        "issue_date": "签发日期",
        "currency": "币种",
        "incoterm": "贸易术语",
        "sku": "SKU",
        "description": "品名与规格",
        "hs_code": "HS 编码",
        "qty": "数量",
        "unit_price": "单价",
        "line_total": "金额",
        "subtotal": "货值小计",
        "shipping": "运费",
        "discount": "折扣",
        "tax": "税费",
        "grand_total": "总计金额",
        "bank_info": "银行电汇 (T/T) 信息",
        "beneficiary": "收款人",
        "bank_name": "开户银行",
        "account_no": "银行账号",
        "swift": "SWIFT / BIC",
        "bank_address": "银行地址",
        "units_per_carton": "每箱数量",
        "cartons": "箱数",
        "net_weight": "净重 (kg)",
        "gross_weight": "毛重 (kg)",
        "volume": "体积 (CBM)",
        "total_row": "合计",
        "total_qty": "总数量",
        "total_cartons": "总箱数",
        "total_net_weight": "总净重",
        "total_gross_weight": "总毛重",
        "total_volume": "总体积",
        "shipping_marks": "唛头",
        "signature": "授权签字及公司盖章",
        "seller_signature": "卖方代表签字",
        "declaration": "兹证明本单据所载信息真实准确。",
        "tax_no": "税号 / 注册号",
        "no_packing_specs": "这些 SKU 尚未配置装箱参数。",
    },
}


@dataclass(slots=True)
class ExportDocumentLine:
    sku: str
    description: str
    hs_code: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    units_per_carton: int
    cartons: int
    net_weight_kg: Decimal
    gross_weight_kg: Decimal
    volume_cbm: Decimal


@dataclass(slots=True)
class ExportDocumentContext:
    """Everything both the commercial invoice and the packing list need to render."""

    tenant_id: UUID
    language: str
    order_id: UUID
    order_no: str
    issued_at: datetime
    currency: str
    incoterm: str
    seller_name: str
    seller_address: str
    seller_phone: str
    seller_email: str
    seller_website: str
    seller_tax_no: str
    buyer_name: str
    buyer_contact: str
    buyer_email: str
    buyer_phone: str
    buyer_country: str
    bank_beneficiary_name: str
    bank_name: str
    bank_account_no: str
    bank_swift_code: str
    bank_address: str
    terms_notes: str
    footer_signature_title: str
    subtotal_amount: Decimal
    shipping_charged: Decimal
    discount_amount: Decimal
    tax_collected: Decimal
    total_amount: Decimal
    lines: list[ExportDocumentLine] = field(default_factory=list)

    @property
    def invoice_no(self) -> str:
        return f"CI-{self.order_no}"

    @property
    def packing_list_no(self) -> str:
        return f"PL-{self.order_no}"

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def total_cartons(self) -> int:
        return sum(line.cartons for line in self.lines)

    @property
    def total_net_weight_kg(self) -> Decimal:
        return _round_cents(sum((line.net_weight_kg for line in self.lines), Decimal("0")))

    @property
    def total_gross_weight_kg(self) -> Decimal:
        return _round_cents(sum((line.gross_weight_kg for line in self.lines), Decimal("0")))

    @property
    def total_volume_cbm(self) -> Decimal:
        return _round_measure(sum((line.volume_cbm for line in self.lines), Decimal("0")))

    @property
    def has_packing_specs(self) -> bool:
        return any(line.units_per_carton > 0 for line in self.lines)


def _round_cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _round_measure(value: Decimal) -> Decimal:
    return value.quantize(_MEASURE, rounding=ROUND_HALF_UP)


def _spec_decimal(attributes: dict[str, Any], key: str) -> Decimal:
    raw = attributes.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return Decimal("0")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")
    if not value.is_finite() or value < 0:
        return Decimal("0")
    return value


def _spec_int(attributes: dict[str, Any], key: str) -> int:
    raw = attributes.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return 0
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return 0
    if not value.is_finite() or value < 0:
        return 0
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def _description(specs: dict[str, Any], language: str, fallback: str) -> str:
    for key in (language, "en"):
        block = specs.get(key)
        if isinstance(block, dict):
            title = block.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    return fallback


def _normalize_language(language: str) -> str:
    return "zh" if language.strip().lower().startswith("zh") else "en"


def _packing_line(
    *,
    sku: str,
    description: str,
    hs_code: str,
    quantity: int,
    unit_price: Decimal,
    line_total: Decimal,
    attributes: dict[str, Any],
) -> ExportDocumentLine:
    units_per_carton = _spec_int(attributes, "carton_qty")
    cartons = (
        int(
            (Decimal(quantity) / Decimal(units_per_carton)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        if units_per_carton > 0
        else 0
    )
    return ExportDocumentLine(
        sku=sku,
        description=description,
        hs_code=hs_code,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
        units_per_carton=units_per_carton,
        cartons=cartons,
        net_weight_kg=_round_cents(
            Decimal(cartons) * _spec_decimal(attributes, "net_weight_kg")
        ),
        gross_weight_kg=_round_cents(
            Decimal(cartons) * _spec_decimal(attributes, "gross_weight_kg")
        ),
        volume_cbm=_round_measure(
            Decimal(cartons) * _spec_decimal(attributes, "cbm_per_carton")
        ),
    )


class ExportDocumentService:
    """Read model for export paperwork: order, buyer, seller letterhead and packing specs."""

    async def build_context(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        *,
        language: str = "en",
    ) -> ExportDocumentContext:
        header = (
            await session.execute(
                sa.text(
                    """
                    SELECT o.id, o.order_no, o.currency, o.subtotal_amount,
                           o.shipping_charged, o.discount_amount, o.tax_collected,
                           o.total_amount, o.created_at,
                           COALESCE(o.channel_metadata->>'incoterm', '') AS incoterm,
                           c.display_name AS customer_name,
                           COALESCE(c.primary_email, '') AS customer_email,
                           COALESCE(c.primary_phone_e164, '') AS customer_phone,
                           COALESCE(c.country_code, '') AS customer_country,
                           t.name AS tenant_name,
                           t.ai_policy_settings->'pi_template' AS pi_template
                    FROM orders o
                    JOIN customers c
                        ON c.tenant_id = o.tenant_id AND c.id = o.customer_id
                    JOIN tenants t ON t.id = o.tenant_id
                    WHERE o.tenant_id = :tid AND o.id = :oid
                    """
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).mappings().one_or_none()
        if header is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' does not exist in this workspace.",
            )

        rows = (
            await session.execute(
                sa.text(
                    """
                    SELECT oi.sku_snapshot, oi.quantity, oi.unit_price, oi.line_total,
                           COALESCE(pv.attributes, '{}'::jsonb) AS attributes,
                           COALESCE(p.hs_code, '') AS hs_code,
                           COALESCE(p.spu_code, oi.sku_snapshot) AS spu_code,
                           COALESCE(p.i18n_specs, '{}'::jsonb) AS i18n_specs
                    FROM order_items oi
                    JOIN product_variants pv
                        ON pv.tenant_id = oi.tenant_id AND pv.id = oi.variant_id
                    JOIN products p
                        ON p.tenant_id = pv.tenant_id AND p.id = pv.product_id
                    WHERE oi.tenant_id = :tid AND oi.order_id = :oid
                    ORDER BY oi.sku_snapshot
                    """
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).mappings().all()

        resolved_language = _normalize_language(language)
        template: PiTemplateSettings = parse_pi_template(
            str(header["tenant_name"]), header["pi_template"]
        )
        lines = [
            _packing_line(
                sku=str(row["sku_snapshot"]),
                description=_description(
                    dict(row["i18n_specs"] or {}), resolved_language, str(row["spu_code"])
                ),
                hs_code=str(row["hs_code"] or ""),
                quantity=int(row["quantity"]),
                unit_price=Decimal(str(row["unit_price"])),
                line_total=Decimal(str(row["line_total"])),
                attributes=dict(row["attributes"] or {}),
            )
            for row in rows
        ]

        return ExportDocumentContext(
            tenant_id=tenant_id,
            language=resolved_language,
            order_id=UUID(str(header["id"])),
            order_no=str(header["order_no"]),
            issued_at=header["created_at"],
            currency=str(header["currency"]),
            incoterm=str(header["incoterm"] or ""),
            seller_name=template.company_legal_name or str(header["tenant_name"]),
            seller_address=template.company_address,
            seller_phone=template.contact_phone,
            seller_email=template.contact_email,
            seller_website=template.website,
            seller_tax_no=template.tax_or_reg_no,
            buyer_name=str(header["customer_name"]),
            buyer_contact=str(header["customer_name"]),
            buyer_email=str(header["customer_email"]),
            buyer_phone=str(header["customer_phone"]),
            buyer_country=str(header["customer_country"]),
            bank_beneficiary_name=template.bank_beneficiary_name,
            bank_name=template.bank_name,
            bank_account_no=template.bank_account_no,
            bank_swift_code=template.bank_swift_code,
            bank_address=template.bank_address,
            terms_notes=template.default_terms_notes,
            footer_signature_title=template.footer_signature_title,
            subtotal_amount=Decimal(str(header["subtotal_amount"])),
            shipping_charged=Decimal(str(header["shipping_charged"])),
            discount_amount=Decimal(str(header["discount_amount"])),
            tax_collected=Decimal(str(header["tax_collected"])),
            total_amount=Decimal(str(header["total_amount"])),
            lines=lines,
        )


def safe_filename(prefix: str, order_no: str) -> str:
    """Strip characters that would break a Content-Disposition filename."""
    cleaned = "".join(ch for ch in order_no if ch.isalnum() or ch in "-_.").strip()
    return f"{prefix}-{cleaned or 'order'}.pdf"


@dataclass(slots=True)
class _DocStyles:
    """Emerald/slate reportlab palette and paragraph styles shared by both documents."""

    font_name: str
    bold_font: str
    emerald: Any
    deep_emerald: Any
    ink: Any
    muted: Any
    hairline: Any
    title: Any
    small: Any
    section: Any
    body: Any
    cell: Any
    head: Any


def _build_styles(language: str) -> _DocStyles:

    font_name, bold_font = "Helvetica", "Helvetica-Bold"
    if language == "zh":
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font_name = bold_font = "STSong-Light"

    deep_emerald = colors.HexColor("#065f46")
    ink = colors.HexColor("#111827")
    muted = colors.HexColor("#6b7280")
    sheets = getSampleStyleSheet()
    return _DocStyles(
        font_name=font_name,
        bold_font=bold_font,
        emerald=colors.HexColor("#059669"),
        deep_emerald=deep_emerald,
        ink=ink,
        muted=muted,
        hairline=colors.HexColor("#e5e7eb"),
        title=ParagraphStyle(
            "DocTitle",
            parent=sheets["Heading1"],
            fontName=bold_font,
            fontSize=15,
            leading=18,
            alignment=2,
            textColor=deep_emerald,
        ),
        small=ParagraphStyle(
            "DocSmall",
            parent=sheets["BodyText"],
            fontName=font_name,
            fontSize=8.5,
            leading=11,
            textColor=muted,
        ),
        section=ParagraphStyle(
            "DocSection",
            parent=sheets["BodyText"],
            fontName=bold_font,
            fontSize=8.5,
            leading=11,
            textColor=deep_emerald,
        ),
        body=ParagraphStyle(
            "DocBody",
            parent=sheets["BodyText"],
            fontName=font_name,
            fontSize=8,
            leading=11,
            textColor=ink,
        ),
        cell=ParagraphStyle(
            "DocCell", parent=sheets["BodyText"], fontName=font_name, fontSize=8, leading=10
        ),
        head=ParagraphStyle(
            "DocHead",
            parent=sheets["BodyText"],
            fontName=bold_font,
            fontSize=8,
            leading=10,
            textColor=deep_emerald,
        ),
    )


def _paragraph(value: str, style: Any) -> Any:

    return Paragraph(value, style)


def _letterhead(
    ctx: ExportDocumentContext,
    styles: _DocStyles,
    *,
    title: str,
    number_label: str,
    number: str,
) -> Any:

    labels = DOC_LABELS.get(ctx.language, DOC_LABELS["en"])
    seller_lines = [f"<b>{ctx.seller_name}</b>"]
    seller_lines.extend(
        value
        for value in (
            ctx.seller_address,
            ctx.seller_website,
            ctx.seller_email,
            ctx.seller_phone,
        )
        if value.strip()
    )
    if ctx.seller_tax_no.strip():
        seller_lines.append(f"{labels['tax_no']}: {ctx.seller_tax_no}")
    heading = _paragraph(
        f"{title}<br/><font size=8 color='#6b7280'>{number_label}: <b>{number}</b></font>",
        styles.title,
    )
    table = Table(
        [[_paragraph("<br/>".join(seller_lines), styles.small), heading]],
        colWidths=[100 * mm, 80 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1), 1.4, styles.emerald),
            ]
        )
    )
    return table


def _parties(ctx: ExportDocumentContext, styles: _DocStyles) -> Any:

    labels = DOC_LABELS.get(ctx.language, DOC_LABELS["en"])
    seller_lines = [f"<b>{ctx.seller_name}</b>"]
    seller_lines.extend(
        value for value in (ctx.seller_address, ctx.seller_phone, ctx.seller_email) if value.strip()
    )
    buyer_lines = [f"<b>{ctx.buyer_name}</b>"]
    buyer_lines.extend(
        value
        for value in (ctx.buyer_contact, ctx.buyer_email, ctx.buyer_phone, ctx.buyer_country)
        if value.strip() and value.strip() != ctx.buyer_name.strip()
    )
    table = Table(
        [
            [_paragraph(labels["seller"], styles.head), _paragraph(labels["buyer"], styles.head)],
            [
                _paragraph("<br/>".join(seller_lines), styles.cell),
                _paragraph("<br/>".join(buyer_lines), styles.cell),
            ],
        ],
        colWidths=[90 * mm, 90 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, styles.hairline),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ecfdf5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _meta_table(styles: _DocStyles, pairs: Sequence[tuple[str, str]]) -> Any:

    cells = [
        [_paragraph(label, styles.cell), _paragraph(value, styles.cell)] for label, value in pairs
    ]
    rows: list[list[Any]] = []
    for index in range(0, len(cells), 2):
        left = cells[index]
        right = cells[index + 1] if index + 1 < len(cells) else [_paragraph("", styles.cell)] * 2
        rows.append([left[0], left[1], right[0], right[1]])
    table = Table(rows, colWidths=[32 * mm, 58 * mm, 32 * mm, 58 * mm])
    table.setStyle(
        TableStyle(
            [
                ("TEXTCOLOR", (0, 0), (0, -1), styles.muted),
                ("TEXTCOLOR", (2, 0), (2, -1), styles.muted),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, styles.hairline),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _signature_block(ctx: ExportDocumentContext, styles: _DocStyles) -> Any:

    labels = DOC_LABELS.get(ctx.language, DOC_LABELS["en"])
    stamp = ctx.footer_signature_title.strip() or labels["signature"]
    table = Table(
        [
            [_paragraph(labels["declaration"], styles.body), ""],
            ["", ""],
            [
                _paragraph(f"<b>{labels['seller_signature']}</b><br/>{stamp}", styles.small),
                _paragraph(f"<b>{labels['buyer']}</b>", styles.small),
            ],
        ],
        colWidths=[90 * mm, 90 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (-1, 0)),
                ("LINEABOVE", (0, 2), (0, 2), 0.6, styles.muted),
                ("LINEABOVE", (1, 2), (1, 2), 0.6, styles.muted),
                ("VALIGN", (0, 2), (-1, 2), "BOTTOM"),
                ("TOPPADDING", (0, 2), (-1, 2), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _document_buffer(story: list[object], *, title: str, author: str) -> bytes:

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=title,
        author=author,
    )
    doc.build(story)
    return bytes(buffer.getvalue())


def render_commercial_invoice_pdf(ctx: ExportDocumentContext) -> bytes:
    """Commercial invoice: letterhead, buyer block, HS codes, amount summary, T/T box, stamp."""

    labels = DOC_LABELS.get(ctx.language, DOC_LABELS["en"])
    styles = _build_styles(ctx.language)

    item_rows: list[list[Any]] = [
        [
            _paragraph(labels["sku"], styles.head),
            _paragraph(labels["description"], styles.head),
            _paragraph(labels["hs_code"], styles.head),
            _paragraph(labels["qty"], styles.head),
            _paragraph(labels["unit_price"], styles.head),
            _paragraph(labels["line_total"], styles.head),
        ]
    ]
    for line in ctx.lines:
        item_rows.append(
            [
                _paragraph(line.sku, styles.cell),
                _paragraph(line.description, styles.cell),
                _paragraph(line.hs_code or "-", styles.cell),
                _paragraph(str(line.quantity), styles.cell),
                _paragraph(f"{ctx.currency} {line.unit_price:.4f}", styles.cell),
                _paragraph(f"{ctx.currency} {line.line_total:.2f}", styles.cell),
            ]
        )
    items_table = Table(
        item_rows,
        colWidths=[32 * mm, 48 * mm, 24 * mm, 14 * mm, 30 * mm, 32 * mm],
        repeatRows=1,
    )
    items_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (3, 1), (5, -1), "RIGHT"),
            ]
        )
    )

    total_style = styles.section
    summary_rows = [
        [
            _paragraph(labels["subtotal"], styles.cell),
            _paragraph(f"{ctx.currency} {ctx.subtotal_amount:.2f}", styles.cell),
        ],
        [
            _paragraph(labels["shipping"], styles.cell),
            _paragraph(f"{ctx.currency} {ctx.shipping_charged:.2f}", styles.cell),
        ],
        [
            _paragraph(labels["discount"], styles.cell),
            _paragraph(f"{ctx.currency} {ctx.discount_amount:.2f}", styles.cell),
        ],
        [
            _paragraph(labels["tax"], styles.cell),
            _paragraph(f"{ctx.currency} {ctx.tax_collected:.2f}", styles.cell),
        ],
        [
            _paragraph(labels["grand_total"], total_style),
            _paragraph(f"{ctx.currency} {ctx.total_amount:.2f}", total_style),
        ],
    ]
    summary_table = Table(summary_rows, colWidths=[34 * mm, 46 * mm], hAlign="RIGHT")
    summary_table.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -2), 0.3, styles.hairline),
                ("LINEABOVE", (0, -1), (-1, -1), 1.1, styles.deep_emerald),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    bank_pairs = [
        (labels["beneficiary"], ctx.bank_beneficiary_name),
        (labels["bank_name"], ctx.bank_name),
        (labels["account_no"], ctx.bank_account_no),
        (labels["swift"], ctx.bank_swift_code),
        (labels["bank_address"], ctx.bank_address),
    ]
    bank_rows: list[list[Any]] = [[_paragraph(labels["bank_info"], styles.section), ""]]
    bank_rows.extend(
        [_paragraph(label, styles.cell), _paragraph(value, styles.cell)]
        for label, value in bank_pairs
        if value.strip()
    )

    story: list[object] = [
        _letterhead(
            ctx,
            styles,
            title=labels["ci_title"],
            number_label=labels["invoice_no"],
            number=ctx.invoice_no,
        ),
        Spacer(1, 4 * mm),
        _parties(ctx, styles),
        Spacer(1, 3 * mm),
        _meta_table(
            styles,
            [
                (labels["order_no"], ctx.order_no),
                (labels["issue_date"], ctx.issued_at.strftime("%Y-%m-%d")),
                (labels["currency"], ctx.currency),
                (labels["incoterm"], ctx.incoterm or "-"),
            ],
        ),
        Spacer(1, 4 * mm),
        items_table,
        Spacer(1, 2 * mm),
        summary_table,
    ]
    if len(bank_rows) > 1:
        bank_table = Table(bank_rows, colWidths=[40 * mm, 140 * mm])
        bank_table.setStyle(
            TableStyle(
                [
                    ("SPAN", (0, 0), (-1, 0)),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ecfdf5")),
                    ("BOX", (0, 0), (-1, -1), 0.5, styles.deep_emerald),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.extend([Spacer(1, 4 * mm), KeepTogether([bank_table])])
    story.extend(
        [Spacer(1, 6 * mm), KeepTogether([_signature_block(ctx, styles)])]
    )
    return _document_buffer(
        story, title=f"{labels['ci_title']} {ctx.invoice_no}", author=ctx.seller_name
    )


def render_packing_list_pdf(ctx: ExportDocumentContext) -> bytes:
    """Packing list: cartons, net/gross weight, CBM per SKU plus a grand-total row."""

    labels = DOC_LABELS.get(ctx.language, DOC_LABELS["en"])
    styles = _build_styles(ctx.language)

    item_rows: list[list[Any]] = [
        [
            _paragraph(labels["sku"], styles.head),
            _paragraph(labels["description"], styles.head),
            _paragraph(labels["qty"], styles.head),
            _paragraph(labels["units_per_carton"], styles.head),
            _paragraph(labels["cartons"], styles.head),
            _paragraph(labels["net_weight"], styles.head),
            _paragraph(labels["gross_weight"], styles.head),
            _paragraph(labels["volume"], styles.head),
        ]
    ]
    for line in ctx.lines:
        item_rows.append(
            [
                _paragraph(line.sku, styles.cell),
                _paragraph(line.description, styles.cell),
                _paragraph(str(line.quantity), styles.cell),
                _paragraph(
                    str(line.units_per_carton) if line.units_per_carton else "-", styles.cell
                ),
                _paragraph(str(line.cartons) if line.cartons else "-", styles.cell),
                _paragraph(f"{line.net_weight_kg:.2f}", styles.cell),
                _paragraph(f"{line.gross_weight_kg:.2f}", styles.cell),
                _paragraph(f"{line.volume_cbm:.4f}", styles.cell),
            ]
        )
    item_rows.append(
        [
            _paragraph(labels["total_row"], styles.section),
            _paragraph(f"{labels['total_qty']}: {ctx.total_quantity}", styles.section),
            _paragraph(str(ctx.total_quantity), styles.section),
            _paragraph("", styles.section),
            _paragraph(str(ctx.total_cartons), styles.section),
            _paragraph(f"{ctx.total_net_weight_kg:.2f}", styles.section),
            _paragraph(f"{ctx.total_gross_weight_kg:.2f}", styles.section),
            _paragraph(f"{ctx.total_volume_cbm:.4f}", styles.section),
        ]
    )

    items_table = Table(
        item_rows,
        colWidths=[32 * mm, 37 * mm, 12 * mm, 22 * mm, 15 * mm, 20 * mm, 20 * mm, 22 * mm],
        repeatRows=1,
    )
    items_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#ecfdf5")),
                ("LINEABOVE", (0, -1), (-1, -1), 1.0, styles.deep_emerald),
            ]
        )
    )

    totals_rows = [
        [labels["total_cartons"], str(ctx.total_cartons)],
        [labels["total_net_weight"], f"{ctx.total_net_weight_kg:.2f} kg"],
        [labels["total_gross_weight"], f"{ctx.total_gross_weight_kg:.2f} kg"],
        [labels["total_volume"], f"{ctx.total_volume_cbm:.4f} CBM"],
    ]
    totals_table = Table(
        [[_paragraph(label, styles.section), _paragraph(value, styles.section)]
         for label, value in totals_rows],
        colWidths=[120 * mm, 60 * mm],
        hAlign="RIGHT",
    )
    totals_table.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, styles.hairline),
                ("BOX", (0, 0), (-1, -1), 0.5, styles.deep_emerald),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f9fafb")),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    marks_rows: list[list[Any]] = [[_paragraph(labels["shipping_marks"], styles.section)]]
    for text in (
        f"{labels['buyer']}: {ctx.buyer_name}",
        f"{labels['order_no']}: {ctx.order_no}",
        f"{labels['packing_no']}: {ctx.packing_list_no}",
        f"{labels['total_cartons']}: {ctx.total_cartons}",
    ):
        marks_rows.append([_paragraph(text, styles.cell)])
    marks_table = Table(marks_rows, colWidths=[180 * mm])
    marks_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ecfdf5")),
                ("BOX", (0, 0), (-1, -1), 0.5, styles.deep_emerald),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    story: list[object] = [
        _letterhead(
            ctx,
            styles,
            title=labels["pl_title"],
            number_label=labels["packing_no"],
            number=ctx.packing_list_no,
        ),
        Spacer(1, 4 * mm),
        _parties(ctx, styles),
        Spacer(1, 3 * mm),
        _meta_table(
            styles,
            [
                (labels["order_no"], ctx.order_no),
                (labels["issue_date"], ctx.issued_at.strftime("%Y-%m-%d")),
                (labels["currency"], ctx.currency),
                (labels["incoterm"], ctx.incoterm or "-"),
            ],
        ),
        Spacer(1, 4 * mm),
        items_table,
    ]
    if not ctx.has_packing_specs:
        story.extend([Spacer(1, 2 * mm), _paragraph(labels["no_packing_specs"], styles.body)])
    story.extend(
        [
            Spacer(1, 3 * mm),
            totals_table,
            Spacer(1, 4 * mm),
            KeepTogether([marks_table]),
            Spacer(1, 6 * mm),
            KeepTogether([_signature_block(ctx, styles)]),
        ]
    )
    return _document_buffer(
        story, title=f"{labels['pl_title']} {ctx.packing_list_no}", author=ctx.seller_name
    )
