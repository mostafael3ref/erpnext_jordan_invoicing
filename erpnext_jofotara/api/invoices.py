# erpnext_jofotara/api/invoices.py

import base64
from urllib.parse import urljoin

import requests
import frappe
from frappe import _

# يتأكد أن الحقول المخصصة على Sales Invoice موجودة قبل أي db_set
from erpnext_jofotara.install import ensure_custom_fields


# ---------------------------
# Helpers
# ---------------------------

def _full_url(base: str, path: str) -> str:
    """ضم Base + Path بشكل آمن."""
    if (path or "").startswith("http"):
        return path
    return urljoin(base.rstrip("/") + "/", (path or "").lstrip("/"))


def _get_settings():
    """DocType الإعدادات"""
    # اسم الدوكتايب عندك "JoFotara Settings"
    return frappe.get_single("JoFotara Settings")


# ---------------------------
# توليد UBL XML مبسّط (مؤقتًا)
# ---------------------------

def generate_ubl_xml(doc) -> str:
    """
    يولّد UBL 2.1 مبسّط من الفاتورة.
    ملاحظة: قد تحتاج إضافة عناصر حسب متطلبات JoFotara لاحقًا (تصنيف الضريبة، أكواد، ...إلخ).
    """
    cur = doc.currency or "JOD"
    issue_date = str(doc.posting_date)

    supplier_name = (
        frappe.db.get_value("Company", doc.company, "company_name") or doc.company
    )
    supplier_tax = doc.company_tax_id or ""
    customer_name = doc.customer_name or doc.customer
    customer_tax = doc.tax_id or ""

    # سطور الفاتورة
    lines = []
    for i, it in enumerate(doc.items, start=1):
        qty = float(it.qty or 1)
        rate = float(it.rate or 0)
        ext = float(it.amount or (qty * rate))
        uom = (it.uom or "EA")
        name = frappe.utils.escape_html(it.item_name or it.item_code or "Item")
        lines.append(
            f"""
  <cac:InvoiceLine>
    <cbc:ID>{i}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="{uom}">{qty:.3f}</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="{cur}">{ext:.3f}</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>{name}</cbc:Name></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="{cur}">{rate:.3f}</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>"""
        )

    lines_xml = "\n".join(lines)
    tax_amt = float(doc.total_taxes_and_charges or 0)
    net = float(doc.net_total or doc.total or 0)
    gt = float(doc.grand_total or 0)

    # XML
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:jo:jofotara:ubl:invoice</cbc:CustomizationID>
  <cbc:ProfileID>reporting:1.0</cbc:ProfileID>
  <cbc:ID>{doc.name}</cbc:ID>
  <cbc:IssueDate>{issue_date}</cbc:IssueDate>
  <cbc:InvoiceTypeCode>388</cbc:InvoiceTypeCode>

  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyName><cbc:Name>{frappe.utils.escape_html(supplier_name)}</cbc:Name></cac:PartyName>
      <cac:PartyTaxScheme><cbc:CompanyID>{supplier_tax}</cbc:CompanyID></cac:PartyTaxScheme>
    </cac:Party>
  </cac:AccountingSupplierParty>

  <cac:AccountingCustomerParty>
    <cac:Party>
      <cac:PartyName><cbc:Name>{frappe.utils.escape_html(customer_name)}</cbc:Name></cac:PartyName>
      <cac:PartyTaxScheme><cbc:CompanyID>{customer_tax}</cbc:CompanyID></cac:PartyTaxScheme>
    </cac:Party>
  </cac:AccountingCustomerParty>

  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="{cur}">{tax_amt:.3f}</cbc:TaxAmount>
  </cac:TaxTotal>

  <cac:LegalMonetaryTotal>
    <cbc:TaxExclusiveAmount currencyID="{cur}">{net:.3f}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="{cur}">{gt:.3f}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="{cur}">{gt:.3f}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>

  {lines_xml}
</Invoice>"""


# ---------------------------
# Hook: إرسال عند الاعتماد
# ---------------------------

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

        # لو عندك حقل مخصص jofotara_xml استخدمه، وإلا ولّد XML مبسّط مؤقتًا
        xml_str = getattr(doc, "jofotara_xml", None) or generate_ubl_xml(doc)
        if not xml_str:
            frappe.throw(
                _("Missing UBL XML (field jofotara_xml). Please generate UBL 2.1 and try again.")
            )

        # Base64
        xml_bytes = xml_str.encode("utf-8")
        payload = {"invoice": base64.b64encode(xml_bytes).decode()}

        # Endpoint + Headers
        url = _full_url(s.base_url, s.submit_url or "/core/invoices/")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Client-Id": s.client_id,
            "Secret-Key": s.secret_key,
            "Accept-Language": "ar",
        }

        # Call
        r = requests.post(url, json=payload, headers=headers, timeout=90)
        if r.status_code >= 400:
            # رجّع رسالة واضحة من السيرفر
            raise frappe.ValidationError(f"JoFotara API error {r.status_code}: {r.text}")

        # JSON or raw
        if r.headers.get("content-type", "").startswith("application/json"):
            resp = r.json()
        else:
            resp = {"raw": r.text}

        handle_submit_response(doc, resp)

        frappe.msgprint(_("JoFotara: Invoice submitted successfully"), alert=1, indicator="green")

    except Exception as e:
        ensure_custom_fields()
        if doc.meta.has_field("jofotara_status"):
            doc.db_set("jofotara_status", "Error")
        frappe.log_error(frappe.get_traceback(), "JoFotara Submit Error")
        frappe.throw(_("JoFotara submission failed: {0}").format(str(e)))


# ---------------------------
# معالجة الرد وتحديث الحقول
# ---------------------------

def handle_submit_response(doc, resp: dict):
    """حدّث الحقول حسب رد JoFotara + سجّل الرد كتعليق."""
    ensure_custom_fields()

    # حاول التقاط UUID و QR من مفاتيح شائعة
    uuid = (
        resp.get("uuid")
        or resp.get("invoiceUUID")
        or resp.get("invoice_uuid")
        or resp.get("id")
    )
    qr = (
        resp.get("qr")
        or resp.get("qrCode")
        or resp.get("qr_code")
        or resp.get("qrcode")
    )

    # حدّد النجاح
    text_blob = frappe.as_json(resp) if isinstance(resp, (dict, list)) else str(resp)
    status = "Submitted" if (uuid or qr or "success" in text_blob.lower()) else "Error"

    if doc.meta.has_field("jofotara_status"):
        doc.db_set("jofotara_status", status)
    if uuid and doc.meta.has_field("jofotara_uuid"):
        doc.db_set("jofotara_uuid", uuid)
    # لو رجع QR كـ Base64 PNG اطبعه في الطباعة:
    if qr and doc.meta.has_field("jofotara_qr"):
        doc.db_set("jofotara_qr", qr)

    # سجّل الرد في تعليق مرجعي على الفاتورة
    doc.add_comment("Comment", text=frappe.as_json(resp, indent=2))


# ---------------------------
# (اختياري) إعادة محاولة لاحقًا
# ---------------------------

@frappe.whitelist()
def retry_pending_jobs():
    """مكان لمُنطق إعادة الإرسال لو اعتمدت حالة Pending مستقبلاً."""
    pass
