# erpnext_jofotara/api/invoices.py
from __future__ import annotations

import base64
import re
from decimal import Decimal, ROUND_HALF_UP
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


_Q = Decimal("0.001")
def _q(x) -> Decimal:
    try:
        return (Decimal(str(x))).quantize(_Q, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.000")


def _uom_code(uom: str | None) -> str:
    if not uom:
        return "PCE"
    key = (uom or "").strip().lower()
    mapping = {
        "unit": "PCE", "units": "PCE", "each": "PCE", "pcs": "PCE", "piece": "PCE", "nos": "PCE",
        "وحدة": "PCE", "قطعة": "PCE",
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
    if not xml_str:
        return xml_str
    s = xml_str.replace("\r", "").replace("\n", "").replace("\t", "").strip()
    s = re.sub(r">\s+<", "><", s)
    s = s.replace("\ufeff", "")
    return s


# =========================
# مشتركات
# =========================

def _is_cash_invoice(doc) -> bool:
    try:
        paid = float(getattr(doc, "paid_amount", 0) or 0)
        gt = float(getattr(doc, "grand_total", 0) or 0)
        outstanding = float(getattr(doc, "outstanding_amount", (gt - paid)) or 0)
    except Exception:
        paid, gt, outstanding = 0.0, 0.0, 0.0

    if getattr(doc, "is_pos", 0):
        return True
    if paid >= gt - 1e-3:
        return True
    if abs(outstanding) < 1e-3:
        return True

    for p in (getattr(doc, "payments", []) or []):
        m = (getattr(p, "mode_of_payment", "") or "").strip().lower()
        if m in ("cash", "bank", "card", "visa", "mastercard", "credit card", "debit card"):
            return True
    return False


def _seller_info(doc):
    supplier_name = frappe.db.get_value("Company", doc.company, "company_name") or doc.company

    s = _get_settings()
    supplier_tax_raw = (
        (doc.company_tax_id or "")
        or (frappe.db.get_value("Company", doc.company, "tax_id") or "")
        or ((getattr(s, "seller_tax_number", None) or ""))
    ).strip()

    supplier_tax = re.sub(r"\D", "", supplier_tax_raw)
    if not (1 <= len(supplier_tax) <= 15):
        frappe.throw(_("Seller Tax Number is required (1-15 digits). Current: '{0}'").format(supplier_tax_raw))
    return supplier_name, supplier_tax


def _buyer_info(doc):
    customer_name = (doc.customer_name or doc.customer or "").strip()
    buyer_phone = (getattr(doc, "contact_phone", None) or getattr(doc, "contact_mobile", None) or "").strip()
    postal_code = ""
    try:
        if getattr(doc, "customer_address", None):
            postal_code = frappe.db.get_value("Address", doc.customer_address, "pincode") or ""
    except Exception:
        pass
    buyer_id = (doc.tax_id or "").strip()
    buyer_scheme = "TN" if buyer_id else ""
    return customer_name, buyer_phone, postal_code, buyer_id, buyer_scheme


def _uuid_icv(doc):
    uuid = (getattr(doc, "jofotara_uuid", None) or frappe.generate_hash(length=36))
    try:
        if not getattr(doc, "jofotara_uuid", None) and doc.meta.has_field("jofotara_uuid"):
            doc.db_set("jofotara_uuid", uuid)
    except Exception:
        pass

    icv = int(getattr(doc, "jofotara_icv", 0) or 1)
    try:
        if doc.meta.has_field("jofotara_icv") and not getattr(doc, "jofotara_icv", None):
            doc.db_set("jofotara_icv", icv)
    except Exception:
        pass
    return uuid, icv


# =========================
# UBL 2.1 - Income Invoice
# =========================

def generate_ubl_xml_income(doc) -> str:
    s = _get_settings()

    cur = (doc.currency or "JOD").upper()
    issue_date = str(doc.posting_date)
    note = (getattr(doc, "remarks", None) or getattr(doc, "po_no", None) or "").strip()

    invoice_id = doc.name
    uuid, icv = _uuid_icv(doc)

    type_name = "011" if _is_cash_invoice(doc) else "021"

    supplier_name, supplier_tax = _seller_info(doc)
    customer_name, buyer_phone, postal_code, buyer_id, buyer_scheme = _buyer_info(doc)

    line_blocks = []
    line_ext_total = Decimal("0.000")
    for idx, it in enumerate(doc.items, start=1):
        qty = _q(it.qty or 1)
        unit_price = _q(it.rate or 0)
        line_discount = _q(getattr(it, "discount_amount", 0) or 0)
        base = _q(qty * unit_price - line_discount)

        line_ext_total += base

        uom = _uom_code(getattr(it, "uom", None))
        name = frappe.utils.escape_html(it.item_name or it.item_code or "Item")
        line_blocks.append("\n".join([
            "  <cac:InvoiceLine>",
            f"    <cbc:ID>{idx}</cbc:ID>",
            f'    <cbc:InvoicedQuantity unitCode="{uom}">{_fmt(qty)}</cbc:InvoicedQuantity>',
            f'    <cbc:LineExtensionAmount currencyID="{cur}">{_fmt(base)}</cbc:LineExtensionAmount>',
            "    <cac:Item>",
            f"      <cbc:Name>{name}</cbc:Name>",
            "    </cac:Item>",
            "    <cac:Price>",
            f'      <cbc:PriceAmount currencyID="{cur}">{_fmt(unit_price)}</cbc:PriceAmount>',
            "      <cac:AllowanceCharge>",
            "        <cbc:ChargeIndicator>false</cbc:ChargeIndicator>",
            "        <cbc:AllowanceChargeReason>DISCOUNT</cbc:AllowanceChargeReason>",
            f'        <cbc:Amount currencyID="{cur}">{_fmt(line_discount)}</cbc:Amount>',
            "      </cac:AllowanceCharge>",
            "    </cac:Price>",
            "  </cac:InvoiceLine>",
        ]))
    lines_xml = "\n".join(line_blocks)

    parts = []
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
        "  <cac:AdditionalDocumentReference>",
        "    <cbc:ID>ICV</cbc:ID>",
        f"    <cbc:UUID>{icv}</cbc:UUID>",
        "  </cac:AdditionalDocumentReference>",
        "",
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
    income_sequence = (getattr(s, "activity_number", None) or "").strip()
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

    parts += [
        "  <cac:LegalMonetaryTotal>",
        f'    <cbc:LineExtensionAmount currencyID="{cur}">{_fmt(line_ext_total)}</cbc:LineExtensionAmount>',
        f'    <cbc:TaxExclusiveAmount currencyID="{cur}">{_fmt(line_ext_total)}</cbc:TaxExclusiveAmount>',
        f'    <cbc:TaxInclusiveAmount currencyID="{cur}">{_fmt(line_ext_total)}</cbc:TaxInclusiveAmount>',
        f'    <cbc:PayableAmount currencyID="{cur}">{_fmt(line_ext_total)}</cbc:PayableAmount>',
        "  </cac:LegalMonetaryTotal>",
        "",
        lines_xml,
        "</Invoice>",
    ]
    return "\n".join(parts)


# =========================
# UBL 2.1 - Sales Invoice
# =========================

def generate_ubl_xml_sales(doc) -> str:
    cur = (doc.currency or "JOD").upper()
    issue_date = str(doc.posting_date)
    note = (getattr(doc, "remarks", None) or "").strip()

    invoice_id = doc.name
    uuid, icv = _uuid_icv(doc)

    type_name = "011" if _is_cash_invoice(doc) else "021"

    tax_rate = Decimal("0.000")
    if getattr(doc, "taxes", None):
        for tx in doc.taxes:
            if (tx.rate or 0) > 0:
                tax_rate = _q(tx.rate or 0)
                break
    if tax_rate <= 0:
        tax_rate = Decimal("16.000")

    supplier_name, supplier_tax = _seller_info(doc)
    customer_name, buyer_phone, postal_code, buyer_id, buyer_scheme = _buyer_info(doc)

    line_blocks = []
    net_total = Decimal("0.000")
    tax_total = Decimal("0.000")
    grand_total = Decimal("0.000")

    for idx, it in enumerate(doc.items, start=1):
        qty = _q(it.qty or 1)
        rate = _q(it.rate or 0)
        line_disc = _q(getattr(it, "discount_amount", 0) or 0)

        base = _q(qty * rate - line_disc)
        tax_amt = _q(base * (tax_rate / Decimal("100")))

        net_total += base
        tax_total += tax_amt
        grand_total += _q(base + tax_amt)

        uom = _uom_code(getattr(it, "uom", None))
        name = frappe.utils.escape_html(it.item_name or it.item_code or "Item")

        line_blocks.append("\n".join([
            "  <cac:InvoiceLine>",
            f"    <cbc:ID>{idx}</cbc:ID>",
            f'    <cbc:InvoicedQuantity unitCode="{uom}">{_fmt(qty)}</cbc:InvoicedQuantity>',
            f'    <cbc:LineExtensionAmount currencyID="{cur}">{_fmt(base)}</cbc:LineExtensionAmount>',
            "    <cac:Item>",
            f"      <cbc:Name>{name}</cbc:Name>",
            "      <cac:ClassifiedTaxCategory>",
            "        <cbc:ID>S</cbc:ID>",
            f"        <cbc:Percent>{_fmt(tax_rate)}</cbc:Percent>",
            "        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>",
            "      </cac:ClassifiedTaxCategory>",
            "    </cac:Item>",
            "    <cac:Price>",
            f'      <cbc:PriceAmount currencyID="{cur}">{_fmt(rate)}</cbc:PriceAmount>',
            f'      <cbc:BaseQuantity unitCode="{uom}">{_fmt(1)}</cbc:BaseQuantity>',
            "      <cac:AllowanceCharge>",
            "        <cbc:ChargeIndicator>false</cbc:ChargeIndicator>",
            "        <cbc:AllowanceChargeReason>DISCOUNT</cbc:AllowanceChargeReason>",
            f'        <cbc:Amount currencyID="{cur}">{_fmt(line_disc)}</cbc:Amount>',
            "      </cac:AllowanceCharge>",
            "    </cac:Price>",
            "  </cac:InvoiceLine>",
        ]))

    lines_xml = "\n".join(line_blocks)

    s = _get_settings()
    activity_number = (getattr(s, "activity_number", None) or "").strip()

    parts = []
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
        "  <cac:AdditionalDocumentReference>",
        "    <cbc:ID>ICV</cbc:ID>",
        f"    <cbc:UUID>{icv}</cbc:UUID>",
        "  </cac:AdditionalDocumentReference>",
        "",
        "  <cac:AccountingSupplierParty>",
        "    <cac:Party>",
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
        "  </cac:AccountingCustomerParty>",
        "",
    ]
    if activity_number:
        parts += [
            "  <cac:SellerSupplierParty>",
            "    <cac:Party>",
            "      <cac:PartyIdentification>",
            f"        <cbc:ID>{frappe.utils.escape_html(activity_number)}</cbc:ID>",
            "      </cac:PartyIdentification>",
            "    </cac:Party>",
            "  </cac:SellerSupplierParty>",
            "",
        ]

    parts += [
        "  <cac:TaxTotal>",
        f'    <cbc:TaxAmount currencyID="{cur}">{_fmt(tax_total)}</cbc:TaxAmount>',
        "    <cac:TaxSubtotal>",
        f'      <cbc:TaxableAmount currencyID="{cur}">{_fmt(net_total)}</cbc:TaxableAmount>',
        f'      <cbc:TaxAmount currencyID="{cur}">{_fmt(tax_total)}</cbc:TaxAmount>',
        "      <cac:TaxCategory>",
        "        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>",
        "      </cac:TaxCategory>",
        "    </cac:TaxSubtotal>",
        "  </cac:TaxTotal>",
        "",
        "  <cac:LegalMonetaryTotal>",
        f'    <cbc:LineExtensionAmount currencyID="{cur}">{_fmt(net_total)}</cbc:LineExtensionAmount>',
        f'    <cbc:TaxExclusiveAmount currencyID="{cur}">{_fmt(net_total)}</cbc:TaxExclusiveAmount>',
        f'    <cbc:TaxInclusiveAmount currencyID="{cur}">{_fmt(grand_total)}</cbc:TaxInclusiveAmount>',
        f'    <cbc:PayableAmount currencyID="{cur}">{_fmt(grand_total)}</cbc:PayableAmount>',
        "  </cac:LegalMonetaryTotal>",
        "",
        lines_xml,
        "</Invoice>",
    ]
    return "\n".join(parts)


# =========================
# اختيار النوع (AUTO)
# =========================

def _has_vat(doc) -> bool:
    try:
        for tx in (getattr(doc, "taxes", None) or []):
            if float(tx.rate or 0) > 0 or float(tx.tax_amount or 0) > 0:
                return True
    except Exception:
        pass
    return False


def generate_ubl_xml(doc) -> str:
    """
    يختار Income أو Sales تلقائيًا:
      - لو في ضريبة > 0 => Sales
      - غير كده => Income
    ولو الإعدادات حددت نوع، بنحترمه، لكن لو اختير income وفي ضريبة، نجبر Sales لتفادي الرفض.
    """
    s = _get_settings()
    setting = (getattr(s, "invoice_template", None) or "").strip().lower()
    has_vat = _has_vat(doc)

    chosen = setting or "auto"
    if chosen == "sales" or (chosen != "income" and has_vat):
        xml = generate_ubl_xml_sales(doc)
        try:
            frappe.log_error("JoFotara XML built as SALES", "JoFotara DEBUG")
        except Exception:
            pass
        return xml

    if chosen == "income" and has_vat:
        # إجبار Sales لو فيه ضريبة رغم اختيار income
        xml = generate_ubl_xml_sales(doc)
        try:
            frappe.log_error("Forcing SALES because invoice has VAT", "JoFotara DEBUG")
        except Exception:
            pass
        return xml

    try:
        frappe.log_error("JoFotara XML built as INCOME", "JoFotara DEBUG")
    except Exception:
        pass
    return generate_ubl_xml_income(doc)


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

        def _build_payload(_doc):
            xml_str = getattr(_doc, "jofotara_xml", None) or generate_ubl_xml(_doc)
            if not xml_str:
                frappe.throw(_("Missing UBL XML (field jofotara_xml). Please generate UBL 2.1 and try again."))
            xml_min = _minify_xml(xml_str)
            xml_bytes = xml_min.encode("utf-8")
            return {"payload": {"invoice": base64.b64encode(xml_bytes).decode()}, "xml_min": xml_min}

        data = _build_payload(doc)
        url = _full_url(getattr(s, "base_url", ""), getattr(s, "submit_url", "/core/invoices/") or "/core/invoices/")
        headers = _build_headers(s)

        if frappe.conf.get("developer_mode"):
            try:
                itc_match = re.search(r"<cbc:InvoiceTypeCode[^>]*>.*?</cbc:InvoiceTypeCode>", data["xml_min"])
                itc_text = itc_match.group(0) if itc_match else "NOT FOUND"
                frappe.log_error(
                    message=f"InvoiceTypeCode: {itc_text}\nXML (first 800):\n{data['xml_min'][:800]}",
                    title="JoFotara DEBUG - TypeCode"
                )
            except Exception:
                pass

        r = requests.post(url, json=data["payload"], headers=headers, timeout=90)

        need_retry_as_sales = False
        if r.status_code >= 400:
            try:
                j = r.json()
                msg = frappe.as_json(j)
                if "not authorized to submit this type of invoice" in msg.lower():
                    need_retry_as_sales = True
            except Exception:
                pass

        if need_retry_as_sales:
            xml_sales = generate_ubl_xml_sales(doc)
            xml_min = _minify_xml(xml_sales)
            payload = {"invoice": base64.b64encode(xml_min.encode("utf-8")).decode()}
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
                    f"Payload keys: ['invoice']\n"
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
    pass
