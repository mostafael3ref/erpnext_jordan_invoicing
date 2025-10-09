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


# ---------------------------
# Helpers
# ---------------------------

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
    # credentials (with device fallback)
    client_id     = (s.client_id or "").strip()
    client_secret = (s.get_password("secret_key", raise_exception=False) or "").strip()
    device_user   = (s.device_user or "").strip()
    device_secret = (s.get_password("device_secret", raise_exception=False) or "").strip()

    if not client_id and device_user:
        client_id = device_user
    if not client_secret and device_secret:
        client_secret = device_secret

    if not client_id or not client_secret:
        frappe.throw(_("JoFotara Settings is missing credentials. "
                       "Fill either Client ID/Secret (OAuth2) or Device User/Secret."))

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
    if not uom:
        return "C62"
    key = (uom or "").strip().lower()
    mapping = {
        "unit": "C62", "units": "C62", "each": "C62", "pcs": "C62", "piece": "C62",
        "nos": "C62", "وحدة": "C62", "قطعة": "C62",
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
    return mapping.get(key, "C62")


def _minify_xml(xml_str: str) -> str:
    """Minify XML safely (keep text spaces, remove formatting)."""
    if not xml_str:
        return xml_str
    s = xml_str.replace("\r", "").replace("\n", "").replace("\t", "").strip()
    s = re.sub(r">\s+<", "><", s)
    s = s.replace("\ufeff", "")
    return s


# ---------------------------
# UBL 2.1 generator (structurally valid)
# ---------------------------

def _invoice_type_code(doc) -> str:
    """388 = Tax Invoice, 381 = Credit Note, 383 = Debit Note."""
    if getattr(doc, "is_return", 0):
        return "381"
    if getattr(doc, "is_debit_note", 0):
        return "383"
    return "388"


def generate_ubl_xml(doc) -> str:
    cur = doc.currency or "JOD"
    issue_date = str(doc.posting_date)

    supplier_name = frappe.db.get_value("Company", doc.company, "company_name") or doc.company
    supplier_tax  = doc.company_tax_id or ""
    customer_name = doc.customer_name or doc.customer
    customer_tax  = doc.tax_id or ""

    # tax rate
    tax_rate = 0.0
    if getattr(doc, "taxes", None):
        for tx in doc.taxes:
            if (tx.rate or 0) > 0:
                tax_rate = float(tx.rate or 0)
                break

    tax_amt = float(doc.total_taxes_and_charges or 0)
    net     = float(doc.net_total or doc.total or 0)
    gt      = float(doc.grand_total or 0)

    if tax_rate <= 0 and net > 0 and tax_amt > 0:
        tax_rate = (tax_amt / net) * 100.0
    if tax_rate <= 0:
        tax_rate = 16.0

    inv_code = _invoice_type_code(doc)

    # lines
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

    # IMPORTANT: add list attributes to InvoiceTypeCode
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:UBLVersionID>2.1</cbc:UBLVersionID>
  <cbc:CustomizationID>urn:jo:jofotara:ubl:invoice</cbc:CustomizationID>
  <cbc:ProfileID>reporting:1.0</cbc:ProfileID>

  <cbc:ID>{doc.name}</cbc:ID>
  <cbc:IssueDate>{issue_date}</cbc:IssueDate>
  <cbc:InvoiceTypeCode listID="UNCL1001" listAgencyID="6" listVersionID="D16B">{inv_code}</cbc:InvoiceTypeCode>
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


# ---------------------------
# Hook: send on submit
# ---------------------------

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

        # Minify to satisfy "Invoice Minification"
        xml_min = _minify_xml(xml_str)

        payload = {"invoice": base64.b64encode(xml_min.encode("utf-8")).decode()}
        url = _full_url(getattr(s, "base_url", ""), getattr(s, "submit_url", "/core/invoices/") or "/core/invoices/")
        headers = _build_headers(s)

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


# ---------------------------
# Response handling
# ---------------------------

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


# ---------------------------
# Retry hook (optional)
# ---------------------------

@frappe.whitelist()
def retry_pending_jobs():
    pass
