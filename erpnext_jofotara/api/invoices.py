# erpnext_jofotara/api/invoices.py

from __future__ import annotations

import base64
from decimal import Decimal
from urllib.parse import urljoin

import requests
import frappe
from frappe import _

# نتأكد من وجود الحقول المخصصة على Sales Invoice قبل أي db_set
from erpnext_jofotara.install import ensure_custom_fields


# =========================
# Helpers
# =========================

def _full_url(base: str, path: str) -> str:
    """ضم Base + Path بشكل آمن."""
    if (path or "").startswith("http"):
        return path
    return urljoin((base or "").rstrip("/") + "/", (path or "").lstrip("/"))


def _get_settings():
    """جلب DocType الإعدادات."""
    return frappe.get_single("JoFotara Settings")


def _mask_headers(h: dict) -> dict:
    """إخفاء القيم الحساسة قبل التسجيل في Error Log."""
    masked = dict(h or {})
    for k in ("Secret-Key", "Authorization"):
        if k in masked and masked[k]:
            masked[k] = "********"
    return masked


def _build_headers(s) -> dict:
    """
    بناء الهيدر طبقًا لتوثيق JoFotara (بدون OAuth).
    - لو Client-ID/Secret فاضيين، نستخدم Device User/Secret.
    - إضافة Activity Number تحت المفتاح Key.
    """
    client_id = (getattr(s, "client_id", "") or getattr(s, "device_user", "") or "").strip()
    secret    = (getattr(s, "secret_key", "") or getattr(s, "device_secret", "") or "").strip()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": "ar",
        "Client-Id": client_id,
        "Secret-Key": secret,
    }
    act = (getattr(s, "activity_number", "") or "").strip()
    if act:
        headers["Key"] = act
    return headers


def _fmt(v) -> str:
    """تهيئة رقم بمرتين عشريتين كـ نص (متوافق مع UBL)."""
    return f"{Decimal(v or 0):.2f}"


def _uom_code(uom: str | None) -> str:
    """
    كود وحدة القياس في UBL (UN/ECE Rec 20).
    لو مش معروف، نستعمل C62 (Each).
    """
    return "C62"


# =========================
# UBL 2.1 (مبسّط لكنه صحيح بنيويًا)
# =========================

def generate_ubl_xml(doc) -> str:
    """
    توليد UBL 2.1 صالح بنيويًا.
    ملاحظة: قد تحتاج إضافة عناصر حسب متطلبات JoFotara النهائية.
    """
    cur = doc.currency or "JOD"
    issue_date = str(doc.posting_date)

    supplier_name = frappe.db.get_value("Company", doc.company, "company_name") or doc.company
    supplier_tax  = doc.company_tax_id or ""
    customer_name = doc.customer_name or doc.customer
    customer_tax  = doc.tax_id or ""

    # استنتاج معدل الضريبة (نأخذ أول ضريبة موجبة، وإلا 16%)
    tax_rate = 0.0
    if getattr(doc, "taxes", None):
        for tx in doc.taxes:
            if (tx.rate or 0) > 0:
                tax_rate = float(tx.rate or 0)
                break
    if tax_rate <= 0:
        tax_rate = 16.0

    tax_amt = float(doc.total_taxes_and_charges or 0)
    net     = float(doc.net_total or doc.total or 0)
    gt      = float(doc.grand_total or 0)

    # سطور الفاتورة
    lines_xml = []
    for idx, it in enumerate(doc.items, start=1):
        qty  = float(it.qty or 1)
        rate = float(it.rate or 0)
        ext  = float(it.amount or (qty * rate))
        uom  = _uom_code(getattr(it, "uom", None))
        name = frappe.utils.escape_html(it.item_name or it.item_code or "Item")

        line = f"""
  <cac:InvoiceLine>
    <cbc:ID>{idx}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="{uom}">{_fmt(qty)}</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="{cur}">{_fmt(ext)}</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>{name}</cbc:Name>
      <cac:ClassifiedTaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>{_fmt(tax_rate)}</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="{cur}">{_fmt(rate)}</cbc:PriceAmount>
      <cbc:BaseQuantity unitCode="{uom}">{_fmt(1)}</cbc:BaseQuantity>
    </cac:Price>
  </cac:InvoiceLine>"""
        lines_xml.append(line)

    lines_xml = "\n".join(lines_xml)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:UBLVersionID>2.1</cbc:UBLVersionID>
  <cbc:CustomizationID>urn:jo:jofotara:ubl:invoice</cbc:CustomizationID>
  <cbc:ProfileID>reporting:1.0</cbc:ProfileID>

  <cbc:ID>{doc.name}</cbc:ID>
  <cbc:IssueDate>{issue_date}</cbc:IssueDate>
  <cbc:InvoiceTypeCode>388</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>{cur}</cbc:DocumentCurrencyCode>

  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyName><cbc:Name>{frappe.utils.escape_html(supplier_name)}</cbc:Name></cac:PartyName>
      <cac:PartyTaxScheme>
        <cbc:CompanyID>{frappe.utils.escape_html(supplier_tax)}</cbc:CompanyID>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:PartyTaxScheme>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>{frappe.utils.escape_html(supplier_name)}</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>

  <cac:AccountingCustomerParty>
    <cac:Party>
      <cac:PartyName><cbc:Name>{frappe.utils.escape_html(customer_name)}</cbc:Name></cac:PartyName>
      <cac:PartyTaxScheme>
        <cbc:CompanyID>{frappe.utils.escape_html(customer_tax)}</cbc:CompanyID>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:PartyTaxScheme>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>{frappe.utils.escape_html(customer_name)}</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingCustomerParty>

  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="{cur}">{_fmt(tax_amt)}</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="{cur}">{_fmt(net)}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="{cur}">{_fmt(tax_amt)}</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>{_fmt(tax_rate)}</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>

  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="{cur}">{_fmt(net)}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="{cur}">{_fmt(net)}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="{cur}">{_fmt(gt)}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="{cur}">{_fmt(gt)}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>

  {lines_xml}
</Invoice>"""


# =========================
# Hook: إرسال عند الاعتماد
# =========================

def on_submit_send(doc, method=None):
    """
    يُستدعى تلقائيًا عند اعتماد Sales Invoice (من hooks.py)
    - يقرأ الإعدادات
    - يولّد/يقرأ UBL XML
    - يرسل POST إلى JoFotara
    - يحدّث الحقول ويسجّل الرد
    """
    s = _get_settings()

    # احترام الإعداد
    if not s.get("send_on_submit"):
        return

    # عدم إرسال المرتجع
    if getattr(doc, "is_return", 0):
        return

    try:
        ensure_custom_fields()

        # لو عندك حقل jofotara_xml استخدمه وإلا ولّد XML مبسّط
        xml_str = getattr(doc, "jofotara_xml", None) or generate_ubl_xml(doc)
        if not xml_str:
            frappe.throw(_("Missing UBL XML (field jofotara_xml). Please generate UBL 2.1 and try again."))

        # Base64
        xml_bytes = xml_str.encode("utf-8")
        payload = {"invoice": base64.b64encode(xml_bytes).decode()}

        # Endpoint + Headers
        url = _full_url(getattr(s, "base_url", ""), getattr(s, "submit_url", "/core/invoices/") or "/core/invoices/")
        headers = _build_headers(s)

        # Call
        r = requests.post(url, json=payload, headers=headers, timeout=90)

        # HTTP errors
        if r.status_code >= 400:
            # حاول تفسر الرد JSON لو موجود
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

        # Parse response
        if r.headers.get("content-type", "").startswith("application/json"):
            resp = r.json()
        else:
            resp = {"raw": r.text}

        handle_submit_response(doc, resp)

        frappe.msgprint(_("JoFotara: Invoice submitted successfully"), alert=1, indicator="green")

    except Exception:
        ensure_custom_fields()
        if doc.meta.has_field("jofotara_status"):
            doc.db_set("jofotara_status", "Error")
        frappe.log_error(frappe.get_traceback(), "JoFotara Submit Error")
        # ارمي نفس الخطأ للواجهة
        raise


# =========================
# معالجة الرد وتحديث الحقول
# =========================

def handle_submit_response(doc, resp: dict):
    """تحديث الحقول حسب رد JoFotara + تسجيل الرد كتعليق."""
    ensure_custom_fields()

    # محاولة لالتقاط UUID و QR من مفاتيح شائعة
    uuid = (
        (resp or {}).get("uuid")
        or (resp or {}).get("invoiceUUID")
        or (resp or {}).get("invoice_uuid")
        or (resp or {}).get("id")
    )
    qr = (
        (resp or {}).get("qr")
        or (resp or {}).get("qrCode")
        or (resp or {}).get("qr_code")
        or (resp or {}).get("qrcode")
    )

    blob = frappe.as_json(resp) if isinstance(resp, (dict, list)) else str(resp or "")
    status = "Submitted" if (uuid or qr or "success" in blob.lower()) else "Error"

    if doc.meta.has_field("jofotara_status"):
        doc.db_set("jofotara_status", status)
    if uuid and doc.meta.has_field("jofotara_uuid"):
        doc.db_set("jofotara_uuid", uuid)
    if qr and doc.meta.has_field("jofotara_qr"):
        # لو الـ API بيرجع Base64 PNG هنقدر نطبعه مباشرة في Print Format
        doc.db_set("jofotara_qr", qr)

    # سجل الرد للمرجعية
    try:
        doc.add_comment("Comment", text=frappe.as_json(resp, indent=2))
    except Exception:
        # في بعض الحالات (خاصة بعد on_submit) قد يفشل add_comment؛ نتجاهلها
        pass


# =========================
# (اختياري) إعادة محاولة لاحقًا
# =========================

@frappe.whitelist()
def retry_pending_jobs():
    """مكان لمنطق إعادة الإرسال لو اعتمدت حالة Pending مستقبلًا."""
    pass
