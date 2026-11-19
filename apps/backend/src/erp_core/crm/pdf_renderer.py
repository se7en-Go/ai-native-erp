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


def build_pi_html(ctx: PiRenderContext) -> str:
    labels = PI_LABELS.get(ctx.language, PI_LABELS["en"])
    heading = labels["title"] if ctx.is_proforma_invoice else labels["quote_title"]
    rows_html = []
    for item in ctx.items:
        rows_html.append(
            f"""
            <tr>
                <td>{html.escape(item.sku)}</td>
                <td>{html.escape(item.description)}</td>
                <td class="num">{item.quantity}</td>
                <td class="num">
        {html.escape(ctx.quote_currency)} {item.unit_price:.4f}
      </td>
                <td class="num">{html.escape(ctx.quote_currency)} {item.line_total:.2f}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="{html.escape(ctx.language)}">
<head>
<meta charset="utf-8" />
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: "DejaVu Sans", Arial, sans-serif; color: #111827; font-size: 10pt; }}
  .header {{{{
    display: flex; justify-content: space-between;
    border-bottom: 2px solid #1f2937; padding-bottom: 10px; margin-bottom: 16px;
  }}}}
  .title {{ font-size: 18pt; font-weight: 700; color: #0f172a; }}
  .meta {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
  .meta td {{ padding: 4px 8px; vertical-align: top; border: 1px solid #e5e7eb; }}
  .items {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
  .items th {{ background: #f3f4f6; text-align: left; padding: 8px; border: 1px solid #d1d5db; }}
  .items td {{ padding: 8px; border: 1px solid #e5e7eb; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .summary {{ width: 45%; margin-left: auto; border-collapse: collapse; }}
  .summary td {{ padding: 6px 8px; border-bottom: 1px solid #e5e7eb; }}
  .summary tr.total td {{ font-weight: 700; font-size: 11pt; border-top: 2px solid #111827; }}
</style>
</head>
<body>
  <div class="header">
    <div>
      <div class="title">{html.escape(heading)}</div>
      <div>
        {html.escape(labels["no"])}:
        <strong>{html.escape(ctx.quote_no)} v{ctx.version}</strong>
      </div>
    </div>
    <div>
      <div>{html.escape(labels["date"])}: {ctx.issued_at:%Y-%m-%d}</div>
      <div>{html.escape(labels["valid_until"])}: {ctx.fx_locked_until:%Y-%m-%d %H:%M UTC}</div>
    </div>
  </div>
  <table class="meta">
    <tr>
      <td>
        <strong>{html.escape(labels["seller"])}</strong><br/>{html.escape(ctx.seller_name)}
      </td>
      <td>
        <strong>{html.escape(labels["buyer"])}</strong><br/>
        {html.escape(ctx.buyer_company)}<br/>
        {html.escape(ctx.buyer_contact)} {html.escape(ctx.buyer_email or "")}
      </td>
    </tr>
    <tr>
      <td>
        <strong>{html.escape(labels["incoterm"])}:</strong> {html.escape(ctx.incoterm)}<br/>
        <strong>{html.escape(labels["loading"])}:</strong> {html.escape(ctx.port_of_loading)}
      </td>
      <td>
        <strong>{html.escape(labels["destination"])}:</strong>
        {html.escape(ctx.port_of_destination)}<br/>
        <strong>{html.escape(labels["payment_terms"])}:</strong>
        {html.escape(ctx.payment_terms)}
      </td>
    </tr>
  </table>
  <table class="items">
    <thead>
      <tr>
        <th>{html.escape(labels["sku"])}</th>
        <th>{html.escape(labels["description"])}</th>
        <th class="num">{html.escape(labels["qty"])}</th>
        <th class="num">{html.escape(labels["unit_price"])}</th>
        <th class="num">{html.escape(labels["line_total"])}</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows_html)}
    </tbody>
  </table>
  <table class="summary">
    <tr>
      <td>{html.escape(labels["freight"])}</td>
      <td class="num">{html.escape(ctx.quote_currency)} {ctx.freight_fee:.2f}</td>
    </tr>
    <tr>
      <td>{html.escape(labels["insurance"])}</td>
      <td class="num">{html.escape(ctx.quote_currency)} {ctx.insurance_fee:.2f}</td>
    </tr>
    <tr>
      <td>{html.escape(labels["duty"])}</td>
      <td class="num">
        {html.escape(ctx.quote_currency)} {ctx.customs_duty_estimate:.2f}
      </td>
    </tr>
    <tr class="total">
      <td>{html.escape(labels["total"])}</td>
      <td class="num">{html.escape(ctx.quote_currency)} {ctx.total_amount:.2f}</td>
    </tr>
  </table>
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

    Renders the same Proforma Invoice layout with reportlab's built-in CID fonts, which
    cover the en / es / zh label sets without shipping a font file or requiring pango.
    """
    from io import BytesIO

    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    labels = PI_LABELS.get(ctx.language, PI_LABELS["en"])
    heading = labels["title"] if ctx.is_proforma_invoice else labels["quote_title"]

    font_name = "Helvetica"
    if ctx.language == "zh":
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font_name = "STSong-Light"

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PiTitle", parent=styles["Heading1"], fontName=font_name, fontSize=17
    )
    body_style = ParagraphStyle("PiBody", parent=styles["BodyText"], fontName=font_name, fontSize=9)
    cell_style = ParagraphStyle("PiCell", parent=body_style, fontSize=8.5)

    def _p(value: object) -> Paragraph:
        return Paragraph(str(value), cell_style)

    story: list[object] = [
        Paragraph(f"{heading} - {ctx.quote_no} v{ctx.version}", title_style),
        Spacer(1, 4 * mm),
    ]

    meta_rows = [
        [_p(labels["date"]), _p(ctx.issued_at.strftime("%Y-%m-%d"))],
        [
            _p(labels["valid_until"]),
            _p(ctx.fx_locked_until.strftime("%Y-%m-%d %H:%M UTC")),
        ],
        [_p(labels["seller"]), _p(ctx.seller_name)],
        [_p(labels["buyer"]), _p(f"{ctx.buyer_company} / {ctx.buyer_contact}")],
        [
            _p(labels["incoterm"]),
            _p(f"{ctx.incoterm} | {labels['loading']}: {ctx.port_of_loading}"),
        ],
        [_p(labels["destination"]), _p(ctx.port_of_destination)],
        [_p(labels["payment_terms"]), _p(ctx.payment_terms)],
        [_p("FX"), _p(f"{ctx.fx_rate_snapshot} ({ctx.quote_currency})")],
    ]
    meta_table = Table(meta_rows, colWidths=[45 * mm, 125 * mm])
    meta_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e7eb")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f9fafb")),
            ]
        )
    )
    story.extend([meta_table, Spacer(1, 5 * mm)])

    item_rows = [
        [
            _p(labels["sku"]),
            _p(labels["description"]),
            _p(labels["qty"]),
            _p(labels["unit_price"]),
            _p(labels["line_total"]),
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
    items_table = Table(item_rows, colWidths=[28 * mm, 62 * mm, 16 * mm, 32 * mm, 32 * mm])
    items_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.extend([items_table, Spacer(1, 5 * mm)])

    summary_rows = [
        [_p(labels["freight"]), _p(f"{ctx.quote_currency} {ctx.freight_fee:.2f}")],
        [_p(labels["insurance"]), _p(f"{ctx.quote_currency} {ctx.insurance_fee:.2f}")],
        [_p(labels["duty"]), _p(f"{ctx.quote_currency} {ctx.customs_duty_estimate:.2f}")],
        [_p(labels["total"]), _p(f"{ctx.quote_currency} {ctx.total_amount:.2f}")],
    ]
    summary_table = Table(summary_rows, colWidths=[120 * mm, 50 * mm], hAlign="RIGHT")
    summary_table.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#e5e7eb")),
                ("LINEABOVE", (0, -1), (-1, -1), 1.1, colors.HexColor("#111827")),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ]
        )
    )
    story.append(summary_table)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"{heading} {ctx.quote_no} v{ctx.version}",
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
