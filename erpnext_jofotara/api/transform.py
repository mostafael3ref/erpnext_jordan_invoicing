# -*- coding: utf-8 -*-
# erpnext_jofotara/api/transform.py

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Tuple
from xml.etree.ElementTree import Element, SubElement, tostring
import xml.etree.ElementTree as ET
import json
import re
import uuid

import frappe
from frappe.utils import getdate

# ================================
# Namespaces & Constants
# ================================

NS = {
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
}
for p, uri in NS.items():
    ET.register_namespace("" if p == "inv" else p, uri)

CURRENCY_CODE_DOC = "JOD"   # header codes
CURRENCY_ID_AMT = "JO"      # inside monetary amounts
FMT3 = Decimal("0.001")

VAT_SCHEME_AGENCY = "6"
VAT_SCHEME_5305 = "UN/ECE 5305"
VAT_SCHEME_5153 = "UN/ECE 5153"

INVOICE = "388"
CREDIT_NOTE = "381"

# ================================
# Helpers
# ================================

def _qn(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"

def _dec(x) -> Decimal:
    if x is None:
        return Decimal("0")
    return Decimal(str(x))

def _q3(x) -> Decimal:
    return _dec(x).quantize(FMT3, rounding=ROUND_HALF_UP)

def _fmt(x, places: int = 3) -> str:
    return f"{_q3(x):.{places}f}"

def _fmt_qty(x) -> str:
    # زي Odoo: كمية بعُشر واحد (1.0 / 25.0)
    return f"{float(x):.1f}"

def _get_settings():
    return frappe.get_single("JoFotara Settings")

def _company_info(company: str) -> Tuple[dict, str]:
    """
    يرجّع (company_doc_as_dict, tax_id_fallback)
    """
    tax = ""
    cd = {}
    try:
        c = frappe.get_doc("Company", company)
        cd = c.as_dict()
        for f in ("tax_id", "company_tax_id", "tax_no", "tax_number"):
            if getattr(c, f, None):
                tax = str(getattr(c, f)).strip()
                break
    except Exception:
        pass
    if not tax:
        try:
            s = _get_settings()
            tax = (getattr(s, "seller_tax_number", "") or "").strip()
        except Exception:
            tax = ""
    return cd, tax

def _company_postal_zone(company_doc: dict) -> str:
    try:
        addr_link = frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Company", "link_name": company_doc.get("name"), "parenttype": "Address"},
            fields=["parent"], limit=1
        )
        if addr_link:
            addr = frappe.get_doc("Address", addr_link[0]["parent"])
            for f in ("pincode", "zip", "postal_code", "po_box"):
                v = (getattr(addr, f, None) or "").strip()
                if v:
                    return v
    except Exception:
        pass
    return ""

def _customer_name(doc) -> str:
    nm = (getattr(doc, "customer_name", "") or getattr(doc, "customer", "") or "").strip()
    if nm:
        return nm
    try:
        cust = frappe.get_doc("Customer", doc.customer)
        return (getattr(cust, "customer_name", "") or cust.name or "Consumer").strip()
    except Exception:
        return "Consumer"

def _activity_number() -> str:
    s = _get_settings()
    raw = (getattr(s, "activity_number", "") or "").strip()
    return re.sub(r"\D", "", raw)

def _uom_code(u: str | None) -> str:
    m = {
        "unit": "PCE", "units": "PCE", "each": "PCE", "pcs": "PCE", "piece": "PCE", "nos": "PCE",
        "قطعة": "PCE", "وحدة": "PCE", "صندوق": "BOX", "box": "BOX",
        "kg": "KGM", "كيلو": "KGM", "kilogram": "KGM",
        "g": "GRM", "جرام": "GRM",
        "m": "MTR", "meter": "MTR", "متر": "MTR",
        "cm": "CMT", "سم": "CMT", "mm": "MMT",
        "m2": "MTK", "sq m": "MTK", "متر مربع": "MTK",
        "l": "LTR", "liter": "LTR", "لتر": "LTR",
        "hour": "HUR", "ساعة": "HUR", "day": "DAY", "يوم": "DAY",
    }
    key = (u or "").strip().lower()
    return m.get(key, "PCE")

def _parse_item_vat_rate(item) -> Decimal:
    try:
        txt = getattr(item, "item_tax_rate", "") or ""
        if txt:
            d = json.loads(txt)
            for _, v in d.items():
                rate = _dec(v)
                if abs(rate) > 0:
                    return rate
    except Exception:
        pass
    return Decimal("0")

def _global_vat_rate(doc) -> Decimal:
    try:
        for t in (doc.taxes or []):
            rate = _dec(getattr(t, "rate", 0))
            if abs(rate) > 0:
                return rate
    except Exception:
        pass
    return Decimal("16.0")

# ================================
# Public: build UBL XML
# ================================

def build_invoice_xml(sales_invoice_name: str) -> str:
    """
    يولّد UBL 2.1 بمعيار Odoo المقبول لدى JoFotara:
      - ProfileID=reporting:1.0
      - name="022" مع القيمة 388 للفاتورة، 381 للمرتجع
      - Document/TaxCurrencyCode = JOD، وكل currencyID داخل المبالغ = JO
      - الفاتورة: Header TaxTotal بدون Subtotal
      - المرتجع: Header TaxTotal + TaxSubtotal
      - AllowanceCharge=0.000 في الهيدر وتحت السعر
      - الكميات في المرتجع موجبة (زي Odoo)
      - BillingReference و PaymentMeans في المرتجع
    """
    doc = frappe.get_doc("Sales Invoice", sales_invoice_name)

    is_return = int(getattr(doc, "is_return", 0) or 0) == 1
    issue_date = str(getdate(getattr(doc, "posting_date", None)) or getdate())
    inv_code = CREDIT_NOTE if is_return else INVOICE
    inv_name_attr = "022"  # ثابت زي المثال المقبول

    company_doc, supplier_tax = _company_info(doc.company)
    supplier_name = (company_doc.get("company_name") or company_doc.get("name") or doc.company).strip()
    customer_name = _customer_name(doc)
    activity = _activity_number()

    currency_doc = (doc.currency or CURRENCY_CODE_DOC).upper() or CURRENCY_CODE_DOC
    cur_id = CURRENCY_ID_AMT

    # ===== totals from lines =====
    lines: List[Dict] = []
    net_sum = Decimal("0.0")
    vat_sum = Decimal("0.0")
    header_discount = _dec(getattr(doc, "discount_amount", 0) or 0)
    global_vat = _global_vat_rate(doc)

    for it in (doc.items or []):
        raw_qty = _dec(getattr(it, "qty", 0) or 0)
        qty = abs(raw_qty) if is_return else raw_qty
        rate = abs(_dec(getattr(it, "rate", 0) or 0)) if is_return else _dec(getattr(it, "rate", 0) or 0)
        unit_code = _uom_code(getattr(it, "uom", None))
        line_disc = abs(_dec(getattr(it, "discount_amount", 0) or 0)) if is_return else _dec(getattr(it, "discount_amount", 0) or 0)

        vat_rate = _parse_item_vat_rate(it) or global_vat

        line_net = (qty * rate) - line_disc
        if line_net < 0:
            line_net = Decimal("0.0")
        line_vat = (line_net * vat_rate / Decimal("100"))

        net_sum += line_net
        vat_sum += line_vat

        item_name = (getattr(it, "item_name", "") or getattr(it, "item_code", "") or getattr(it, "description", "") or "Item").strip() or "Item"

        lines.append({
            "name": item_name,
            "qty": qty,
            "unit_code": unit_code,
            "unit_price": rate,
            "line_net": line_net,
            "vat_rate": vat_rate,   # % مثل 16.0
            "line_vat": line_vat,
            "line_disc": line_disc,
        })

    net_after_header_disc = net_sum - header_discount
    if net_after_header_disc < 0:
        net_after_header_disc = Decimal("0.0")

    inclusive_total = net_after_header_disc + vat_sum
    payable = inclusive_total

    # ===== XML =====
    inv = Element(_qn("inv", "Invoice"))

    # Header
    SubElement(inv, _qn("cbc", "ProfileID")).text = "reporting:1.0"
    SubElement(inv, _qn("cbc", "ID")).text = str(doc.name)
    # استخدم UUID محفوظ إن وجد، وإلا أنشئ واحد جديد
    uuid_value = getattr(doc, "jofotara_uuid", "") or ""
    if not uuid_value:
        uuid_value = str(uuid.uuid4())
    SubElement(inv, _qn("cbc", "UUID")).text = uuid_value
    SubElement(inv, _qn("cbc", "IssueDate")).text = issue_date
    SubElement(inv, _qn("cbc", "InvoiceTypeCode"), {"name": inv_name_attr}).text = inv_code
    SubElement(inv, _qn("cbc", "DocumentCurrencyCode")).text = currency_doc
    SubElement(inv, _qn("cbc", "TaxCurrencyCode")).text = currency_doc

    # للمرتجع: BillingReference + PaymentMeans مثل Odoo
    if is_return:
        original_id = getattr(doc, "return_against", "") or getattr(doc, "amended_from", "") or ""
        orig_uuid = ""
        orig_total = None
        if original_id:
            try:
                orig = frappe.get_doc("Sales Invoice", original_id)
                orig_total = _dec(getattr(orig, "grand_total", 0) or 0)
                # لو عندك حقل UUID محفوظ في الأصل
                orig_uuid = getattr(orig, "jofotara_uuid", "") or ""
            except Exception:
                pass

        # BillingReference
        br = SubElement(inv, _qn("cac", "BillingReference"))
        invref = SubElement(br, _qn("cac", "InvoiceDocumentReference"))
        if original_id:
            SubElement(invref, _qn("cbc", "ID")).text = original_id
        if orig_uuid:
            SubElement(invref, _qn("cbc", "UUID")).text = orig_uuid
        if orig_total is not None:
            # Odoo يضع الإجمالي الشامل كنص في DocumentDescription
            SubElement(invref, _qn("cbc", "DocumentDescription")).text = _fmt(orig_total)

        # PaymentMeans
        pm = SubElement(inv, _qn("cac", "PaymentMeans"))
        SubElement(pm, _qn("cbc", "PaymentMeansCode"), {"listID": "UN/ECE 4461"}).text = "10"
        reason = getattr(doc, "remarks", "") or "مرتجع"
        if original_id:
            note = f"عكس: {original_id}, {reason}"
        else:
            note = reason
        SubElement(pm, _qn("cbc", "InstructionNote")).text = note

    # AdditionalDocumentReference: ICV
    add_doc = SubElement(inv, _qn("cac", "AdditionalDocumentReference"))
    SubElement(add_doc, _qn("cbc", "ID")).text = "ICV"
    SubElement(add_doc, _qn("cbc", "UUID")).text = "1"

    # Supplier
    acc_sup = SubElement(inv, _qn("cac", "AccountingSupplierParty"))
    party = SubElement(acc_sup, _qn("cac", "Party"))
    addr = SubElement(party, _qn("cac", "PostalAddress"))
    pz = _company_postal_zone(company_doc)
    if pz:
        SubElement(addr, _qn("cbc", "PostalZone")).text = pz
    SubElement(addr, _qn("cbc", "CountrySubentityCode")).text = "JO-AM"
    ctry = SubElement(addr, _qn("cac", "Country"))
    SubElement(ctry, _qn("cbc", "IdentificationCode")).text = "JO"

    pts = SubElement(party, _qn("cac", "PartyTaxScheme"))
    if supplier_tax:
        SubElement(pts, _qn("cbc", "CompanyID")).text = supplier_tax
    ts = SubElement(pts, _qn("cac", "TaxScheme"))
    SubElement(ts, _qn("cbc", "ID")).text = "VAT"

    ple = SubElement(party, _qn("cac", "PartyLegalEntity"))
    SubElement(ple, _qn("cbc", "RegistrationName")).text = supplier_name

    # Customer
    acc_cus = SubElement(inv, _qn("cac", "AccountingCustomerParty"))
    party = SubElement(acc_cus, _qn("cac", "Party"))

    pid = SubElement(party, _qn("cac", "PartyIdentification"))
    SubElement(pid, _qn("cbc", "ID"), {"schemeID": "TN"})

    addr = SubElement(party, _qn("cac", "PostalAddress"))
    SubElement(addr, _qn("cbc", "CountrySubentityCode")).text = "JO-AM"
    ctry = SubElement(addr, _qn("cac", "Country"))
    SubElement(ctry, _qn("cbc", "IdentificationCode")).text = "JO"

    pts = SubElement(party, _qn("cac", "PartyTaxScheme"))
    ts = SubElement(pts, _qn("cac", "TaxScheme"))
    SubElement(ts, _qn("cbc", "ID")).text = "VAT"

    ple = SubElement(party, _qn("cac", "PartyLegalEntity"))
    SubElement(ple, _qn("cbc", "RegistrationName")).text = customer_name

    # SellerSupplierParty (Activity)
    if activity:
        ssp = SubElement(inv, _qn("cac", "SellerSupplierParty"))
        p2 = SubElement(ssp, _qn("cac", "Party"))
        pid2 = SubElement(p2, _qn("cac", "PartyIdentification"))
        SubElement(pid2, _qn("cbc", "ID")).text = activity

    # Header AllowanceCharge (0)
    ac = SubElement(inv, _qn("cac", "AllowanceCharge"))
    SubElement(ac, _qn("cbc", "ChargeIndicator")).text = "false"
    SubElement(ac, _qn("cbc", "AllowanceChargeReason")).text = "discount"
    SubElement(ac, _qn("cbc", "Amount"), {"currencyID": cur_id}).text = _fmt(header_discount)

    # Header TaxTotal
    head_tax = SubElement(inv, _qn("cac", "TaxTotal"))
    SubElement(head_tax, _qn("cbc", "TaxAmount"), {"currencyID": cur_id}).text = _fmt(vat_sum)
    if is_return:
        # زي Odoo في المرتجع: نضيف TaxSubtotal في الهيدر
        hts = SubElement(head_tax, _qn("cac", "TaxSubtotal"))
        SubElement(hts, _qn("cbc", "TaxableAmount"), {"currencyID": cur_id}).text = _fmt(net_after_header_disc)
        SubElement(hts, _qn("cbc", "TaxAmount"), {"currencyID": cur_id}).text = _fmt(vat_sum)
        tcat = SubElement(hts, _qn("cac", "TaxCategory"))
        SubElement(tcat, _qn("cbc", "ID"), {"schemeAgencyID": VAT_SCHEME_AGENCY, "schemeID": VAT_SCHEME_5305}).text = "S"
        SubElement(tcat, _qn("cbc", "Percent")).text = f"{_q3(global_vat):.1f}"
        tsch = SubElement(tcat, _qn("cac", "TaxScheme"))
        SubElement(tsch, _qn("cbc", "ID"), {"schemeAgencyID": VAT_SCHEME_AGENCY, "schemeID": VAT_SCHEME_5153}).text = "VAT"

    # LegalMonetaryTotal
    lmt = SubElement(inv, _qn("cac", "LegalMonetaryTotal"))
    SubElement(lmt, _qn("cbc", "TaxExclusiveAmount"), {"currencyID": cur_id}).text = _fmt(net_after_header_disc)
    SubElement(lmt, _qn("cbc", "TaxInclusiveAmount"), {"currencyID": cur_id}).text = _fmt(inclusive_total)
    SubElement(lmt, _qn("cbc", "AllowanceTotalAmount"), {"currencyID": cur_id}).text = _fmt(header_discount)
    if is_return:
        SubElement(lmt, _qn("cbc", "PrepaidAmount"), {"currencyID": cur_id}).text = _fmt(0)
    SubElement(lmt, _qn("cbc", "PayableAmount"), {"currencyID": cur_id}).text = _fmt(payable)

    # Lines
    single_line = (len(lines) == 1)
    for idx, L in enumerate(lines, start=1):
        il = SubElement(inv, _qn("cac", "InvoiceLine"))
        SubElement(il, _qn("cbc", "ID")).text = str(idx)
        SubElement(il, _qn("cbc", "InvoicedQuantity"), {"unitCode": L["unit_code"]}).text = _fmt_qty(L["qty"])
        SubElement(il, _qn("cbc", "LineExtensionAmount"), {"currencyID": cur_id}).text = _fmt(L["line_net"])

        # Line TaxTotal + Subtotal
        ttotal = SubElement(il, _qn("cac", "TaxTotal"))
        SubElement(ttotal, _qn("cbc", "TaxAmount"), {"currencyID": cur_id}).text = _fmt(L["line_vat"])
        if single_line:
            SubElement(ttotal, _qn("cbc", "RoundingAmount"), {"currencyID": cur_id}).text = _fmt(payable)

        tsub = SubElement(ttotal, _qn("cac", "TaxSubtotal"))
        SubElement(tsub, _qn("cbc", "TaxableAmount"), {"currencyID": cur_id}).text = _fmt(L["line_net"])
        SubElement(tsub, _qn("cbc", "TaxAmount"), {"currencyID": cur_id}).text = _fmt(L["line_vat"])
        tcat = SubElement(tsub, _qn("cac", "TaxCategory"))
        SubElement(tcat, _qn("cbc", "ID"), {"schemeAgencyID": VAT_SCHEME_AGENCY, "schemeID": VAT_SCHEME_5305}).text = "S"
        SubElement(tcat, _qn("cbc", "Percent")).text = f"{_q3(L['vat_rate']):.1f}"
        tsch = SubElement(tcat, _qn("cac", "TaxScheme"))
        SubElement(tsch, _qn("cbc", "ID"), {"schemeAgencyID": VAT_SCHEME_AGENCY, "schemeID": VAT_SCHEME_5153}).text = "VAT"

        # Item
        item = SubElement(il, _qn("cac", "Item"))
        SubElement(item, _qn("cbc", "Name")).text = L["name"]

        # Price + AllowanceCharge
        price = SubElement(il, _qn("cac", "Price"))
        SubElement(price, _qn("cbc", "PriceAmount"), {"currencyID": cur_id}).text = _fmt(L["unit_price"])
        pac = SubElement(price, _qn("cac", "AllowanceCharge"))
        SubElement(pac, _qn("cbc", "ChargeIndicator")).text = "false"
        SubElement(pac, _qn("cbc", "AllowanceChargeReason")).text = "DISCOUNT"
        SubElement(pac, _qn("cbc", "Amount"), {"currencyID": cur_id}).text = _fmt(L["line_disc"])

    xml = tostring(inv, encoding="utf-8", method="xml").decode("utf-8")

    try:
        s = _get_settings()
        if s.meta.has_field("last_xml"):
            s.db_set("last_xml", xml[:100000])
    except Exception:
        pass

    return xml
