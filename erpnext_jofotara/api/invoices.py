# erpnext_jofotara/api/invoices.py
from __future__ import annotations

import base64
import re
from decimal import Decimal
from urllib.parse import urljoin

import requests
import frappe
from frappe import _
from erpnext_jofotara.install import ensure_custom_fields


# =========================
# Helpers
# =========================

def _full_url(base: str, path: str) -> str:
    if (path or "").startswith("http"):
        return path
    return urljoin((base or "").rstrip("/") + "/", (path or "").lstrip("/"))


def _get_settings():
    return frappe.get_single("JoFotara Settings")


def _mask_headers(h: dict) -> dict:
    masked = dict(h or {})
    for k in ("Secret-Key", "Authorization"):
        if k in masked and masked[k]:
            masked[k] = "********"
    return masked


def _build_headers(s):
    """
    يبني الهيدر المطلوب لواجهة JoFotara.
    يقبل إما Client ID/Secret أو Device User/Secret (Fallback).
    """
    client_id = (s.client_id or "").strip()
    client_secret = (s.get_password("secret_key", raise_exception=False) or "").strip()

    device_user = (s.device_user or "").strip()
    device_secret = (s.get_password("device_secret", raise_exception=False) or "").strip()

    # fallback من Device لو Client فاضي
    if not client_id and device_user:
        client_id = device_user
    if not client_secret and device_secret:
        client_secret = device_secret

    if not client_id or not client_secret:
        frappe.throw(
            _("JoFotara Settings is missing credentials. Fill either Client ID/Secret or Device User/Secret.")
        )

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Client-Id": client_id,
        "Secret-Key": client_secret,
        "Accept-Language": "ar",
    }

    key = (s.activity_number or "").strip()
    if key:
        headers["Key"] = key
        headers["Activity-Number"] = key
        headers["ActivityNumber"] = key

    return headers


def _fmt(n: float | Decimal, places: int = 3) -> str:
    try:
        return f"{float(n):.{places}f}"
    except Exception:
        return f"{0:.{places}f}"


def _uom_code(uom: str | None) -> str:
    """UOM mapping. الافتراضي PCE كما في أمثلة الدليل (قطعة)."""
    if not uom:
        return "PCE"
    key = (uom or "").strip().lower()
    mapping = {
        # شائعة كـ "قطعة/وحدة"
        "unit": "PCE", "units": "PCE", "each": "PCE", "pcs": "PCE", "piece": "PCE", "nos": "PCE",
        "وحدة": "PCE", "قطعة": "PCE",
        # أمثلة أخرى
        "kg": "KGM", "kilogram": "KGM", "كيلو": "KGM",
        "g": "GRM", "gram": "GRM",
        "l": "LTR", "lt": "LTR", "liter": "LTR", "لتر": "LTR",
        "ml": "MLT",
        "m": "MTR", "meter": "MTR", "متر": "MTR",
        "cm": "CMT", "mm": "MMT",
        "hour": "HUR", "hr": "HUR", "ساعة": "HUR",
        "day": "DAY", "يوم": "DAY",
        "month": "MON", "شهر": "MON",
        "year": "ANN", "سنة": "ANN",
    }
    return mapping.get(key, "PCE")


def _minify_xml(xml_str: str) -> str:
    # Minify XML لتفادي مشاكل المسافات/السطور
    if not xml_str:
        return xml_str
    s = xml_str.replace("\r", "").replace("\n", "").replace("\t", "").strip()
    s = re.sub(r">\s+<", "><", s)
    s = s.replace("\ufeff", "")
    return s


# =========================
# UBL 2.1 - Income Invoice ONLY
# =========================

def generate_ubl_xml(doc) -> str:
    """
    يبني XML لفاتورة Income (غير مسجل ضريبة مبيعات).
    الالتزام بنموذج JoFotara (ProfileID reporting:1.0)
    - بدون TaxTotal على مستوى الفاتورة.
    - مع AdditionalDocumentReference (ICV).
    - مع SellerSupplierParty (sequence of income source).
    - InvoiceTypeCode ثابت 388 و name = 011 (نقدي) أو 021 (آجل).
    """
    s = _get_settings()

    cur = (doc.currency or "JOD").upper()
    issue_date = str(doc.posting_date)  # JoFotara يقبل yyyy-mm-dd
    note = (getattr(doc, "remarks", None) or getattr(doc, "po_no", None) or "").strip()

    # معرّفات أساسية
    invoice_id = doc.name

    # UUID فريد – إن لم يوجد بالدوك، أنشئ واحد واحفظه
    uuid = (getattr(doc, "jofotara_uuid", None) or frappe.generate_hash(length=36))
    try:
        if not getattr(doc, "jofotara_uuid", None) and doc.meta.has_field("jofotara_uuid"):
            doc.db_set("jofotara_uuid", uuid)
    except Exception:
        pass

    # عداد ICV (ابدأ من 1 إن لم يوجد)
    icv = int(getattr(doc, "jofotara_icv", 0) or 1)
    try:
        if doc.meta.has_field("jofotara_icv") and not getattr(doc, "jofotara_icv", None):
            doc.db_set("jofotara_icv", icv)
    except Exception:
        pass

    # نوع السداد: 011 نقدي / 021 آجل (ذمم)
    is_cash = bool(getattr(doc, "is_pos", 0)) or (
        float(getattr(doc, "paid_amount", 0) or 0) >= float(getattr(doc, "grand_total", 0) or 0)
    )
    type_name = "011" if is_cash else "021"  # Income فقط

    # بيانات البائع (taxpayer) — الرقم الضريبي إلزامي وأرقام فقط 1..15
    supplier_name = frappe.db.get_value("Company", doc.company, "company_name") or doc.company
    supplier_tax_raw = (doc.company_tax_id or "").strip()
    supplier_tax = re.sub(r"\D", "", supplier_tax_raw)  # أرقام فقط
    if not (1 <= len(supplier_tax) <= 15):
        frappe.throw(_("Seller Tax Number is required (1-15 digits). Current: '{0}'").format(supplier_tax_raw))

    # بيانات المشتري (مختصرة لـ Income)
    customer_name = (doc.customer_name or doc.customer or "").strip()
    buyer_phone = (getattr(doc, "contact_phone", None) or getattr(doc, "contact_mobile", None) or "").strip()
    postal_code = ""
    try:
        if getattr(doc, "customer_address", None):
            postal_code = frappe.db.get_value("Address", doc.customer_address, "pincode") or ""
    except Exception:
        pass

    # رقم تعريف المشتري (اختياري: TN/NIN/PN حسب ما يتوفر لديك)
    buyer_id = (doc.tax_id or "").strip()
    buyer_scheme = "TN" if buyer_id else ""  # غيّرها لـ NIN/PN لو عندك حقل مخصص

    # ===== احسب المجاميع من السطور التي سنرسلها =====
    gross_lines = 0.0          # مجموع (qty * rate) قبل الخصم
    total_item_discount = 0.0  # مجموع خصومات السطور
    line_ext_total = 0.0       # مجموع صافي السطور بعد خصم السطور

    line_blocks: list[str] = []
    for idx, it in enumerate(doc.items, start=1):
        qty = float(it.qty or 1)
        unit_price = float(it.rate or 0)  # Income بدون VAT
        line_discount = float(getattr(it, "discount_amount", 0) or 0)
        gross = qty * unit_price
        net_line = gross - line_discount

        gross_lines += gross
        total_item_discount += line_discount
        line_ext_total += net_line

        uom = _uom_code(getattr(it, "uom", None))
        name = frappe.utils.escape_html(it.item_name or it.item_code or "Item")

        line_blocks.append(
            "\n".join([
                "  <cac:InvoiceLine>",
                f"    <cbc:ID>{idx}</cbc:ID>",
                f'    <cbc:InvoicedQuantity unitCode="{uom}">{_fmt(qty, 2)}</cbc:InvoicedQuantity>',
                f'    <cbc:LineExtensionAmount currencyID="{cur}">{_fmt(net_line, 3)}</cbc:LineExtensionAmount>',
                "    <cac:Item>",
                f"      <cbc:Name>{name}</cbc:Name>",
                "    </cac:Item>",
                "    <cac:Price>",
                f'      <cbc:PriceAmount currencyID="{cur}">{_fmt(unit_price, 3)}</cbc:PriceAmount>',
                "      <cac:AllowanceCharge>",
                "        <cbc:ChargeIndicator>false</cbc:ChargeIndicator>",
                "        <cbc:AllowanceChargeReason>DISCOUNT</cbc:AllowanceChargeReason>",
                f'        <cbc:Amount currencyID="{cur}">{_fmt(line_discount, 3)}</cbc:Amount>',
                "      </cac:AllowanceCharge>",
                "    </cac:Price>",
                "  </cac:InvoiceLine>",
            ])
        )

    lines_xml = "\n".join(line_blocks)

    # في Income لا يوجد VAT: إذن الشامل = الصافي = المستحق
    tax_exclusive_total = line_ext_total
    tax_inclusive_total = line_ext_total
    payable_total = line_ext_total
    allowance_total = max(total_item_discount, 0.0)

    # sequence of income source من الإعدادات (Activity Number)
    income_sequence = (getattr(s, "activity_number", None) or "").strip()

    parts: list[str] = []
    parts += [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"',
        '         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"',
        '         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">',
        '  <cbc:ProfileID>reporting:1.0</cbc:ProfileID>',
        f'  <cbc:ID>{invoice_id}</cbc:ID>',
        f'  <cbc:UUID>{uuid}</cbc:UUID>',
        f'  <cbc:IssueDate>{issue_date}</cbc:IssueDate>',
        f'  <cbc:InvoiceTypeCode name="{type_name}">388</cbc:InvoiceTypeCode>',
        f'  <cbc:Note>{frappe.utils.escape_html(note)}</cbc:Note>',
        f'  <cbc:DocumentCurrencyCode>{cur}</cbc:DocumentCurrencyCode>',
        f'  <cbc:TaxCurrencyCode>{cur}</cbc:TaxCurrencyCode>',
        # ICV (العداد)
        "  <cac:AdditionalDocumentReference>",
        "    <cbc:ID>ICV</cbc:ID>",
        f"    <cbc:UUID>{icv}</cbc:UUID>",
        "  </cac:AdditionalDocumentReference>",
        "",
        # Seller (taxpayer)
        "  <cac:AccountingSupplierParty>",
        "    <cac:Party>",
        "      <cac:PostalAddress>",
        "        <cac:Country><cbc:IdentificationCode>JO</cbc:IdentificationCode></cac:Country>",
        "      </cac:PostalAddress>",
        "      <cac:PartyTaxScheme>",
        f"        <cbc:CompanyID>{frappe.utils.escape_html(supplier_tax)}</cbc:CompanyID>",
        "        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>",
        "      </cac:PartyTaxScheme>",
        "      <cac:PartyLegalEntity>",
        f"        <cbc:RegistrationName>{frappe.utils.escape_html(supplier_name)}</cbc:RegistrationName>",
        "      </cac:PartyLegalEntity>",
        "    </cac:Party>",
        "  </cac:AccountingSupplierParty>",
        "",
        # Buyer
        "  <cac:AccountingCustomerParty>",
        "    <cac:Party>",
    ]

    if buyer_id:
        parts += [
            "      <cac:PartyIdentification>",
            f'        <cbc:ID schemeID="{buyer_scheme}">{frappe.utils.escape_html(buyer_id)}</cbc:ID>',
            "      </cac:PartyIdentification>",
        ]

    parts += [
        "      <cac:PostalAddress>",
        f"        <cbc:PostalZone>{frappe.utils.escape_html(postal_code)}</cbc:PostalZone>",
        "        <cac:Country><cbc:IdentificationCode>JO</cbc:IdentificationCode></cac:Country>",
        "      </cac:PostalAddress>",
        "      <cac:PartyTaxScheme>",
        "        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>",
        "      </cac:PartyTaxScheme>",
        "      <cac:PartyLegalEntity>",
        f"        <cbc:RegistrationName>{frappe.utils.escape_html(customer_name)}</cbc:RegistrationName>",
        "      </cac:PartyLegalEntity>",
        "    </cac:Party>",
        "    <cac:AccountingContact>",
        f"      <cbc:Telephone>{frappe.utils.escape_html(buyer_phone)}</cbc:Telephone>",
        "    </cac:AccountingContact>",
        "  </cac:AccountingCustomerParty>",
        "",
    ]

    # SellerSupplierParty (sequence of income source)
    if income_sequence:
        parts += [
            "  <cac:SellerSupplierParty>",
            "    <cac:Party>",
            "      <cac:PartyIdentification>",
            f"        <cbc:ID>{frappe.utils.escape_html(income_sequence)}</cbc:ID>",
            "      </cac:PartyIdentification>",
            "    </cac:Party>",
            "  </cac:SellerSupplierParty>",
            "",
        ]

    # AllowanceCharge + LegalMonetaryTotal (بدون TaxTotal)
    parts += [
        "  <cac:AllowanceCharge>",
        "    <cbc:ChargeIndicator>false</cbc:ChargeIndicator>",
        "    <cbc:AllowanceChargeReason>discount</cbc:AllowanceChargeReason>",
        f'    <cbc:Amount currencyID="{cur}">{_fmt(allowance_total, 3)}</cbc:Amount>',
        "  </cac:AllowanceCharge>",
        "  <cac:LegalMonetaryTotal>",
        f'    <cbc:LineExtensionAmount currencyID="{cur}">{_fmt(line_ext_total, 3)}</cbc:LineExtensionAmount>',
        f'    <cbc:TaxExclusiveAmount currencyID="{cur}">{_fmt(tax_exclusive_total, 3)}</cbc:TaxExclusiveAmount>',
        f'    <cbc:TaxInclusiveAmount currencyID="{cur}">{_fmt(tax_inclusive_total, 3)}</cbc:TaxInclusiveAmount>',
        f'    <cbc:AllowanceTotalAmount currencyID="{cur}">{_fmt(allowance_total, 3)}</cbc:AllowanceTotalAmount>',
        f'    <cbc:PayableAmount currencyID="{cur}">{_fmt(payable_total, 3)}</cbc:PayableAmount>',
        "  </cac:LegalMonetaryTotal>",
        "",
        lines_xml,
        "</Invoice>",
    ]

    return "\n".join(parts)


# =========================
# Hook: إرسال عند الاعتماد
# =========================

def on_submit_send(doc, method=None):
    s = _get_settings()

    if not s.get("send_on_submit"):
        return
    if getattr(doc, "is_return", 0):
        return

    try:
        ensure_custom_fields()

        xml_str = getattr(doc, "jofotara_xml", None) or generate_ubl_xml(doc)
        if not xml_str:
            frappe.throw(_("Missing UBL XML (field jofotara_xml). Please generate UBL 2.1 and try again."))

        xml_min = _minify_xml(xml_str)
        xml_bytes = xml_min.encode("utf-8")
        payload = {"invoice": base64.b64encode(xml_bytes).decode()}

        url = _full_url(getattr(s, "base_url", ""), getattr(s, "submit_url", "/core/invoices/") or "/core/invoices/")
        headers = _build_headers(s)

        # DEBUG: لوج لأول 800 حرف من الـ XML عند تفعيل developer_mode
        if frappe.conf.get("developer_mode"):
            try:
                sample = xml_min[:800]
                frappe.log_error(
                    message=f"Outgoing UBL (first 800 chars):\n{sample}",
                    title="JoFotara DEBUG - Outgoing XML"
                )
            except Exception:
                pass

        r = requests.post(url, json=payload, headers=headers, timeout=90)

        if r.status_code >= 400:
            detail = r.text
            try:
                detail = frappe.as_json(r.json(), indent=2)
            except Exception:
                pass

            frappe.log_error(
                message=(
                    f"URL: {url}\n"
                    f"Status: {r.status_code}\n"
                    f"Request Headers (masked): {frappe.as_json(_mask_headers(headers))}\n"
                    f"Payload keys: {list(payload.keys())}\n"
                    f"Response Body:\n{detail}"
                ),
                title="JoFotara API Error",
            )
            raise frappe.ValidationError(_("JoFotara API error {0}. See Error Log for details.").format(r.status_code))

        resp = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}

        handle_submit_response(doc, resp)
        frappe.msgprint(_("JoFotara: Invoice submitted successfully"), alert=1, indicator="green")

    except Exception:
        ensure_custom_fields()
        if doc.meta.has_field("jofotara_status"):
            doc.db_set("jofotara_status", "Error")
        frappe.log_error(frappe.get_traceback(), "JoFotara Submit Error")
        raise


# =========================
# معالجة الرد
# =========================

def handle_submit_response(doc, resp: dict):
    ensure_custom_fields()

    uuid = ((resp or {}).get("uuid") or (resp or {}).get("invoiceUUID") or
            (resp or {}).get("invoice_uuid") or (resp or {}).get("id"))
    qr = ((resp or {}).get("qr") or (resp or {}).get("qrCode") or
          (resp or {}).get("qr_code") or (resp or {}).get("qrcode"))

    blob = frappe.as_json(resp) if isinstance(resp, (dict, list)) else str(resp or "")
    status = "Submitted" if (uuid or qr or "success" in blob.lower()) else "Error"

    if doc.meta.has_field("jofotara_status"):
        doc.db_set("jofotara_status", status)
    if uuid and doc.meta.has_field("jofotara_uuid"):
        doc.db_set("jofotara_uuid", uuid)
    if qr and doc.meta.has_field("jofotara_qr"):
        doc.db_set("jofotara_qr", qr)

    try:
        doc.add_comment("Comment", text=frappe.as_json(resp, indent=2))
    except Exception:
        pass


@frappe.whitelist()
def retry_pending_jobs():
    # مساحة لاحقة لإعادة المحاولة إن أردت
    pass
