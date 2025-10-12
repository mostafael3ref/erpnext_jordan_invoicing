# erpnext_jofotara/api/transform.py
from __future__ import annotations

import re
from decimal import Decimal
from typing import Tuple

import frappe

__all__ = ["build_invoice_xml"]

INVOICE = "388"      # New invoice
CREDIT_NOTE = "381"  # Credit note

def _fmt(n: float | Decimal, places: int = 3) -> str:
    try:
        return f"{float(n):.{places}f}"
    except Exception:
        return f"{0.0:.{places}f}"

def _uom_code(uom: str | None) -> str:
    """Map ERPNext UOM to UBL unitCode; default PCE."""
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
        "hour": "HUR", "ساعة": "HUR",
        "day": "DAY", "يوم": "DAY",
        "month": "MON", "شهر": "MON",
        "year": "ANN", "سنة": "ANN",
    }
    return mapping.get(key, "PCE")

def _seller_info(doc) -> Tuple[str, str]:
    supplier_name = frappe.db.get_value("Company", doc.company, "company_name") or doc.company
    # Company.tax_id أو من حقل الجهة في الفاتورة
    tax_raw = (doc.company_tax_id or frappe.db.get_value("Company", doc.company, "tax_id") or "").strip()
    tax_num = re.sub(r"\D", "", tax_raw)
    if not (1 <= len(tax_num) <= 15):
        frappe.throw(f"Seller Tax Number is required (1-15 digits). Current: '{tax_raw}'")
    return supplier_name, tax_num

def _buyer_info(doc) -> Tuple[str, str, str, str, str]:
    customer_name = (doc.customer_name or doc.customer or "").strip()
    buyer_phone = (getattr(doc, "contact_mobile", None) or getattr(doc, "contact_phone", None) or "").strip()
    postal_code = ""
    try:
        if getattr(doc, "customer_address", None):
            postal_code = frappe.db.get_value("Address", doc.customer_address, "pincode") or ""
    except Exception:
        pass
    buyer_id = (doc.tax_id or "").strip()
    buyer_scheme = "TN" if buyer_id else ""  # عدّلها عند الحاجة
    return customer_name, buyer_phone, postal_code, buyer_id, buyer_scheme

def _uuid_icv(doc) -> Tuple[str, int]:
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

def _payment_method_name(doc) -> str:
    # 011 = Cash, 021 = Credit (تقدير مبسط حسب الدليل)
    is_cash = bool(getattr(doc, "is_pos", 0)) or (
        float(getattr(doc, "paid_amount", 0) or 0) >= float(getattr(doc, "grand_total", 0) or 0)
    )
    return "011" if is_cash else "021"

def _tax_rate_from_doc(doc) -> float:
    rate = 0.0
    for tx in (getattr(doc, "taxes", None) or []):
        if (tx.rate or 0) > 0:
            rate = float(tx.rate or 0)
            break
    return rate

def build_invoice_xml(si_name: str) -> str:
    """يبني XML بصيغة UBL 2.1 لواجهة JoFotara. يدعم 388 و 381 تلقائيًا."""
    doc = frappe.get_doc("Sales Invoice", si_name)

    cur = (doc.currency or "JOD").upper()
    issue_date = str(doc.posting_date)
    note = (getattr(doc, "remarks", None) or getattr(doc, "po_no", None) or "").strip()

    invoice_id = doc.name
    uuid, icv = _uuid_icv(doc)
    inv_code = CREDIT_NOTE if getattr(doc, "is_return", 0) else INVOICE
    inv_type_name = _payment_method_name(doc)

    supplier_name, supplier_tax = _seller_info(doc)
    customer_name, buyer_phone, postal_code, buyer_id, buyer_scheme = _buyer_info(doc)

    # السطور وحساب المجاميع
    total_item_discount = 0.0
    line_ext_total = 0.0
    line_blocks = []

    for idx, it in enumerate(doc.items or [], start=1):
        qty = float(it.qty or 1)
        unit_price = float(it.rate or 0)
        line_discount = float(getattr(it, "discount_amount", 0) or 0)
        net_line = (qty * unit_price) - line_discount
        total_item_discount += line_discount
        line_ext_total += net_line

        uom = _uom_code(getattr(it, "uom", None))
        name = frappe.utils.escape_html(it.item_name or it.item_code or "Item")

        line_blocks.append("\n".join([
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
        ]))

    # أرقام ERP الحقيقية إن وُجدت
    net_total = float(getattr(doc, "net_total", 0) or line_ext_total)
    tax_rate = _tax_rate_from_doc(doc)
    tax_total = float(getattr(doc, "total_taxes_and_charges", 0) or (net_total * tax_rate / 100.0))
    grand_total = float(getattr(doc, "grand_total", 0) or (net_total + tax_total))
    rounded_total = float(getattr(doc, "rounded_total", 0) or grand_total)
    rounding_adj = float(getattr(doc, "rounding_adjustment", 0) or (rounded_total - grand_total))
    allowance_total = max(total_item_discount + float(getattr(doc, "discount_amount", 0) or 0), 0.0)

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
        f'  <cbc:InvoiceTypeCode name="{inv_type_name}">{inv_code}</cbc:InvoiceTypeCode>',
        f'  <cbc:Note>{frappe.utils.escape_html(note)}</cbc:Note>',
        f'  <cbc:DocumentCurrencyCode>{cur}</cbc:DocumentCurrencyCode>',
        f'  <cbc:TaxCurrencyCode>{cur}</cbc:TaxCurrencyCode>',
        "  <cac:AdditionalDocumentReference>",
        "    <cbc:ID>ICV</cbc:ID>",
        f"    <cbc:UUID>{icv}</cbc:UUID>",
        "  </cac:AdditionalDocumentReference>",
        "",
        # Seller
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

    # إجمالي الضريبة (لو في ضريبة)
    if tax_total and tax_total != 0:
        parts += [
            "  <cac:TaxTotal>",
            f'    <cbc:TaxAmount currencyID="{cur}">{_fmt(tax_total, 3)}</cbc:TaxAmount>',
            "    <cac:TaxSubtotal>",
            f'      <cbc:TaxableAmount currencyID="{cur}">{_fmt(net_total, 3)}</cbc:TaxableAmount>',
            f'      <cbc:TaxAmount currencyID="{cur}">{_fmt(tax_total, 3)}</cbc:TaxAmount>',
            "      <cac:TaxCategory>",
            "        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>",
            "      </cac:TaxCategory>",
            "    </cac:TaxSubtotal>",
            "  </cac:TaxTotal>",
            "",
        ]

    # الخصومات + الإجماليات
    parts += [
        "  <cac:AllowanceCharge>",
        "    <cbc:ChargeIndicator>false</cbc:ChargeIndicator>",
        "    <cbc:AllowanceChargeReason>discount</cbc:AllowanceChargeReason>",
        f'    <cbc:Amount currencyID="{cur}">{_fmt(allowance_total, 3)}</cbc:Amount>',
        "  </cac:AllowanceCharge>",
        "  <cac:LegalMonetaryTotal>",
        f'    <cbc:LineExtensionAmount currencyID="{cur}">{_fmt(net_total, 3)}</cbc:LineExtensionAmount>',
        f'    <cbc:TaxExclusiveAmount currencyID="{cur}">{_fmt(net_total, 3)}</cbc:TaxExclusiveAmount>',
        f'    <cbc:TaxInclusiveAmount currencyID="{cur}">{_fmt(grand_total, 3)}</cbc:TaxInclusiveAmount>',
        f'    <cbc:AllowanceTotalAmount currencyID="{cur}">{_fmt(allowance_total, 3)}</cbc:AllowanceTotalAmount>',
        f'    <cbc:PayableRoundingAmount currencyID="{cur}">{_fmt(rounding_adj, 3)}</cbc:PayableRoundingAmount>',
        f'    <cbc:PayableAmount currencyID="{cur}">{_fmt(rounded_total, 3)}</cbc:PayableAmount>',
        "  </cac:LegalMonetaryTotal>",
        "",
        lines_xml,
        "</Invoice>",
    ]
    return "\n".join(parts)
