"""Sourcing AI rewrite contract owned by ``erp_core.catalog``.

``infra/importlinter.ini`` forbids ``erp_core.catalog`` from importing
``erp_core.ai_engine`` and ``erp_core.channels`` (contracts ``catalog_isolated`` and
``domain_modules_never_import_ai_engine``). This module therefore declares *only* the
plain DTOs, the platform/language merchandising rules and a fully deterministic
localized copywriter, so the concrete LLM-backed adapter can live in
``erp_core.ai_engine.sourcing_copy`` and be injected through ``app.state`` exactly like
``MarketplaceListingPublisher`` is.

The deterministic path is not a placeholder: it produces real localized titles, five
platform-shaped bullets and a description in all twelve supported languages, and it
re-uses the payload's exact ``Decimal`` retail price and MOQ ladder numbers (never
recomputing, rounding or inventing money). It is what a tenant sees when no BYOK or
platform LLM key is configured, and it costs zero credits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CHANNEL_DISPLAY_NAMES",
    "DEFAULT_LANGUAGE_BY_CHANNEL",
    "PLATFORM_COPY_GUIDELINES",
    "SUPPORTED_REWRITE_LANGUAGES",
    "SourcingAiRewriteRequest",
    "SourcingCopyRewriteInput",
    "SourcingCopyRewriteOutput",
    "SourcingCopyRewriter",
    "SourcingRewriteTone",
    "build_deterministic_localized_copy",
]

SUPPORTED_REWRITE_LANGUAGES: dict[str, str] = {
    "en": "English",
    "zh": "Chinese",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ms": "Malay",
    "fr": "French",
    "de": "German",
    "ja": "Japanese",
}

DEFAULT_LANGUAGE_BY_CHANNEL: dict[str, str] = {
    "shopify": "en",
    "amazon_sp": "en",
    "ebay": "en",
    "walmart": "en",
    "woocommerce": "en",
    "shopline": "en",
    "shoplazza": "en",
    "alibaba_email": "en",
    "tiktok_shop": "en",
    "temu": "en",
    "shein": "en",
    "aliexpress": "en",
    "shopee": "th",
    "lazada": "ms",
    "ozon": "ru",
    "mercado_libre": "es",
}

#: Human-facing channel names used inside generated copy (proper nouns stay untranslated).
CHANNEL_DISPLAY_NAMES: dict[str, str] = {
    "shopify": "Shopify",
    "amazon_sp": "Amazon",
    "ebay": "eBay",
    "walmart": "Walmart",
    "woocommerce": "WooCommerce",
    "shopline": "SHOPLINE",
    "shoplazza": "Shoplazza",
    "alibaba_email": "Alibaba.com",
    "tiktok_shop": "TikTok Shop",
    "temu": "Temu",
    "shein": "SHEIN",
    "aliexpress": "AliExpress",
    "shopee": "Shopee",
    "lazada": "Lazada",
    "ozon": "Ozon",
    "mercado_libre": "Mercado Libre",
}

PLATFORM_COPY_GUIDELINES: dict[str, str] = {
    "shopify": (
        "DTC brand storytelling: lead with the customer outcome, keep the title human and "
        "brandable (<=70 characters), then five benefit-led bullets (outcome, build quality, "
        "specification, care/use, shipping), and a description written as short brand prose. "
        "Sell the lifestyle, never keyword-stuff; no emoji, no ALL CAPS."
    ),
    "amazon_sp": (
        "Amazon SEO: a keyword-dense title (brand + product type + top attributes + size, "
        "<=200 characters, no promotional words), exactly five benefit-first bullets of "
        "roughly 150-220 characters that begin with the buyer benefit in caps-free sentence "
        "case, and a description that repeats the highest-value search terms naturally. "
        "Never state price, MOQ, warranty duration or certification unless it is in context."
    ),
    "ebay": (
        "eBay US spec-forward retail: factual title (<=80 characters) with model, size and "
        "key specification, five compact bullets covering compatibility, build material, "
        "dimensions, included items and shipping expectations. No marketing fluff, no pricing."
    ),
    "walmart": (
        "Walmart US retail: clean title (<=100 characters) with product type plus attributes, "
        "five short scannable bullets that front-load measurable specifications, and a "
        "description in plain declarative sentences. Walmart rejects subjective superlatives."
    ),
    "woocommerce": (
        "WooCommerce DTC storefront: flexible brand title, five bullets mixing emotional and "
        "specification detail, description with short paragraphs and scannable sub-points, and "
        "SEO keywords that match the store's collection naming."
    ),
    "shopline": (
        "SHOPLINE DTC storefront: campaign-ready title, five benefit bullets for social traffic "
        "landing pages, description built for mobile reading (short lines, first sentence sells "
        "the outcome), local promotional keyword set."
    ),
    "shoplazza": (
        "Shoplazza DTC storefront: conversion-first title, five bullets covering outcome, "
        "specification, use case, packaging and after-sales, description that answers the three "
        "objections a first-time buyer has."
    ),
    "alibaba_email": (
        "Alibaba.com international B2B: a title that names product type and OEM capability, "
        "five bullets that must read as a quotation sheet (OEM/ODM, MOQ ladder with exact "
        "quantities and unit prices, certifications such as CE/RoHS, Incoterms FOB/DDP, lead "
        "time, packaging), and a description written as a supplier capability statement. "
        "Never invent certifications, factory size or lead times."
    ),
    "tiktok_shop": (
        "TikTok Shop: a scroll-stopping hook in the first bullet, ultra-short sentences, "
        "benefit before specification, five bullets total, description that reads like a "
        "creator script with a clear call to action. No hashtags, no emoji spam, no price claims."
    ),
    "temu": (
        "Temu ultra-concise value listing: title <=60 characters naming the product and the two "
        "most obvious attributes, five very short value bullets (no full sentences), description "
        "of two or three lines. Zero brand storytelling, zero long specifications."
    ),
    "shein": (
        "SHEIN ultra-concise fashion/value listing: title <=60 characters, five punchy bullets "
        "with material and fit/size cues first, description kept to three short lines, "
        "style-oriented but never misleading about material or certification."
    ),
    "aliexpress": (
        "AliExpress value listing: title <=128 characters combining product type and searchable "
        "attributes, five concise bullets, description that lists what is in the box, "
        "specification table-style lines and shipping expectations. No invented guarantees."
    ),
    "shopee": (
        "Shopee SEA localized format: title <=120 characters with the top search keyword first, "
        "five short scannable bullets separated by clear line breaks, description that opens "
        "with the main benefit, then specification, then what is in the box. Use the local "
        "language and local buying habits; avoid machine-literal phrasing."
    ),
    "lazada": (
        "Lazada SEA localized format: campaign keyword in the first three words of the title, "
        "five scannable bullets, description structured as benefit / specification / package "
        "contents / warranty. Keep sentences short and use the local language register."
    ),
    "ozon": (
        "Ozon RU technical specification format: a title naming product type, model and key "
        "technical parameters, five bullets written as a technical spec list (power, voltage, "
        "protection class, materials, dimensions), and a description that leads with the "
        "specification table and closes with delivery and warranty facts from context."
    ),
    "mercado_libre": (
        "Mercado Libre LATAM (ES/PT) clarity format: a title with product type and the two most "
        "searched attributes, five bullets in plain local Spanish or Portuguese covering use, "
        "specification, contents, warranty and compatibility, and a description with a clear "
        "warranty sentence and after-sales channel. Prefer neutral LATAM vocabulary."
    ),
}

SourcingRewriteTone = Literal["conversion", "seo", "b2b_wholesale", "social_viral"]

# Copy *shape* per channel: DTC storytelling, marketplace SEO, SEA local, ultra-concise
# value, social hook, technical spec, LATAM clarity, or international B2B wholesale.
_PLATFORM_STYLE: dict[str, str] = {
    "shopify": "dtc_story",
    "woocommerce": "dtc_story",
    "shopline": "dtc_story",
    "shoplazza": "dtc_story",
    "amazon_sp": "marketplace_seo",
    "ebay": "marketplace_seo",
    "walmart": "marketplace_seo",
    "tiktok_shop": "social_hook",
    "temu": "value_concise",
    "shein": "value_concise",
    "aliexpress": "value_concise",
    "shopee": "sea_local",
    "lazada": "sea_local",
    "ozon": "technical_spec",
    "mercado_libre": "latam_clarity",
    "alibaba_email": "b2b_wholesale",
}

_TITLE_BUDGET: dict[str, int] = {
    "dtc_story": 200,
    "marketplace_seo": 200,
    "social_hook": 90,
    "value_concise": 70,
    "sea_local": 120,
    "technical_spec": 200,
    "latam_clarity": 160,
    "b2b_wholesale": 170,
}

_STYLE_FALLBACK = "dtc_story"


@dataclass(frozen=True, slots=True)
class _LanguagePack:
    """Every localized string the deterministic copywriter needs for one language."""

    code: str
    display_name: str
    word_joiner: str
    label_sep: str
    list_separator: str
    ladder_separator: str
    hooks: dict[str, str]
    closings: dict[str, str]
    feature: str
    price: str
    moq: str
    ladder_entry: str
    ladder_fallback: str
    description: str
    attributes_fallback: str
    hs_line: str
    keywords: tuple[str, ...]


_LANGUAGE_PACKS: dict[str, _LanguagePack] = {
    "en": _LanguagePack(
        code="en",
        display_name="English",
        word_joiner=" ",
        label_sep=": ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "Brand-ready hero product",
            "marketplace_seo": "Search-optimised listing",
            "social_hook": "Stop-the-scroll launch pick",
            "value_concise": "Straight-to-the-point value pick",
            "sea_local": "Everyday best-value pick",
            "technical_spec": "Spec-first professional model",
            "latam_clarity": "Clear specs, confident purchase",
            "b2b_wholesale": "Factory-direct wholesale offer",
        },
        closings={
            "dtc_story": (
                "Private-label ready with custom packaging and a 12-month warranty"
            ),
            "marketplace_seo": (
                "QC inspected before shipment and covered by a 12-month warranty"
            ),
            "social_hook": "Ready stock for your next drop, shipped from a vetted factory",
            "value_concise": "Sturdy build, ready to ship, samples without a minimum order",
            "sea_local": "Ready stock with nationwide delivery and a 12-month warranty",
            "technical_spec": (
                "Full specification sheet, customs documents and a 12-month warranty included"
            ),
            "latam_clarity": "12-month warranty with clear after-sales support",
            "b2b_wholesale": (
                "OEM/ODM private label, custom packaging, CE/RoHS documents, FOB and DDP terms"
            ),
        },
        feature="{category} with {attributes}",
        price="Retail price {price} (USD)",
        moq="Volume ladder: {ladder}",
        ladder_entry="{qty} pcs at {price}",
        ladder_fallback="volume pricing quoted on request",
        description=(
            "{title}. Factory-direct {category} for {channel} buyers. Key specifications: "
            "{attributes}. Retail price {price}; volume pricing {ladder}. {closing}"
        ),
        attributes_fallback="export-grade build",
        hs_line="HS code {hs}.",
        keywords=("wholesale", "factory direct", "export quality"),
    ),
    "zh": _LanguagePack(
        code="zh",
        display_name="Chinese",
        word_joiner="",
        label_sep="：",
        list_separator="、",
        ladder_separator="、",
        hooks={
            "dtc_story": "品牌主推爆款",
            "marketplace_seo": "搜索优化上架首选",
            "social_hook": "刷屏级上新款",
            "value_concise": "高性价比直供款",
            "sea_local": "日常热销实惠款",
            "technical_spec": "参数优先的专业款",
            "latam_clarity": "规格清晰放心购",
            "b2b_wholesale": "工厂直供批发报价",
        },
        closings={
            "dtc_story": "支持贴牌与包装定制，整机质保 12 个月",
            "marketplace_seo": "出货前全检，整机质保 12 个月",
            "social_hook": "现货充足，可直接上架出货",
            "value_concise": "做工扎实，现货速发，支持先拿样",
            "sea_local": "现货充足，全国发货，整机质保 12 个月",
            "technical_spec": "提供完整规格书、报关资料与 12 个月质保",
            "latam_clarity": "12 个月质保，并提供本地语言售后支持",
            "b2b_wholesale": (
                "支持 OEM/ODM 贴牌、包装定制、CE/RoHS 认证资料，可报 FOB 与 DDP"
            ),
        },
        feature="{attributes}的{category}",
        price="零售价 {price}（美元）",
        moq="批量阶梯价：{ladder}",
        ladder_entry="{qty} 件 {price}",
        ladder_fallback="批量价格可另询",
        description=(
            "{title}。面向{channel}买家的工厂直供{category}。核心规格：{attributes}。"
            "零售价 {price}；批量价格 {ladder}。{closing}"
        ),
        attributes_fallback="出口级做工",
        hs_line="海关编码 {hs}。",
        keywords=("批发", "工厂直供", "出口品质"),
    ),
    "es": _LanguagePack(
        code="es",
        display_name="Spanish",
        word_joiner=" ",
        label_sep=": ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "Producto estrella para tu marca",
            "marketplace_seo": "Ficha optimizada para buscar y convertir",
            "social_hook": "Lanzamiento que frena el scroll",
            "value_concise": "Valor directo, sin rodeos",
            "sea_local": "El favorito de precio diario",
            "technical_spec": "Modelo profesional con especificaciones primero",
            "latam_clarity": "Especificaciones claras, compra segura",
            "b2b_wholesale": "Oferta mayorista directa de fábrica",
        },
        closings={
            "dtc_story": (
                "Listo para marca propia con embalaje personalizado y 12 meses de garantía"
            ),
            "marketplace_seo": "Inspeccionado antes del envío y con 12 meses de garantía",
            "social_hook": (
                "Stock listo para tu próxima campaña, enviado desde una fábrica verificada"
            ),
            "value_concise": "Construcción robusta, envío inmediato y muestra sin mínimo",
            "sea_local": "Stock disponible, entrega nacional y 12 meses de garantía",
            "technical_spec": (
                "Hoja de especificaciones completa, documentación HS y 12 meses de garantía"
            ),
            "latam_clarity": "12 meses de garantía y soporte posventa en tu idioma",
            "b2b_wholesale": (
                "Marca privada OEM/ODM, embalaje personalizado, certificados CE/RoHS y "
                "términos FOB y DDP"
            ),
        },
        feature="{category} con {attributes}",
        price="Precio de venta {price} (USD)",
        moq="Escala por volumen: {ladder}",
        ladder_entry="{qty} uds. a {price}",
        ladder_fallback="precio por volumen a consulta",
        description=(
            "{title}. {category} directo de fábrica para compradores de {channel}. "
            "Especificaciones clave: {attributes}. Precio de venta {price}; precios por "
            "volumen {ladder}. {closing}"
        ),
        attributes_fallback="calidad de exportación",
        hs_line="Código HS {hs}.",
        keywords=("mayoreo", "directo de fábrica", "calidad de exportación"),
    ),
    "pt": _LanguagePack(
        code="pt",
        display_name="Portuguese",
        word_joiner=" ",
        label_sep=": ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "Produto estrela para a sua marca",
            "marketplace_seo": "Anúncio otimizado para buscar e vender",
            "social_hook": "Lançamento que para o scroll",
            "value_concise": "Valor direto, sem enrolação",
            "sea_local": "O favorito de preço do dia a dia",
            "technical_spec": "Modelo profissional com ficha técnica primeiro",
            "latam_clarity": "Especificações claras, compra segura",
            "b2b_wholesale": "Oferta atacadista direto da fábrica",
        },
        closings={
            "dtc_story": (
                "Pronto para marca própria com embalagem personalizada e 12 meses de garantia"
            ),
            "marketplace_seo": "Inspecionado antes do envio e com 12 meses de garantia",
            "social_hook": (
                "Estoque pronto para o seu próximo lançamento, enviado de fábrica verificada"
            ),
            "value_concise": "Construção robusta, envio imediato e amostra sem mínimo",
            "sea_local": "Estoque disponível, entrega nacional e 12 meses de garantia",
            "technical_spec": (
                "Ficha técnica completa, documentação HS e 12 meses de garantia"
            ),
            "latam_clarity": "12 meses de garantia e suporte pós-venda no seu idioma",
            "b2b_wholesale": (
                "Marca própria OEM/ODM, embalagem personalizada, certificados CE/RoHS e "
                "condições FOB e DDP"
            ),
        },
        feature="{category} com {attributes}",
        price="Preço de venda {price} (USD)",
        moq="Escala por volume: {ladder}",
        ladder_entry="{qty} un. a {price}",
        ladder_fallback="preço por volume sob consulta",
        description=(
            "{title}. {category} direto da fábrica para compradores da {channel}. "
            "Especificações principais: {attributes}. Preço de venda {price}; preços por "
            "volume {ladder}. {closing}"
        ),
        attributes_fallback="qualidade de exportação",
        hs_line="Código HS {hs}.",
        keywords=("atacado", "direto da fábrica", "qualidade de exportação"),
    ),
    "ru": _LanguagePack(
        code="ru",
        display_name="Russian",
        word_joiner=" ",
        label_sep=": ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "Хит для вашего бренда",
            "marketplace_seo": "Карточка, оптимизированная под поиск",
            "social_hook": "Новинка, которая останавливает ленту",
            "value_concise": "Прямая выгода без лишних слов",
            "sea_local": "Практичный выбор на каждый день",
            "technical_spec": "Профессиональная модель: сначала характеристики",
            "latam_clarity": "Понятные характеристики, уверенная покупка",
            "b2b_wholesale": "Опт напрямую с завода",
        },
        closings={
            "dtc_story": (
                "Готово к продаже под вашим брендом: своя упаковка и гарантия 12 месяцев"
            ),
            "marketplace_seo": "Проверка качества перед отгрузкой и гарантия 12 месяцев",
            "social_hook": (
                "Товар в наличии для следующего запуска, отгрузка с проверенного завода"
            ),
            "value_concise": "Прочная конструкция, быстрая отгрузка, образец без минимума",
            "sea_local": "В наличии, доставка по стране, гарантия 12 месяцев",
            "technical_spec": (
                "Полный паспорт изделия, документы по коду ТН ВЭД и гарантия 12 месяцев"
            ),
            "latam_clarity": "Гарантия 12 месяцев и поддержка после продажи",
            "b2b_wholesale": (
                "OEM/ODM под вашим брендом, своя упаковка, документы CE/RoHS, "
                "условия FOB и DDP"
            ),
        },
        feature="{category}: {attributes}",
        price="Розничная цена {price} (USD)",
        moq="Шкала по объёму: {ladder}",
        ladder_entry="{qty} шт. по {price}",
        ladder_fallback="цена по объёму по запросу",
        description=(
            "{title}. {category} напрямую с завода для покупателей {channel}. Ключевые "
            "характеристики: {attributes}. Розничная цена {price}; цены по объёму {ladder}. "
            "{closing}"
        ),
        attributes_fallback="экспортное исполнение",
        hs_line="Код ТН ВЭД {hs}.",
        keywords=("оптом", "напрямую с завода", "экспортное качество"),
    ),
    "th": _LanguagePack(
        code="th",
        display_name="Thai",
        word_joiner=" ",
        label_sep=": ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "สินค้า hero สำหรับแบรนด์ของคุณ",
            "marketplace_seo": "Listing ที่ปรับให้ค้นหาเจอและขายได้",
            "social_hook": "สินค้าเปิดตัวที่หยุดนิ้วเลื่อน",
            "value_concise": "คุ้มค่าตรงไปตรงมา",
            "sea_local": "ตัวเลือกคุ้มค่าที่ขายดีทุกวัน",
            "technical_spec": "รุ่นมืออาชีพ เน้นสเปกก่อน",
            "latam_clarity": "สเปกชัดเจน ซื้อได้มั่นใจ",
            "b2b_wholesale": "ข้อเสนอขายส่งตรงจากโรงงาน",
        },
        closings={
            "dtc_story": "พร้อมทำแบรนด์ตัวเอง บรรจุภัณฑ์สั่งทำ และรับประกัน 12 เดือน",
            "marketplace_seo": "ตรวจคุณภาพก่อนส่ง และรับประกัน 12 เดือน",
            "social_hook": "สต็อกพร้อมส่งสำหรับแคมเปญถัดไป จากโรงงานที่ตรวจสอบแล้ว",
            "value_concise": "โครงสร้างแข็งแรง ส่งไว ขอตัวอย่างได้ไม่ขั้นต่ำ",
            "sea_local": "มีสต็อกพร้อมส่งทั่วประเทศ รับประกัน 12 เดือน",
            "technical_spec": "เอกสารสเปกครบ เอกสาร HS พร้อม และรับประกัน 12 เดือน",
            "latam_clarity": "รับประกัน 12 เดือน พร้อมบริการหลังการขาย",
            "b2b_wholesale": (
                "OEM/ODM แบรนด์ตัวเอง บรรจุภัณฑ์สั่งทำ เอกสาร CE/RoHS เงื่อนไข FOB และ DDP"
            ),
        },
        feature="{category} พร้อม{attributes}",
        price="ราคาขายปลีก {price} (USD)",
        moq="ขั้นบันไดตามปริมาณ: {ladder}",
        ladder_entry="{qty} ชิ้น ราคา {price}",
        ladder_fallback="ราคาตามปริมาณ สอบถามได้",
        description=(
            "{title} {category} ตรงจากโรงงานสำหรับผู้ซื้อ {channel} สเปกหลัก: {attributes} "
            "ราคาขายปลีก {price} ราคาตามปริมาณ {ladder} {closing}"
        ),
        attributes_fallback="งานคุณภาพเพื่อการส่งออก",
        hs_line="รหัส HS {hs}",
        keywords=("ขายส่ง", "ตรงจากโรงงาน", "คุณภาพส่งออก"),
    ),
    "vi": _LanguagePack(
        code="vi",
        display_name="Vietnamese",
        word_joiner=" ",
        label_sep=": ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "Sản phẩm chủ lực cho thương hiệu của bạn",
            "marketplace_seo": "Listing tối ưu tìm kiếm và chuyển đổi",
            "social_hook": "Hàng mới khiến người xem dừng lại",
            "value_concise": "Giá trị thẳng thắn, không vòng vo",
            "sea_local": "Lựa chọn tiết kiệm bán chạy mỗi ngày",
            "technical_spec": "Mẫu chuyên nghiệp, thông số đặt trước",
            "latam_clarity": "Thông số rõ ràng, mua an tâm",
            "b2b_wholesale": "Báo giá sỉ trực tiếp từ nhà máy",
        },
        closings={
            "dtc_story": (
                "Sẵn sàng bán dưới thương hiệu riêng, bao bì tùy chỉnh và bảo hành 12 tháng"
            ),
            "marketplace_seo": "Kiểm tra chất lượng trước khi giao và bảo hành 12 tháng",
            "social_hook": "Sẵn hàng cho chiến dịch tiếp theo, giao từ nhà máy đã thẩm định",
            "value_concise": "Kết cấu chắc chắn, giao nhanh, lấy mẫu không cần đơn tối thiểu",
            "sea_local": "Sẵn hàng, giao toàn quốc và bảo hành 12 tháng",
            "technical_spec": "Đầy đủ bảng thông số, hồ sơ mã HS và bảo hành 12 tháng",
            "latam_clarity": "Bảo hành 12 tháng và hỗ trợ sau bán hàng",
            "b2b_wholesale": (
                "OEM/ODM thương hiệu riêng, bao bì tùy chỉnh, chứng từ CE/RoHS, "
                "điều kiện FOB và DDP"
            ),
        },
        feature="{category} với {attributes}",
        price="Giá bán lẻ {price} (USD)",
        moq="Thang giá theo số lượng: {ladder}",
        ladder_entry="{qty} cái giá {price}",
        ladder_fallback="giá theo số lượng, vui lòng liên hệ",
        description=(
            "{title}. {category} trực tiếp từ nhà máy cho người mua {channel}. Thông số "
            "chính: {attributes}. Giá bán lẻ {price}; giá theo số lượng {ladder}. {closing}"
        ),
        attributes_fallback="chất lượng xuất khẩu",
        hs_line="Mã HS {hs}.",
        keywords=("bán sỉ", "trực tiếp từ nhà máy", "chất lượng xuất khẩu"),
    ),
    "id": _LanguagePack(
        code="id",
        display_name="Indonesian",
        word_joiner=" ",
        label_sep=": ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "Produk andalan untuk brand Anda",
            "marketplace_seo": "Listing yang dioptimalkan untuk pencarian",
            "social_hook": "Produk rilis yang menghentikan scroll",
            "value_concise": "Nilai langsung tanpa basa-basi",
            "sea_local": "Pilihan hemat yang laris setiap hari",
            "technical_spec": "Model profesional, spesifikasi di depan",
            "latam_clarity": "Spesifikasi jelas, beli lebih yakin",
            "b2b_wholesale": "Penawaran grosir langsung dari pabrik",
        },
        closings={
            "dtc_story": "Siap private label, kemasan custom, dan garansi 12 bulan",
            "marketplace_seo": "Diperiksa sebelum pengiriman dan bergaransi 12 bulan",
            "social_hook": "Stok siap untuk peluncuran berikutnya dari pabrik terverifikasi",
            "value_concise": "Konstruksi kokoh, kirim cepat, sampel tanpa minimum",
            "sea_local": "Stok tersedia, kirim ke seluruh negeri, garansi 12 bulan",
            "technical_spec": "Lembar spesifikasi lengkap, dokumen HS, dan garansi 12 bulan",
            "latam_clarity": "Garansi 12 bulan dan dukungan purnajual",
            "b2b_wholesale": (
                "Private label OEM/ODM, kemasan custom, dokumen CE/RoHS, syarat FOB dan DDP"
            ),
        },
        feature="{category} dengan {attributes}",
        price="Harga jual {price} (USD)",
        moq="Tangga harga volume: {ladder}",
        ladder_entry="{qty} pcs {price}",
        ladder_fallback="harga volume berdasarkan permintaan",
        description=(
            "{title}. {category} langsung dari pabrik untuk pembeli {channel}. Spesifikasi "
            "utama: {attributes}. Harga jual {price}; harga volume {ladder}. {closing}"
        ),
        attributes_fallback="kualitas ekspor",
        hs_line="Kode HS {hs}.",
        keywords=("grosir", "langsung dari pabrik", "kualitas ekspor"),
    ),
    "ms": _LanguagePack(
        code="ms",
        display_name="Malay",
        word_joiner=" ",
        label_sep=": ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "Produk utama untuk jenama anda",
            "marketplace_seo": "Penyenaraian dioptimum untuk carian",
            "social_hook": "Pelancaran yang menghentikan skrol",
            "value_concise": "Nilai terus tanpa helah",
            "sea_local": "Pilihan berbaloi yang laris setiap hari",
            "technical_spec": "Model profesional, spesifikasi dahulu",
            "latam_clarity": "Spesifikasi jelas, beli dengan yakin",
            "b2b_wholesale": "Tawaran borong terus dari kilang",
        },
        closings={
            "dtc_story": "Sedia untuk jenama sendiri, pembungkusan tersuai dan waranti 12 bulan",
            "marketplace_seo": "Diperiksa sebelum penghantaran dan waranti 12 bulan",
            "social_hook": (
                "Stok sedia untuk pelancaran seterusnya dari kilang yang disahkan"
            ),
            "value_concise": "Binaan kukuh, penghantaran pantas, sampel tanpa minimum",
            "sea_local": "Stok tersedia, penghantaran seluruh negara, waranti 12 bulan",
            "technical_spec": "Lembaran spesifikasi penuh, dokumen HS dan waranti 12 bulan",
            "latam_clarity": "Waranti 12 bulan dan sokongan selepas jualan",
            "b2b_wholesale": (
                "Label peribadi OEM/ODM, pembungkusan tersuai, dokumen CE/RoHS, "
                "terma FOB dan DDP"
            ),
        },
        feature="{category} dengan {attributes}",
        price="Harga jualan {price} (USD)",
        moq="Tangga harga pukal: {ladder}",
        ladder_entry="{qty} unit {price}",
        ladder_fallback="harga pukal atas permintaan",
        description=(
            "{title}. {category} terus dari kilang untuk pembeli {channel}. Spesifikasi "
            "utama: {attributes}. Harga jualan {price}; harga pukal {ladder}. {closing}"
        ),
        attributes_fallback="kualiti eksport",
        hs_line="Kod HS {hs}.",
        keywords=("borong", "terus dari kilang", "kualiti eksport"),
    ),
    "fr": _LanguagePack(
        code="fr",
        display_name="French",
        word_joiner=" ",
        label_sep=" : ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "Produit phare pour votre marque",
            "marketplace_seo": "Fiche optimisée pour la recherche",
            "social_hook": "Lancement qui arrête le défilement",
            "value_concise": "La valeur sans détour",
            "sea_local": "Le favori petit prix du quotidien",
            "technical_spec": "Modèle pro, fiche technique d'abord",
            "latam_clarity": "Des specs claires, un achat serein",
            "b2b_wholesale": "Offre de gros direct usine",
        },
        closings={
            "dtc_story": (
                "Prêt pour la marque blanche, emballage personnalisé et garantie 12 mois"
            ),
            "marketplace_seo": "Contrôlé avant expédition et garanti 12 mois",
            "social_hook": (
                "Stock prêt pour votre prochain lancement, expédié d'une usine vérifiée"
            ),
            "value_concise": "Fabrication robuste, expédition rapide, échantillon sans minimum",
            "sea_local": "Stock disponible, livraison nationale et garantie 12 mois",
            "technical_spec": "Fiche technique complète, documents HS et garantie 12 mois",
            "latam_clarity": "Garantie 12 mois et service après-vente",
            "b2b_wholesale": (
                "Marque blanche OEM/ODM, emballage personnalisé, certificats CE/RoHS, "
                "conditions FOB et DDP"
            ),
        },
        feature="{category} avec {attributes}",
        price="Prix de vente {price} (USD)",
        moq="Paliers de volume : {ladder}",
        ladder_entry="{qty} pièces à {price}",
        ladder_fallback="tarif dégressif sur demande",
        description=(
            "{title}. {category} direct usine pour les acheteurs {channel}. "
            "Caractéristiques clés : {attributes}. Prix de vente {price} ; tarifs par "
            "volume {ladder}. {closing}"
        ),
        attributes_fallback="qualité export",
        hs_line="Code HS {hs}.",
        keywords=("gros", "direct usine", "qualité export"),
    ),
    "de": _LanguagePack(
        code="de",
        display_name="German",
        word_joiner=" ",
        label_sep=": ",
        list_separator=", ",
        ladder_separator=" · ",
        hooks={
            "dtc_story": "Hero-Produkt für deine Marke",
            "marketplace_seo": "Suchoptimiertes Listing",
            "social_hook": "Launch, der den Feed stoppt",
            "value_concise": "Klare Leistung, kein Schnickschnack",
            "sea_local": "Der Preis-Leistungs-Liebling",
            "technical_spec": "Profi-Modell, Technik zuerst",
            "latam_clarity": "Klare Daten, sicherer Kauf",
            "b2b_wholesale": "Großhandelsangebot direkt ab Werk",
        },
        closings={
            "dtc_story": (
                "Bereit für Private Label, individuelle Verpackung und 12 Monate Garantie"
            ),
            "marketplace_seo": "Vor dem Versand geprüft, mit 12 Monaten Garantie",
            "social_hook": "Ab Lager für den nächsten Drop, versandt aus geprüfter Fabrik",
            "value_concise": "Robust gebaut, schneller Versand, Muster ohne Mindestmenge",
            "sea_local": "Ab Lager, Lieferung bundesweit und 12 Monate Garantie",
            "technical_spec": "Vollständiges Datenblatt, HS-Unterlagen und 12 Monate Garantie",
            "latam_clarity": "12 Monate Garantie und After-Sales-Support",
            "b2b_wholesale": (
                "OEM/ODM Private Label, individuelle Verpackung, CE/RoHS-Unterlagen, "
                "FOB- und DDP-Konditionen"
            ),
        },
        feature="{category} mit {attributes}",
        price="Verkaufspreis {price} (USD)",
        moq="Staffelpreise: {ladder}",
        ladder_entry="{qty} Stück zu {price}",
        ladder_fallback="Staffelpreis auf Anfrage",
        description=(
            "{title}. {category} direkt ab Werk für {channel}-Händler. Wichtigste Daten: "
            "{attributes}. Verkaufspreis {price}; Staffelpreise {ladder}. {closing}"
        ),
        attributes_fallback="Exportqualität",
        hs_line="HS-Code {hs}.",
        keywords=("Großhandel", "direkt ab Werk", "Exportqualität"),
    ),
    "ja": _LanguagePack(
        code="ja",
        display_name="Japanese",
        word_joiner="",
        label_sep="：",
        list_separator="、",
        ladder_separator="、",
        hooks={
            "dtc_story": "ブランドの主力商品に",
            "marketplace_seo": "検索最適化されたリスティング",
            "social_hook": "スクロールを止める新作",
            "value_concise": "無駄のない価値提案",
            "sea_local": "毎日売れるコスパ定番",
            "technical_spec": "仕様重視のプロ仕様モデル",
            "latam_clarity": "仕様が明確で安心して購入",
            "b2b_wholesale": "工場直送の卸売提案",
        },
        closings={
            "dtc_story": "PB（プライベートブランド）対応、化粧箱カスタム、12か月保証",
            "marketplace_seo": "出荷前全数検査と12か月保証",
            "social_hook": "次回の販促に使える在庫を、審査済み工場から出荷",
            "value_concise": "堅牢な作りで即出荷、サンプルは最低ロットなし",
            "sea_local": "在庫あり、全国配送、12か月保証",
            "technical_spec": "詳細な仕様書、HSコード書類、12か月保証付き",
            "latam_clarity": "12か月保証と現地語でのアフターサポート",
            "b2b_wholesale": "OEM/ODMによるPB対応、化粧箱カスタム、CE/RoHS書類、FOB・DDP条件",
        },
        feature="{attributes}の{category}",
        price="販売価格 {price}（USD）",
        moq="数量別の段階価格：{ladder}",
        ladder_entry="{qty} 個 {price}",
        ladder_fallback="数量別価格はお問い合わせください",
        description=(
            "{title}。{channel}のバイヤー向け工場直送{category}。主な仕様：{attributes}。"
            "販売価格 {price}、数量別価格 {ladder}。{closing}"
        ),
        attributes_fallback="輸出グレードの作り",
        hs_line="HSコード {hs}。",
        keywords=("卸売", "工場直送", "輸出品質"),
    ),
}

# (canonical category key, translations per language)
_CATEGORY_TRANSLATIONS: dict[str, dict[str, str]] = {
    "led lighting": {
        "en": "LED Lighting",
        "zh": "LED照明",
        "es": "Iluminación LED",
        "pt": "Iluminação LED",
        "ru": "Светодиодное освещение",
        "th": "ไฟ LED",
        "vi": "Đèn LED",
        "id": "Lampu LED",
        "ms": "Lampu LED",
        "fr": "Éclairage LED",
        "de": "LED-Beleuchtung",
        "ja": "LED照明",
    },
    "led flood light": {
        "en": "LED Flood Light",
        "zh": "LED投光灯",
        "es": "Proyector LED",
        "pt": "Refletor LED",
        "ru": "Светодиодный прожектор",
        "th": "ไฟสปอตไลต์ LED",
        "vi": "Đèn pha LED",
        "id": "Lampu sorot LED",
        "ms": "Lampu limpah LED",
        "fr": "Projecteur LED",
        "de": "LED-Flutlicht",
        "ja": "LED投光器",
    },
    "lighting": {
        "en": "Lighting",
        "zh": "灯具",
        "es": "Iluminación",
        "pt": "Iluminação",
        "ru": "Освещение",
        "th": "โคมไฟ",
        "vi": "Chiếu sáng",
        "id": "Pencahayaan",
        "ms": "Pencahayaan",
        "fr": "Éclairage",
        "de": "Beleuchtung",
        "ja": "照明",
    },
    "solar": {
        "en": "Solar",
        "zh": "太阳能",
        "es": "Solar",
        "pt": "Solar",
        "ru": "Солнечная энергия",
        "th": "พลังงานแสงอาทิตย์",
        "vi": "Năng lượng mặt trời",
        "id": "Tenaga surya",
        "ms": "Tenaga suria",
        "fr": "Solaire",
        "de": "Solar",
        "ja": "ソーラー",
    },
    "consumer electronics": {
        "en": "Consumer Electronics",
        "zh": "消费电子",
        "es": "Electrónica de consumo",
        "pt": "Eletrônicos de consumo",
        "ru": "Бытовая электроника",
        "th": "อิเล็กทรอนิกส์สำหรับผู้บริโภค",
        "vi": "Điện tử tiêu dùng",
        "id": "Elektronik konsumen",
        "ms": "Elektronik pengguna",
        "fr": "Électronique grand public",
        "de": "Unterhaltungselektronik",
        "ja": "民生用電子機器",
    },
    "home & garden": {
        "en": "Home & Garden",
        "zh": "家居园艺",
        "es": "Hogar y jardín",
        "pt": "Casa e jardim",
        "ru": "Дом и сад",
        "th": "บ้านและสวน",
        "vi": "Nhà cửa và sân vườn",
        "id": "Rumah & taman",
        "ms": "Rumah & taman",
        "fr": "Maison et jardin",
        "de": "Haus & Garten",
        "ja": "ホーム＆ガーデン",
    },
    "kitchen": {
        "en": "Kitchen",
        "zh": "厨房用品",
        "es": "Cocina",
        "pt": "Cozinha",
        "ru": "Кухня",
        "th": "อุปกรณ์ครัว",
        "vi": "Nhà bếp",
        "id": "Dapur",
        "ms": "Dapur",
        "fr": "Cuisine",
        "de": "Küche",
        "ja": "キッチン用品",
    },
    "apparel": {
        "en": "Apparel",
        "zh": "服装",
        "es": "Ropa",
        "pt": "Vestuário",
        "ru": "Одежда",
        "th": "เสื้อผ้า",
        "vi": "Quần áo",
        "id": "Pakaian",
        "ms": "Pakaian",
        "fr": "Vêtements",
        "de": "Bekleidung",
        "ja": "アパレル",
    },
    "tools": {
        "en": "Tools",
        "zh": "工具",
        "es": "Herramientas",
        "pt": "Ferramentas",
        "ru": "Инструменты",
        "th": "เครื่องมือช่าง",
        "vi": "Dụng cụ",
        "id": "Perkakas",
        "ms": "Alatan",
        "fr": "Outillage",
        "de": "Werkzeuge",
        "ja": "工具",
    },
    "furniture": {
        "en": "Furniture",
        "zh": "家具",
        "es": "Muebles",
        "pt": "Móveis",
        "ru": "Мебель",
        "th": "เฟอร์นิเจอร์",
        "vi": "Nội thất",
        "id": "Furnitur",
        "ms": "Perabot",
        "fr": "Meubles",
        "de": "Möbel",
        "ja": "家具",
    },
    "pet supplies": {
        "en": "Pet Supplies",
        "zh": "宠物用品",
        "es": "Artículos para mascotas",
        "pt": "Artigos para animais",
        "ru": "Товары для питомцев",
        "th": "อุปกรณ์สัตว์เลี้ยง",
        "vi": "Đồ dùng thú cưng",
        "id": "Perlengkapan hewan",
        "ms": "Keperluan haiwan",
        "fr": "Accessoires pour animaux",
        "de": "Tierbedarf",
        "ja": "ペット用品",
    },
    "sports & outdoors": {
        "en": "Sports & Outdoors",
        "zh": "运动户外",
        "es": "Deportes y aire libre",
        "pt": "Esportes e ar livre",
        "ru": "Спорт и отдых",
        "th": "กีฬาและกิจกรรมกลางแจ้ง",
        "vi": "Thể thao & ngoài trời",
        "id": "Olahraga & luar ruang",
        "ms": "Sukan & luar",
        "fr": "Sports et plein air",
        "de": "Sport & Outdoor",
        "ja": "スポーツ・アウトドア",
    },
    "packaging": {
        "en": "Packaging",
        "zh": "包装耗材",
        "es": "Embalaje",
        "pt": "Embalagens",
        "ru": "Упаковка",
        "th": "บรรจุภัณฑ์",
        "vi": "Bao bì",
        "id": "Kemasan",
        "ms": "Pembungkusan",
        "fr": "Emballage",
        "de": "Verpackung",
        "ja": "梱包資材",
    },
    "beauty": {
        "en": "Beauty",
        "zh": "美妆个护",
        "es": "Belleza",
        "pt": "Beleza",
        "ru": "Красота",
        "th": "ความงาม",
        "vi": "Mỹ phẩm",
        "id": "Kecantikan",
        "ms": "Kecantikan",
        "fr": "Beauté",
        "de": "Beauty",
        "ja": "ビューティー",
    },
}

# Chinese / English source spellings an importer actually types, mapped to a canonical key.
_CATEGORY_ALIASES: dict[str, str] = {
    "投光灯": "led flood light",
    "泛光灯": "led flood light",
    "flood light": "led flood light",
    "floodlight": "led flood light",
    "led投光灯": "led flood light",
    "led灯": "led lighting",
    "led照明": "led lighting",
    "太阳能": "solar",
    "光伏": "solar",
    "灯具": "lighting",
    "照明": "lighting",
    "light": "lighting",
    "电子": "consumer electronics",
    "electronics": "consumer electronics",
    "家居": "home & garden",
    "home": "home & garden",
    "厨房": "kitchen",
    "kitchen": "kitchen",
    "服装": "apparel",
    "clothing": "apparel",
    "apparel": "apparel",
    "工具": "tools",
    "tool": "tools",
    "家具": "furniture",
    "furniture": "furniture",
    "宠物": "pet supplies",
    "pet": "pet supplies",
    "户外运动": "sports & outdoors",
    "sports": "sports & outdoors",
    "包装": "packaging",
    "packaging": "packaging",
    "美妆": "beauty",
    "美容": "beauty",
    "beauty": "beauty",
}

# (canonical attribute key, translations per language)
_ATTRIBUTE_TRANSLATIONS: dict[str, dict[str, str]] = {
    "waterproof": {
        "en": "waterproof",
        "zh": "防水",
        "es": "resistente al agua",
        "pt": "à prova d'água",
        "ru": "водонепроницаемый",
        "th": "กันน้ำ",
        "vi": "chống nước",
        "id": "tahan air",
        "ms": "kalis air",
        "fr": "étanche",
        "de": "wasserdicht",
        "ja": "防水",
    },
    "rechargeable": {
        "en": "rechargeable",
        "zh": "可充电",
        "es": "recargable",
        "pt": "recarregável",
        "ru": "аккумуляторный",
        "th": "ชาร์จไฟได้",
        "vi": "sạc lại",
        "id": "dapat diisi ulang",
        "ms": "boleh dicas",
        "fr": "rechargeable",
        "de": "wiederaufladbar",
        "ja": "充電式",
    },
    "remote control": {
        "en": "remote control",
        "zh": "遥控",
        "es": "control remoto",
        "pt": "controle remoto",
        "ru": "с дистанционным управлением",
        "th": "รีโมทคอนโทรล",
        "vi": "điều khiển từ xa",
        "id": "kontrol jarak jauh",
        "ms": "kawalan jauh",
        "fr": "télécommande",
        "de": "Fernbedienung",
        "ja": "リモコン付き",
    },
    "dimmable": {
        "en": "dimmable",
        "zh": "可调光",
        "es": "regulable",
        "pt": "dimerizável",
        "ru": "с регулировкой яркости",
        "th": "ปรับแสงได้",
        "vi": "điều chỉnh độ sáng",
        "id": "dapat diredupkan",
        "ms": "boleh dimalapkan",
        "fr": "variable",
        "de": "dimmbar",
        "ja": "調光対応",
    },
    "smart": {
        "en": "smart",
        "zh": "智能",
        "es": "inteligente",
        "pt": "inteligente",
        "ru": "умный",
        "th": "อัจฉริยะ",
        "vi": "thông minh",
        "id": "pintar",
        "ms": "pintar",
        "fr": "connecté",
        "de": "smart",
        "ja": "スマート",
    },
    "motion sensor": {
        "en": "motion sensor",
        "zh": "人体感应",
        "es": "sensor de movimiento",
        "pt": "sensor de movimento",
        "ru": "датчик движения",
        "th": "เซ็นเซอร์ตรวจจับความเคลื่อนไหว",
        "vi": "cảm biến chuyển động",
        "id": "sensor gerak",
        "ms": "penderia gerakan",
        "fr": "détecteur de mouvement",
        "de": "Bewegungsmelder",
        "ja": "人感センサー",
    },
    "outdoor": {
        "en": "outdoor",
        "zh": "户外",
        "es": "exterior",
        "pt": "externo",
        "ru": "для улицы",
        "th": "กลางแจ้ง",
        "vi": "ngoài trời",
        "id": "luar ruang",
        "ms": "luar",
        "fr": "extérieur",
        "de": "Außenbereich",
        "ja": "屋外",
    },
    "indoor": {
        "en": "indoor",
        "zh": "室内",
        "es": "interior",
        "pt": "interno",
        "ru": "для помещений",
        "th": "ในร่ม",
        "vi": "trong nhà",
        "id": "dalam ruang",
        "ms": "dalam",
        "fr": "intérieur",
        "de": "Innenbereich",
        "ja": "屋内",
    },
    "portable": {
        "en": "portable",
        "zh": "便携",
        "es": "portátil",
        "pt": "portátil",
        "ru": "портативный",
        "th": "พกพาได้",
        "vi": "di động",
        "id": "portabel",
        "ms": "mudah alih",
        "fr": "portable",
        "de": "tragbar",
        "ja": "ポータブル",
    },
    "stainless steel": {
        "en": "stainless steel",
        "zh": "不锈钢",
        "es": "acero inoxidable",
        "pt": "aço inoxidável",
        "ru": "нержавеющая сталь",
        "th": "สแตนเลส",
        "vi": "thép không gỉ",
        "id": "baja stainless",
        "ms": "keluli tahan karat",
        "fr": "acier inoxydable",
        "de": "Edelstahl",
        "ja": "ステンレス",
    },
    "wireless": {
        "en": "wireless",
        "zh": "无线",
        "es": "inalámbrico",
        "pt": "sem fio",
        "ru": "беспроводной",
        "th": "ไร้สาย",
        "vi": "không dây",
        "id": "nirkabel",
        "ms": "tanpa wayar",
        "fr": "sans fil",
        "de": "kabellos",
        "ja": "ワイヤレス",
    },
    "heavy duty": {
        "en": "heavy duty",
        "zh": "重型",
        "es": "uso intensivo",
        "pt": "uso intenso",
        "ru": "повышенной прочности",
        "th": "งานหนัก",
        "vi": "chịu lực cao",
        "id": "tahan berat",
        "ms": "tahan lasak",
        "fr": "renforcé",
        "de": "robust",
        "ja": "重荷重対応",
    },
}

# Source spellings (Chinese and English variants) -> canonical attribute key.
_ATTRIBUTE_ALIASES: dict[str, str] = {
    "防水": "waterproof",
    "water proof": "waterproof",
    "可充电": "rechargeable",
    "充电": "rechargeable",
    "遥控": "remote control",
    "remote": "remote control",
    "可调光": "dimmable",
    "调光": "dimmable",
    "智能": "smart",
    "人体感应": "motion sensor",
    "红外感应": "motion sensor",
    "pir sensor": "motion sensor",
    "户外": "outdoor",
    "室外": "outdoor",
    "室内": "indoor",
    "便携": "portable",
    "手提": "portable",
    "不锈钢": "stainless steel",
    "无线": "wireless",
    "重型": "heavy duty",
    "工业级": "heavy duty",
}

# Order matters: multi-word aliases must be tested before the single words they contain.
_ATTRIBUTE_ALIAS_ORDER: tuple[tuple[str, str], ...] = tuple(
    sorted(_ATTRIBUTE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
)

# Units and protection ratings are never translated: they are technical facts, not copy.
_SPEC_TOKEN_RE = re.compile(
    r"\b(?:IP[0-9]{2}|[0-9]{1,4}\s?(?:W|V|mAh|lm|K|mm|cm|kg))\b",
    re.IGNORECASE,
)

_PRICE_PREFIX = "$"


def _resolve_pack(language: str) -> _LanguagePack:
    """Return the language pack for ``language``, defaulting to English when unsupported."""
    normalized = (language or "en").strip().lower()
    return _LANGUAGE_PACKS.get(normalized, _LANGUAGE_PACKS["en"])


def _platform_style(channel: str) -> str:
    return _PLATFORM_STYLE.get((channel or "").strip().lower(), _STYLE_FALLBACK)


def _channel_display(channel: str) -> str:
    normalized = (channel or "").strip().lower()
    return CHANNEL_DISPLAY_NAMES.get(normalized, channel or "marketplace")


def _canonical_category(category: str) -> str | None:
    normalized = (category or "").strip().lower()
    if normalized in _CATEGORY_TRANSLATIONS:
        return normalized
    for alias in sorted(_CATEGORY_ALIASES, key=len, reverse=True):
        if alias in normalized:
            return _CATEGORY_ALIASES[alias]
    return None


def _localized_category(category: str, lang: str) -> str:
    canonical = _canonical_category(category)
    if canonical is None:
        return (category or "product").strip() or "product"
    return _CATEGORY_TRANSLATIONS[canonical].get(lang, _CATEGORY_TRANSLATIONS[canonical]["en"])


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float, str)):
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    return None


def _as_quantity(value: Any) -> int | None:
    parsed = _as_decimal(value)
    if parsed is None or parsed <= 0:
        return None
    integral = int(parsed)
    return integral if Decimal(integral) == parsed else None


def _money(value: Decimal) -> str:
    """Format a ``Decimal`` without dropping or rounding a single digit."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _tier_price(tier: dict[str, Any]) -> Decimal | None:
    for key in ("unit_price_usd", "unit_price", "price_usd"):
        if key in tier:
            return _as_decimal(tier[key])
    return None


def _tier_quantity(tier: dict[str, Any]) -> int | None:
    for key in ("min_qty", "quantity", "qty"):
        if key in tier:
            return _as_quantity(tier[key])
    return None


def _ladder_entries(payload: SourcingCopyRewriteInput, pack: _LanguagePack) -> list[str]:
    entries: list[str] = []
    for tier in payload.moq_tiers:
        if not isinstance(tier, dict):
            continue
        quantity = _tier_quantity(tier)
        price = _tier_price(tier)
        if quantity is None or price is None:
            continue
        entries.append(
            pack.ladder_entry.format(qty=f"{quantity:,}", price=f"{_PRICE_PREFIX}{_money(price)}")
        )
    return entries


def _ladder_text(payload: SourcingCopyRewriteInput, pack: _LanguagePack) -> str:
    entries = _ladder_entries(payload, pack)
    return pack.ladder_separator.join(entries) if entries else pack.ladder_fallback


def _source_haystack(payload: SourcingCopyRewriteInput) -> str:
    parts = [payload.raw_title, payload.source_description or "", " ".join(payload.keywords)]
    parts.extend(payload.existing_bullets)
    return " ".join(parts).lower()


def _localized_attributes(payload: SourcingCopyRewriteInput, lang: str) -> list[str]:
    """Translate known attributes and keep unknown spec tokens (``IP65``, ``50W``) as-is."""
    haystack = _source_haystack(payload)
    spec_source = " ".join(
        [payload.raw_title, payload.source_description or "", " ".join(payload.keywords)]
    )
    attributes: list[str] = []
    seen: set[str] = set()
    for alias, canonical in _ATTRIBUTE_ALIAS_ORDER:
        if alias not in haystack or canonical in seen:
            continue
        seen.add(canonical)
        attributes.append(_ATTRIBUTE_TRANSLATIONS[canonical].get(lang, canonical))
    for token in _SPEC_TOKEN_RE.findall(spec_source):
        normalized = re.sub(r"\s+", "", token)
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        attributes.append(normalized.upper() if key.startswith("ip") else normalized)
    for keyword in payload.keywords:
        cleaned = keyword.strip()
        if not cleaned:
            continue
        matched = _ATTRIBUTE_ALIASES.get(cleaned.lower())
        if matched is not None:
            translated = _ATTRIBUTE_TRANSLATIONS[matched].get(lang, matched)
            if matched not in seen:
                seen.add(matched)
                attributes.append(translated)
            continue
        if cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        attributes.append(cleaned)
    if not attributes:
        attributes.append(_resolve_pack(lang).attributes_fallback)
    return attributes[:4]


def _compose_title(
    payload: SourcingCopyRewriteInput,
    pack: _LanguagePack,
    style: str,
    category: str,
    attributes: list[str],
) -> str:
    budget = _TITLE_BUDGET.get(style, 200)
    attribute_limit = {"value_concise": 2, "social_hook": 3, "sea_local": 3}.get(style, 4)
    parts = [category, *attributes[:attribute_limit]]
    if style == "marketplace_seo":
        parts.extend(payload.keywords[:2])
    title = pack.word_joiner.join(part for part in parts if part).strip()
    if not title:
        title = category
    if len(title) > budget:
        title = title[:budget].rstrip()
    return title or (payload.raw_title or "product")[:budget]


def _seo_keywords(
    payload: SourcingCopyRewriteInput,
    pack: _LanguagePack,
    category: str,
    attributes: list[str],
) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for candidate in [category, *attributes, *payload.keywords, *pack.keywords]:
        cleaned = candidate.strip()
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        keywords.append(cleaned)
    return keywords[:12]


class SourcingAiRewriteRequest(BaseModel):
    """Tenant-supplied rewrite options for one sourcing draft."""

    target_channel: str = Field(default="shopify", min_length=1, max_length=64)
    target_language: str = Field(default="en", min_length=2, max_length=16)
    tone: SourcingRewriteTone = "conversion"
    keywords: list[str] = Field(default_factory=list, max_length=20)
    apply_to_draft: bool = True


class SourcingCopyRewriteInput(BaseModel):
    """Everything the rewrite adapter may read: the draft facts plus the target listing."""

    draft_id: UUID
    raw_title: str
    source_description: str | None = None
    category: str
    hs_code: str | None = None
    target_price_usd: Decimal
    moq_tiers: list[dict[str, Any]] = Field(default_factory=list)
    existing_bullets: list[str] = Field(default_factory=list)
    target_channel: str
    target_language: str
    tone: SourcingRewriteTone = "conversion"
    keywords: list[str] = Field(default_factory=list)


class SourcingCopyRewriteOutput(BaseModel):
    """A ready-to-publish localized listing, with honest provenance of how it was produced."""

    target_channel: str
    target_language: str
    tone: SourcingRewriteTone
    title: str
    bullet_points: list[str] = Field(min_length=5, max_length=5)
    description: str
    seo_keywords: list[str] = Field(default_factory=list)
    rewrite_mode: Literal["llm", "deterministic_fallback"]
    model_used: str
    credits_consumed: Decimal = Decimal("0.0000")
    fallback_reason: str | None = None
    generated_at: datetime


class SourcingCopyRewriter(Protocol):
    """Structural contract implemented by ``erp_core.ai_engine.sourcing_copy``."""

    async def rewrite_copy(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: SourcingCopyRewriteInput,
    ) -> SourcingCopyRewriteOutput:
        """Rewrite one sourcing draft into a localized, platform-ready listing."""


def build_deterministic_localized_copy(
    payload: SourcingCopyRewriteInput, *, reason: str | None = None
) -> SourcingCopyRewriteOutput:
    """Write real localized listing copy without any LLM, for zero credits.

    The retail price and every MOQ tier quantity/price come from the payload and are
    re-emitted verbatim (only formatted), so a fallback listing can never contradict
    the ERP's priced draft.
    """
    pack = _resolve_pack(payload.target_language)
    style = _platform_style(payload.target_channel)
    channel = _channel_display(payload.target_channel)
    category = _localized_category(payload.category, pack.code)
    attributes = _localized_attributes(payload, pack.code)
    attributes_text = pack.list_separator.join(attributes)
    title = _compose_title(payload, pack, style, category, attributes)

    price = _as_decimal(payload.target_price_usd)
    price_text = f"{_PRICE_PREFIX}{_money(price)}" if price is not None else pack.ladder_fallback
    ladder = _ladder_text(payload, pack)
    hook = pack.hooks.get(style, pack.hooks[_STYLE_FALLBACK])
    closing = pack.closings.get(style, pack.closings[_STYLE_FALLBACK])

    description = pack.description.format(
        title=title,
        category=category,
        channel=channel,
        attributes=attributes_text,
        price=price_text,
        ladder=ladder,
        closing=closing,
    )
    hs_code = (payload.hs_code or "").strip()
    if hs_code:
        description = f"{description} {pack.hs_line.format(hs=hs_code)}"

    bullet_points = [
        f"{hook}{pack.label_sep}{title}",
        pack.feature.format(category=category, attributes=attributes_text),
        pack.price.format(price=price_text),
        pack.moq.format(ladder=ladder),
        closing,
    ]

    return SourcingCopyRewriteOutput(
        target_channel=payload.target_channel,
        target_language=payload.target_language,
        tone=payload.tone,
        title=title,
        bullet_points=bullet_points,
        description=description,
        seo_keywords=_seo_keywords(payload, pack, category, attributes),
        rewrite_mode="deterministic_fallback",
        model_used="deterministic-rule-v1",
        credits_consumed=Decimal("0.0000"),
        fallback_reason=reason,
        generated_at=datetime.now(UTC),
    )
