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
    # زى Odoo: كمية بعُشر واحد (1.0 / 25.0)
    return f"{float(x):.1f}"

def _get_settings():
    return frappe.get_single("JoFotara Settings")

def _company_info(company: str) -> Tuple[dict, str]:
    """
    يرجّع (company_doc_as_dict, tax_id_fallback)
    هنحتاج PostalZone لو متاحة من العنوان.
    """
    tax = ""
    cd = {}
    try:
        c = frappe.get_doc("Company", company)
        cd = c.as_dict()
        # probable tax fields
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
    """
    حاول تجيب PostalZone من Company Address إن وُجد.
    هنقرأ العنوان الافتراضي المرتبط بالشركة لو متاح.
    """
    try:
        addr_link = frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Company", "link_name": company_doc.get("name"), "parenttype": "Address"},
            fields=["parent"], limit=1
        )
        if addr_link:
            addr = frappe.get_doc("Address", addr_link[0]["parent"])
            # common fields: pincode / zip / postal_code / po_box
            for f in ("pincode", "zip", "postal_code", "po_box"):
                v = (getattr(addr, f, None) or "").strip()
                if v:
                    return v
    except Exception:
        pass
    return ""  # سيُرسل فاضي لو مش متاح

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
    يولد UBL 2.1 مطابق لستايل الـXML المقبول:
      - ProfileID=reporting:1.0
      - name="022" ثابت مع قيمة 388
      - Document/TaxCurrencyCode = JOD، لكن كل currencyID داخل المبالغ = JO
      - Header TaxTotal بدون TaxSubtotal
      - AllowanceCharge=0.000 في الهيدر وتحت السعر
      - TaxSubtotal على مستوى السطر فقط + attributes
      - RoundingAmount على السطر لو فاتورة سطر واحد (= Payable)
    """
    doc = frappe.get_doc("Sales Invoice", sales_invoice_name)

    # ===== basic =====
    issue_date = str(getdate(getattr(doc, "posting_date", None)) or getdate())
    inv_code = CREDIT_NOTE if int(getattr(doc, "is_return", 0) or 0) == 1 else INVOICE

    # **ثبّتنا name="022"** لمطابقة الـXML المقبول
    inv_name_attr = "022"

    company_doc, supplier_tax = _company_info(doc.company)
    supplier_name = (company_doc.get("company_name") or company_doc.get("name") or doc.company).strip()
    customer_name = _customer_name(doc)
    activity = _activity_number()  # Required

    currency_doc = (doc.currency or CURRENCY_CODE_DOC).upper() or CURRENCY_CODE_DOC
    cur_id = CURRENCY_ID_AMT

    # ===== totals from lines =====
    lines: List[Dict] = []
    net_sum = Decimal("0.0")
    vat_sum = Decimal("0.0")
    header_discount = _dec(getattr(doc, "discount_amount", 0) or 0)
    global_vat = _global_vat_rate(doc)

    for it in (doc.items or []):
        qty = _dec(getattr(it, "qty", 0) or 0)
        rate = _dec(getattr(it, "rate", 0) or 0)
        unit_code = _uom_code(getattr(it, "uom", None))
        line_disc = _dec(getattr(it, "discount_amount", 0) or 0)

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
    payable = inclusive_total  # no extra rounding

    # ===== XML =====
    inv = Element(_qn("inv", "Invoice"))

    # Header (match Odoo)
    SubElement(inv, _qn("cbc", "ProfileID")).text = "reporting:1.0"
    SubElement(inv, _qn("cbc", "ID")).text = str(doc.name)
    # UUID حقيقي بدل اسم المستند
    SubElement(inv, _qn("cbc", "UUID")).text = str(uuid.uuid4())
    SubElement(inv, _qn("cbc", "IssueDate")).text = issue_date
    SubElement(inv, _qn("cbc", "InvoiceTypeCode"), {"name": inv_name_attr}).text = inv_code
    SubElement(inv, _qn("cbc", "DocumentCurrencyCode")).text = currency_doc
    SubElement(inv, _qn("cbc", "TaxCurrencyCode")).text = currency_doc

    add_doc = SubElement(inv, _qn("cac", "AdditionalDocumentReference"))
    SubElement(add_doc, _qn("cbc", "ID")).text = "ICV"
    # لو عندك عدّاد داخلي للـICV استبدله هنا
    SubElement(add_doc, _qn("cbc", "UUID")).text = "1"

    # Supplier
    acc_sup = SubElement(inv, _qn("cac", "AccountingSupplierParty"))
    party = SubElement(acc_sup, _qn("cac", "Party"))

    addr = SubElement(party, _qn("cac", "PostalAddress"))
    # PostalZone لو متاح
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
    SubElement(pid, _qn("cbc", "ID"), {"schemeID": "TN"})  # فارغ زى المثال المقبول

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

    # Header TaxTotal (no Subtotal)
    head_tax = SubElement(inv, _qn("cac", "TaxTotal"))
    SubElement(head_tax, _qn("cbc", "TaxAmount"), {"currencyID": cur_id}).text = _fmt(vat_sum)

    # LegalMonetaryTotal
    lmt = SubElement(inv, _qn("cac", "LegalMonetaryTotal"))
    SubElement(lmt, _qn("cbc", "TaxExclusiveAmount"), {"currencyID": cur_id}).text = _fmt(net_after_header_disc)
    SubElement(lmt, _qn("cbc", "TaxInclusiveAmount"), {"currencyID": cur_id}).text = _fmt(inclusive_total)
    SubElement(lmt, _qn("cbc", "AllowanceTotalAmount"), {"currencyID": cur_id}).text = _fmt(header_discount)
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

        # Price + AC
        price = SubElement(il, _qn("cac", "Price"))
        SubElement(price, _qn("cbc", "PriceAmount"), {"currencyID": cur_id}).text = _fmt(L["unit_price"])
        pac = SubElement(price, _qn("cac", "AllowanceCharge"))
        SubElement(pac, _qn("cbc", "ChargeIndicator")).text = "false"
        SubElement(pac, _qn("cbc", "AllowanceChargeReason")).text = "DISCOUNT"
        SubElement(pac, _qn("cbc", "Amount"), {"currencyID": cur_id}).text = _fmt(L["line_disc"])

    xml = tostring(inv, encoding="utf-8", method="xml").decode("utf-8")

    # اختياري: snapshot آخر XML
    try:
        s = _get_settings()
        if s.meta.has_field("last_xml"):
            s.db_set("last_xml", xml[:100000])
    except Exception:
        pass

    return xml
