from __future__ import annotations

import asyncio
import html
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import boto3  # type: ignore[import-untyped]


class PdfStoragePort(Protocol):
    async def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/pdf"
    ) -> str: ...
    async def get_presigned_url(self, key: str, expires_seconds: int = 3600) -> str: ...


class InMemoryPdfStorage:
    """In-memory object storage implementation for unit and integration tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes, content_type: str = "application/pdf") -> str:
        self.objects[key] = data
        return key

    async def get_presigned_url(self, key: str, expires_seconds: int = 3600) -> str:
        return f"https://storage.local/{key}?expires={expires_seconds}"


class S3PdfStorage:
    """MinIO / Cloudflare R2 S3 storage adapter backed by boto3."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        bucket: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url or os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
        self.bucket = bucket or os.getenv("S3_BUCKET", "erp-assets")
        self.access_key = access_key or os.getenv("S3_ACCESS_KEY", "minioadmin")
        self.secret_key = secret_key or os.getenv("S3_SECRET_KEY", "minioadmin")
        self.region = region or os.getenv("S3_REGION", "auto")

    def _client(self) -> Any:
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
        )

    async def put_bytes(self, key: str, data: bytes, content_type: str = "application/pdf") -> str:
        def _upload() -> None:
            client = self._client()
            client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

        await asyncio.to_thread(_upload)
        return key

    async def get_presigned_url(self, key: str, expires_seconds: int = 3600) -> str:
        def _presign() -> str:
            client = self._client()
            return str(client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_seconds,
            ))

        return str(await asyncio.to_thread(_presign))


PI_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "title": "PROFORMA INVOICE",
        "quote_title": "COMMERCIAL QUOTATION",
        "no": "PI / Quote No.",
        "date": "Issue Date",
        "valid_until": "FX & Price Valid Until",
        "seller": "Seller",
        "buyer": "Buyer",
        "incoterm": "Incoterm",
        "loading": "Port of Loading",
        "destination": "Port of Destination",
        "payment_terms": "Payment Terms",
        "sku": "SKU",
        "description": "Description",
        "qty": "Qty",
        "unit_price": "Unit Price",
        "line_total": "Line Total",
        "freight": "Freight",
        "insurance": "Insurance",
        "duty": "Estimated Duty",
        "total": "Total Amount",
        "trade_terms": "Trade & FX Terms",
        "bank_info": "Bank Wire Transfer (T/T) Information",
        "beneficiary": "Beneficiary",
        "bank_name": "Bank Name",
        "account_no": "Account No.",
        "swift": "SWIFT / BIC",
        "bank_address": "Bank Address",
        "terms_conditions": "Commercial Terms & Conditions",
        "seller_signature": "Seller Authorized Signature",
        "buyer_signature": "Buyer Confirmation",
        "seller_contact": "Seller Contact",
        "tax_no": "Tax / Reg. No.",
        "currency": "Quote Currency",
        "fx": "Locked FX Rate",
        "subtotal": "Subtotal (Goods)",
        "contact": "Contact",
        "footer_signature_title": "Authorized Signature & Company Stamp",
    },
    "es": {
        "title": "FACTURA PROFORMA",
        "quote_title": "COTIZACION COMERCIAL",
        "no": "N. de Documento",
        "date": "Fecha de Emision",
        "valid_until": "Valido Hasta (TC Bloqueado)",
        "seller": "Vendedor",
        "buyer": "Comprador",
        "incoterm": "Incoterm",
        "loading": "Puerto de Embarque",
        "destination": "Puerto de Destino",
        "payment_terms": "Condiciones de Pago",
        "sku": "SKU",
        "description": "Descripcion",
        "qty": "Cant.",
        "unit_price": "Precio Unit.",
        "line_total": "Subtotal",
        "freight": "Flete",
        "insurance": "Seguro",
        "duty": "Aranceles Est.",
        "total": "Importe Total",
        "trade_terms": "Condiciones Comerciales y de TC",
        "bank_info": "Datos Bancarios (Transferencia T/T)",
        "beneficiary": "Beneficiario",
        "bank_name": "Banco",
        "account_no": "N. de Cuenta",
        "swift": "SWIFT / BIC",
        "bank_address": "Direccion del Banco",
        "terms_conditions": "Terminos y Condiciones",
        "seller_signature": "Firma Autorizada del Vendedor",
        "buyer_signature": "Confirmacion del Comprador",
        "seller_contact": "Contacto del Vendedor",
        "tax_no": "NIF / Reg.",
        "currency": "Moneda",
        "fx": "Tipo de Cambio Bloqueado",
        "subtotal": "Subtotal (Mercancia)",
        "contact": "Contacto",
        "footer_signature_title": "Firma Autorizada y Sello de la Empresa",
    },
    "zh": {
        "title": "形式发票 (PROFORMA INVOICE)",
        "quote_title": "商业报价单 (QUOTATION)",
        "no": "单据编号",
        "date": "签发日期",
        "valid_until": "锁汇有效期至",
        "seller": "卖方",
        "buyer": "买方",
        "incoterm": "贸易术语",
        "loading": "装运港",
        "destination": "目的港",
        "payment_terms": "付款条款",
        "sku": "SKU",
        "description": "品名与规格",
        "qty": "数量",
        "unit_price": "单价",
        "line_total": "小计",
        "freight": "运费",
        "insurance": "保险费",
        "duty": "预估关税",
        "total": "总金额",
        "trade_terms": "贸易与汇率条款",
        "bank_info": "银行电汇 (T/T) 信息",
        "beneficiary": "收款人",
        "bank_name": "开户银行",
        "account_no": "银行账号",
        "swift": "SWIFT / BIC",
        "bank_address": "银行地址",
        "terms_conditions": "商务条款与条件",
        "seller_signature": "卖方授权签字及盖章",
        "buyer_signature": "买方确认签收",
        "seller_contact": "卖方联系方式",
        "tax_no": "税号 / 注册号",
        "currency": "结算币种",
        "fx": "锁定汇率",
        "subtotal": "货值小计",
        "contact": "联系人",
        "footer_signature_title": "授权签字及公司盖章",
    },
}


@dataclass(slots=True)
class PiLineRenderItem:
    sku: str
    description: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


@dataclass(slots=True)
class PiRenderContext:
    tenant_id: UUID
    seller_name: str
    buyer_company: str
    buyer_contact: str
    buyer_email: str | None
    quote_no: str
    version: int
    is_proforma_invoice: bool
    incoterm: str
    port_of_loading: str
    port_of_destination: str
    payment_terms: str
    quote_currency: str
    fx_rate_snapshot: Decimal
    fx_locked_until: datetime
    issued_at: datetime
    freight_fee: Decimal
    insurance_fee: Decimal
    customs_duty_estimate: Decimal
    total_amount: Decimal
    items: list[PiLineRenderItem]
    language: str = "en"
    # Optional tenant letterhead / bank wire / terms block (all default empty for
    # backwards compatibility with existing callers).
    seller_address: str = ""
    seller_phone: str = ""
    seller_email: str = ""
    seller_website: str = ""
    seller_tax_no: str = ""
    bank_beneficiary_name: str = ""
    bank_name: str = ""
    bank_account_no: str = ""
    bank_swift_code: str = ""
    bank_address: str = ""
    terms_notes: str = ""
    footer_signature_title: str = ""


def _optional_row(label: str, value: str) -> str:
    """Render a labelled metadata row only when the tenant actually configured a value."""
    if not value.strip():
        return ""
    return f"<tr><td class=\"k\">{html.escape(label)}</td><td>{html.escape(value)}</td></tr>"


def build_pi_html(ctx: PiRenderContext) -> str:
    labels = PI_LABELS.get(ctx.language, PI_LABELS["en"])
    heading = labels["title"] if ctx.is_proforma_invoice else labels["quote_title"]
    esc = html.escape

    rows_html = []
    for item in ctx.items:
        rows_html.append(
            f"""
            <tr>
                <td class="mono">{esc(item.sku)}</td>
                <td>{esc(item.description)}</td>
                <td class="num">{item.quantity}</td>
                <td class="num">{esc(ctx.quote_currency)} {item.unit_price:.4f}</td>
                <td class="num">{esc(ctx.quote_currency)} {item.line_total:.2f}</td>
            </tr>
            """
        )

    seller_letters = [f"<strong>{esc(ctx.seller_name)}</strong>"]
    if ctx.seller_address.strip():
        seller_letters.append(esc(ctx.seller_address))
    contact_bits = [
        esc(bit)
        for bit in (
            f"Tel: {ctx.seller_phone}" if ctx.seller_phone.strip() else "",
            f"Email: {ctx.seller_email}" if ctx.seller_email.strip() else "",
            f"Web: {ctx.seller_website}" if ctx.seller_website.strip() else "",
        )
        if bit
    ]
    if contact_bits:
        seller_letters.append(" &nbsp;|&nbsp; ".join(contact_bits))

    # Seller/Buyer party cells stay concise: the letterhead already carries the full block.
    seller_party = [f"<strong>{esc(ctx.seller_name)}</strong>"]
    if ctx.seller_tax_no.strip():
        seller_party.append(f"{esc(labels['tax_no'])}: {esc(ctx.seller_tax_no)}")
    if ctx.seller_email.strip():
        seller_party.append(esc(ctx.seller_email))
    if ctx.seller_address.strip():
        seller_party.append(esc(ctx.seller_address))

    buyer_letters = [f"<strong>{esc(ctx.buyer_company)}</strong>", esc(ctx.buyer_contact)]
    if ctx.buyer_email:
        buyer_letters.append(esc(ctx.buyer_email))

    trade_rows = "".join(
        [
            _optional_row(labels["incoterm"], ctx.incoterm),
            _optional_row(labels["loading"], ctx.port_of_loading),
            _optional_row(labels["destination"], ctx.port_of_destination),
            _optional_row(labels["payment_terms"], ctx.payment_terms),
            _optional_row(labels["currency"], ctx.quote_currency),
            _optional_row(labels["fx"], f"{ctx.fx_rate_snapshot:.6f}"),
            _optional_row(labels["date"], ctx.issued_at.strftime("%Y-%m-%d")),
            _optional_row(
                labels["valid_until"], ctx.fx_locked_until.strftime("%Y-%m-%d %H:%M UTC")
            ),
        ]
    )

    bank_rows = "".join(
        [
            _optional_row(labels["beneficiary"], ctx.bank_beneficiary_name),
            _optional_row(labels["bank_name"], ctx.bank_name),
            _optional_row(labels["account_no"], ctx.bank_account_no),
            _optional_row(labels["swift"], ctx.bank_swift_code),
            _optional_row(labels["bank_address"], ctx.bank_address),
        ]
    )
    bank_block = (
        f"""
  <div class="panel bank">
    <h3>{esc(labels["bank_info"])}</h3>
    <table class="kv">{bank_rows}</table>
  </div>"""
        if bank_rows
        else ""
    )

    terms_block = (
        f"""
  <div class="panel terms">
    <h3>{esc(labels["terms_conditions"])}</h3>
    <pre>{esc(ctx.terms_notes)}</pre>
  </div>"""
        if ctx.terms_notes.strip()
        else ""
    )

    signature_title = ctx.footer_signature_title.strip() or labels["footer_signature_title"]

    return f"""<!DOCTYPE html>
<html lang="{esc(ctx.language)}">
<head>
<meta charset="utf-8" />
<style>
  @page {{ size: A4; margin: 16mm; }}
  body {{
    font-family: "DejaVu Sans", Arial, sans-serif; color: #111827; font-size: 10pt;
    margin: 0; line-height: 1.45;
  }}
  .accent {{ height: 5px; background: #059669; margin-bottom: 12px; }}
  .letterhead {{ display: flex; justify-content: space-between; gap: 18px; }}
  .letterhead .seller {{ font-size: 9pt; color: #374151; }}
  .letterhead .seller strong {{ font-size: 13pt; color: #065f46; display: block; }}
  .doc-title {{ text-align: right; }}
  .doc-title .title {{ font-size: 18pt; font-weight: 700; color: #0f172a; letter-spacing: 0.4px; }}
  .doc-title .no {{ font-size: 9pt; color: #374151; margin-top: 4px; }}
  .divider {{ border-bottom: 2px solid #065f46; margin: 12px 0 14px; }}
  .parties {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; }}
  .parties th {{
    background: #ecfdf5; color: #065f46; text-align: left; padding: 6px 8px;
    font-size: 8.5pt; text-transform: uppercase; letter-spacing: 0.6px;
    border: 1px solid #d1fae5;
  }}
  .parties td {{ padding: 7px 8px; vertical-align: top; border: 1px solid #e5e7eb; }}
  .items {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; }}
  .items th {{
    background: #f3f4f6; text-align: left; padding: 7px 8px; font-size: 8.5pt;
    border: 1px solid #d1d5db; text-transform: uppercase; letter-spacing: 0.4px;
  }}
  .items td {{ padding: 7px 8px; border: 1px solid #e5e7eb; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .mono {{ font-family: "DejaVu Sans Mono", monospace; font-size: 9pt; }}
  .summary {{ width: 46%; margin-left: auto; border-collapse: collapse; margin-bottom: 14px; }}
  .summary td {{ padding: 5px 8px; border-bottom: 1px solid #e5e7eb; }}
  .summary tr.total td {{
    font-weight: 700; font-size: 11.5pt; color: #065f46;
    border-top: 2px solid #065f46; border-bottom: none;
  }}
  .panel {{
    border: 1px solid #d1d5db; border-radius: 3px; padding: 8px 10px;
    margin-bottom: 10px;
  }}
  .panel h3 {{
    margin: 0 0 6px; font-size: 9pt; color: #065f46; text-transform: uppercase;
    letter-spacing: 0.6px;
  }}
  .kv {{ width: 100%; border-collapse: collapse; }}
  .kv td {{ padding: 2px 4px; vertical-align: top; font-size: 9pt; }}
  .kv td.k {{ width: 30%; color: #6b7280; }}
  .bank {{ background: #f9fafb; }}
  .terms pre {{
    margin: 0; font-family: inherit; font-size: 8.5pt; color: #374151; white-space: pre-wrap;
  }}
  .signatures {{ width: 100%; border-collapse: collapse; margin-top: 18px; }}
  .signatures td {{
    width: 50%; padding: 26px 10px 6px; vertical-align: bottom; font-size: 8.5pt;
    color: #4b5563;
  }}
  .signatures .line {{ border-top: 1px solid #9ca3af; padding-top: 4px; }}
  .footer-note {{ margin-top: 10px; font-size: 7.5pt; color: #9ca3af; text-align: center; }}
</style>
</head>
<body>
  <div class="accent"></div>
  <div class="letterhead">
    <div class="seller">{"<br/>".join(seller_letters)}</div>
    <div class="doc-title">
      <div class="title">{esc(heading)}</div>
      <div class="no">
        {esc(labels["no"])}: <strong>{esc(ctx.quote_no)}</strong> (v{ctx.version})
      </div>
    </div>
  </div>
  <div class="divider"></div>
  <table class="meta parties">
    <tr>
      <th>{esc(labels["seller"])}</th>
      <th>{esc(labels["buyer"])}</th>
    </tr>
    <tr>
      <td>{"<br/>".join(seller_party)}</td>
      <td>{"<br/>".join(buyer_letters)}</td>
    </tr>
  </table>
  <div class="panel">
    <h3>{esc(labels["trade_terms"])}</h3>
    <table class="kv">{trade_rows}</table>
  </div>
  <table class="items">
    <thead>
      <tr>
        <th>{esc(labels["sku"])}</th>
        <th>{esc(labels["description"])}</th>
        <th class="num">{esc(labels["qty"])}</th>
        <th class="num">{esc(labels["unit_price"])}</th>
        <th class="num">{esc(labels["line_total"])}</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows_html)}
    </tbody>
  </table>
  <table class="summary">
    <tr>
      <td>{esc(labels["freight"])}</td>
      <td class="num">{esc(ctx.quote_currency)} {ctx.freight_fee:.2f}</td>
    </tr>
    <tr>
      <td>{esc(labels["insurance"])}</td>
      <td class="num">{esc(ctx.quote_currency)} {ctx.insurance_fee:.2f}</td>
    </tr>
    <tr>
      <td>{esc(labels["duty"])}</td>
      <td class="num">{esc(ctx.quote_currency)} {ctx.customs_duty_estimate:.2f}</td>
    </tr>
    <tr class="total">
      <td>{esc(labels["total"])}</td>
      <td class="num">{esc(ctx.quote_currency)} {ctx.total_amount:.2f}</td>
    </tr>
  </table>{bank_block}{terms_block}
  <table class="signatures">
    <tr>
      <td><div class="line">{esc(labels["seller_signature"])}<br/>{esc(signature_title)}</div></td>
      <td><div class="line">{esc(labels["buyer_signature"])}</div></td>
    </tr>
  </table>
  <div class="footer-note">
    {esc(ctx.seller_name)} &middot; {esc(labels["contact"])}: 
    {esc(ctx.seller_email or ctx.seller_phone or "-")}
  </div>
</body>
</html>"""


def _weasyprint_available() -> bool:
    """WeasyPrint needs native pango/cairo/gdk-pixbuf; probe before importing it."""
    try:
        import weasyprint  # type: ignore[import-not-found]  # noqa: F401
    except Exception:  # pragma: no cover - depends on host native libraries
        return False
    return True


def render_pi_pdf_reportlab(ctx: PiRenderContext) -> bytes:
    """Portable PDF backend used when WeasyPrint's native libraries are unavailable.

    Emits a boardroom-grade international export Proforma Invoice: seller letterhead with an
    emerald accent bar, structured seller/buyer/trade terms, line items, FX summary, a bank
    wire (T/T) box, commercial terms and the dual signature footer.
    """
    from io import BytesIO

    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
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

    labels = PI_LABELS.get(ctx.language, PI_LABELS["en"])
    heading = labels["title"] if ctx.is_proforma_invoice else labels["quote_title"]

    font_name = "Helvetica"
    bold_font = "Helvetica-Bold"
    if ctx.language == "zh":
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font_name = "STSong-Light"
        bold_font = "STSong-Light"

    emerald = colors.HexColor("#059669")
    deep_emerald = colors.HexColor("#065f46")
    ink = colors.HexColor("#111827")
    muted = colors.HexColor("#6b7280")
    hairline = colors.HexColor("#e5e7eb")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PiTitle", parent=styles["Heading1"], fontName=bold_font, fontSize=16, textColor=ink
    )
    subtitle_style = ParagraphStyle(
        "PiSubtitle", parent=styles["BodyText"], fontName=font_name, fontSize=8.5, textColor=muted
    )
    seller_style = ParagraphStyle(
        "PiSeller", parent=styles["BodyText"], fontName=font_name, fontSize=8.5, textColor=muted
    )
    section_style = ParagraphStyle(
        "PiSection",
        parent=styles["BodyText"],
        fontName=bold_font,
        fontSize=8.5,
        textColor=deep_emerald,
    )
    body_style = ParagraphStyle(
        "PiBody", parent=styles["BodyText"], fontName=font_name, fontSize=8.5, textColor=ink
    )
    cell_style = ParagraphStyle("PiCell", parent=body_style, fontSize=8)
    head_style = ParagraphStyle(
        "PiHead", parent=cell_style, fontName=bold_font, textColor=deep_emerald, fontSize=8
    )

    def _p(value: object, style: ParagraphStyle = cell_style) -> Paragraph:
        return Paragraph(str(value), style)

    seller_lines = [
        f"<b>{ctx.seller_name}</b>",
        ctx.seller_address,
        " | ".join(
            bit
            for bit in (
                f"{labels['contact']}: {ctx.seller_phone}" if ctx.seller_phone.strip() else "",
                ctx.seller_email,
                ctx.seller_website,
            )
            if bit
        ),
        f"{labels['tax_no']}: {ctx.seller_tax_no}" if ctx.seller_tax_no.strip() else "",
    ]
    letterhead_table = Table(
        [
            [
                _p("<br/>".join(line for line in seller_lines if line), seller_style),
                _p(
                    f"{heading}<br/><font size=8 color='#6b7280'>{labels['no']}: "
                    f"<b>{ctx.quote_no}</b> (v{ctx.version})</font>",
                    ParagraphStyle(
                        "PiHeadRight",
                        parent=title_style,
                        alignment=2,
                        fontSize=15,
                        textColor=deep_emerald,
                    ),
                ),
            ]
        ],
        colWidths=[95 * mm, 85 * mm],
    )
    letterhead_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    accent_bar = Table([[""]], colWidths=[180 * mm], rowHeights=[2.4])
    accent_bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), emerald)]))

    party_rows = [
        [_p(labels["seller"], head_style), _p(labels["buyer"], head_style)],
        [
            _p(
                "<br/>".join(
                    line
                    for line in (
                        f"<b>{ctx.seller_name}</b>",
                        ctx.seller_address,
                        ctx.seller_phone,
                        ctx.seller_email,
                    )
                    if line
                )
            ),
            _p(
                "<br/>".join(
                    line
                    for line in (
                        f"<b>{ctx.buyer_company}</b>",
                        ctx.buyer_contact,
                        ctx.buyer_email or "",
                    )
                    if line
                )
            ),
        ],
    ]
    party_table = Table(party_rows, colWidths=[90 * mm, 90 * mm])
    party_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, hairline),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ecfdf5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    trade_pairs: list[tuple[str, str]] = [
        (labels["incoterm"], ctx.incoterm),
        (labels["loading"], ctx.port_of_loading),
        (labels["destination"], ctx.port_of_destination),
        (labels["payment_terms"], ctx.payment_terms),
        (labels["currency"], ctx.quote_currency),
        (labels["fx"], f"{ctx.fx_rate_snapshot:.6f}"),
        (labels["date"], ctx.issued_at.strftime("%Y-%m-%d")),
        (labels["valid_until"], ctx.fx_locked_until.strftime("%Y-%m-%d %H:%M UTC")),
    ]
    trade_rows = [
        [_p(label), _p(value)] for label, value in trade_pairs if str(value).strip()
    ]
    trade_table = Table(
        [[_p(labels["trade_terms"], section_style), ""]]
        + [(row[0], row[1]) for row in trade_rows],
        colWidths=[45 * mm, 135 * mm],
    )
    trade_table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f9fafb")),
                ("TEXTCOLOR", (0, 1), (0, -1), muted),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 1), (-1, -2), 0.3, hairline),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOX", (0, 0), (-1, -1), 0.4, hairline),
            ]
        )
    )

    item_rows: list[list[Paragraph]] = [
        [
            _p(labels["sku"], head_style),
            _p(labels["description"], head_style),
            _p(labels["qty"], head_style),
            _p(labels["unit_price"], head_style),
            _p(labels["line_total"], head_style),
        ]
    ]
    for item in ctx.items:
        item_rows.append(
            [
                _p(item.sku),
                _p(item.description),
                _p(item.quantity),
                _p(f"{ctx.quote_currency} {item.unit_price:.4f}"),
                _p(f"{ctx.quote_currency} {item.line_total:.2f}"),
            ]
        )
    items_table = Table(item_rows, colWidths=[34 * mm, 56 * mm, 14 * mm, 36 * mm, 40 * mm])
    items_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
            ]
        )
    )

    summary_rows = [
        [_p(labels["freight"]), _p(f"{ctx.quote_currency} {ctx.freight_fee:.2f}")],
        [_p(labels["insurance"]), _p(f"{ctx.quote_currency} {ctx.insurance_fee:.2f}")],
        [_p(labels["duty"]), _p(f"{ctx.quote_currency} {ctx.customs_duty_estimate:.2f}")],
        [
            _p(labels["total"], ParagraphStyle("PiTotal", parent=cell_style, fontName=bold_font)),
            _p(
                f"{ctx.quote_currency} {ctx.total_amount:.2f}",
                ParagraphStyle(
                    "PiTotalValue",
                    parent=cell_style,
                    fontName=bold_font,
                    textColor=deep_emerald,
                ),
            ),
        ],
    ]
    summary_table = Table(summary_rows, colWidths=[120 * mm, 60 * mm], hAlign="RIGHT")
    summary_table.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -2), 0.3, hairline),
                ("LINEABOVE", (0, -1), (-1, -1), 1.1, deep_emerald),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    story: list[object] = [
        letterhead_table,
        accent_bar,
        Spacer(1, 4 * mm),
        party_table,
        Spacer(1, 3 * mm),
        trade_table,
        Spacer(1, 4 * mm),
        items_table,
        Spacer(1, 2 * mm),
        summary_table,
    ]

    bank_pairs: list[tuple[str, str]] = [
        (labels["beneficiary"], ctx.bank_beneficiary_name),
        (labels["bank_name"], ctx.bank_name),
        (labels["account_no"], ctx.bank_account_no),
        (labels["swift"], ctx.bank_swift_code),
        (labels["bank_address"], ctx.bank_address),
    ]
    bank_rows = [[_p(labels["bank_info"], section_style), ""]] + [
        [_p(label), _p(value)] for label, value in bank_pairs if str(value).strip()
    ]
    if len(bank_rows) > 1:
        bank_table = Table(bank_rows, colWidths=[45 * mm, 135 * mm])
        bank_table.setStyle(
            TableStyle(
                [
                    ("SPAN", (0, 0), (-1, 0)),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ecfdf5")),
                    ("TEXTCOLOR", (0, 1), (0, -1), muted),
                    ("BOX", (0, 0), (-1, -1), 0.5, deep_emerald),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.extend([Spacer(1, 4 * mm), KeepTogether([bank_table])])

    if ctx.terms_notes.strip():
        terms_paragraphs = [
            _p(line, body_style)
            for line in ctx.terms_notes.splitlines()
            if line.strip()
        ]
        terms_table = Table(
            [[_p(labels["terms_conditions"], section_style), ""]]
            + [[paragraph, ""] for paragraph in terms_paragraphs],
            colWidths=[45 * mm, 135 * mm],
        )
        terms_table.setStyle(
            TableStyle(
                [
                    ("SPAN", (0, 0), (-1, 0)),
                    ("SPAN", (0, 1), (-1, -1)),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f9fafb")),
                    ("BOX", (0, 0), (-1, -1), 0.4, hairline),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        story.extend([Spacer(1, 4 * mm), KeepTogether([terms_table])])

    signature_title = ctx.footer_signature_title.strip() or labels["footer_signature_title"]
    signature_table = Table(
        [
            ["", ""],
            [
                _p(f"<b>{labels['seller_signature']}</b><br/>{signature_title}", subtitle_style),
                _p(f"<b>{labels['buyer_signature']}</b>", subtitle_style),
            ],
        ],
        colWidths=[90 * mm, 90 * mm],
    )
    signature_table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 1), (0, 1), 0.6, colors.HexColor("#9ca3af")),
                ("LINEABOVE", (1, 1), (1, 1), 0.6, colors.HexColor("#9ca3af")),
                ("VALIGN", (0, 1), (-1, 1), "BOTTOM"),
                ("TOPPADDING", (0, 1), (-1, 1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.extend([Spacer(1, 10 * mm), KeepTogether([signature_table])])

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"{heading} {ctx.quote_no} v{ctx.version}",
        author=ctx.seller_name,
    )
    doc.build(story)
    return bytes(buffer.getvalue())


class ProformaInvoicePdfRenderer:
    """Proforma Invoice PDF renderer with MinIO/R2 persistence.

    WeasyPrint renders the HTML template when its native libraries are present; otherwise
    the reportlab backend produces the same document. Both emit real `%PDF-` bytes.
    """

    def __init__(self, storage: PdfStoragePort | None = None) -> None:
        self.storage: PdfStoragePort = storage or S3PdfStorage()

    @staticmethod
    def render_pdf_bytes(ctx: PiRenderContext) -> bytes:
        if _weasyprint_available():
            import weasyprint

            pdf_bytes = bytes(weasyprint.HTML(string=build_pi_html(ctx)).write_pdf())
            if not pdf_bytes:
                raise RuntimeError("WeasyPrint produced empty PDF bytes")
            return pdf_bytes
        pdf_bytes = render_pi_pdf_reportlab(ctx)
        if not pdf_bytes:
            raise RuntimeError("reportlab produced empty PDF bytes")
        return pdf_bytes

    async def render_and_upload(self, ctx: PiRenderContext) -> tuple[str, str]:
        pdf_bytes = await asyncio.to_thread(self.render_pdf_bytes, ctx)
        object_key = f"tenants/{ctx.tenant_id}/quotes/{ctx.quote_no}-v{ctx.version}.pdf"
        await self.storage.put_bytes(object_key, pdf_bytes, content_type="application/pdf")
        url = await self.storage.get_presigned_url(object_key)
        return object_key, url
