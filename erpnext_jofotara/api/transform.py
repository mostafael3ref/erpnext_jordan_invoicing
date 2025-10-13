# erpnext_jofotara/api/transform.py
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Tuple

import json
import re
import uuid
from datetime import datetime

import frappe

__all__ = ["build_invoice_xml"]

# =========================
# Constants
# =========================

INVOICE = "388"       # فاتورة مبيعات عادية
CREDIT_NOTE = "381"   # إشعار دائن (مرتجع/تصحيح)

NS_INVOICE = 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'
NS_CAC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
NS_CBC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'

# =========================
# Helpers
# =========================

def _get_settings():
    return frappe.get_single("JoFotara Settings")

def _fmt(n: Decimal | float | int | None, places: int = 3) -> str:
    try:
        return f"{float(n or 0):.{places}f}"
    except Exception:
        return f"{0.0:.{places}f}"

def _escape(txt: str | None) -> str:
    if not txt:
        return ""
    return (
        (txt or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )

def _uom_code(uom: str | None) -> str:
    key = (uom or "").strip().lower()
    mapping = {
        "unit": "PCE", "units": "PCE", "each": "PCE", "pcs": "PCE", "piece": "PCE", "nos": "PCE",
        "قطعة": "PCE", "وحدة": "PCE",
        "box": "BOX", "علبة": "BOX",
        "kg": "KGM", "كيلو": "KGM", "kilogram": "KGM",
        "g": "GRM", "جرام": "GRM",
        "m": "MTR", "meter": "MTR", "متر": "MTR",
        "cm": "CMT", "سم": "CMT",
        "mm": "MMT",
        "sq m": "MTK", "m2": "MTK", "م٢": "MTK", "متر مربع": "MTK",
        "l": "LTR", "liter": "LTR", "لتر": "LTR",
        "hour": "HUR", "ساعة": "HUR",
        "day": "DAY", "يوم": "DAY",
    }
    return mapping.get(key, "PCE")

def _get_company_info(company: str) -> Tuple[str, str]:
    try:
        comp = frappe.get_doc("Company", company)
        name = (getattr(comp, "company_name", "") or comp.name or "").strip()
        tax = (getattr(comp, "tax_id", "") or getattr(comp, "company_tax_id", "") or "").strip()
        if not tax:
            s = _get_settings()
            tax = (getattr(s, "seller_tax_number", "") or "").strip()
        return name, tax
    except Exception:
        s = _get_settings()
        return (company or "Company"), (getattr(s, "seller_tax_number", "") or "")

def _get_customer_info(doc) -> Tuple[str, str]:
    tax = (getattr(doc, "tax_id", "") or getattr(doc, "customer_tax_id", "") or "").strip()
    cname = (getattr(doc, "customer_name", "") or getattr(doc, "customer", "") or "").strip()
    if not tax or not cname:
        try:
            cust = frappe.get_doc("Customer", doc.customer)
            if not cname:
                cname = (getattr(cust, "customer_name", "") or cust.name or "").strip()
            if not tax:
                tax = (getattr(cust, "tax_id", "") or getattr(cust, "national_id", "") or "").strip()
        except Exception:
            pass
    return (cname or "Consumer"), (tax or "")

def _payment_method_code(doc) -> str:
    try:
        outstanding = float(getattr(doc, "outstanding_amount", 0) or 0)
        is_pos = int(getattr(doc, "is_pos", 0) or 0)
        if outstanding <= 0.0001 or is_pos:
            return "011"  # نقدي
    except Exception:
        pass
    return "021"  # آجل

def _is_credit_note(doc) -> bool:
    if int(getattr(doc, "is_return", 0) or 0) == 1:
        return True
    if getattr(doc, "return_against", None):
        return True
    try:
        if float(doc.base_grand_total or 0) < 0:
            return True
    except Exception:
        pass
    return False

def _money_fields(doc) -> Dict[str, Decimal]:
    cur = (doc.currency or "JOD").strip() or "JOD"
    net_total = Decimal(str(getattr(doc, "net_total", 0) or 0))
    taxes_total = Decimal(str(getattr(doc, "total_taxes_and_charges", 0) or 0))
    grand_total = Decimal(str(getattr(doc, "grand_total", 0) or 0))
    disc = Decimal(str(getattr(doc, "discount_amount", 0) or 0))
    return {
        "currency": cur,
        "tax_exclusive": net_total,
        "tax_inclusive": net_total + taxes_total,
        "tax_total": taxes_total,
        "discount_total": disc if disc > 0 else Decimal("0.0"),
        "payable": grand_total,
    }

def _line_tax_ratio(doc) -> float:
    try:
        net_total = float(getattr(doc, "net_total", 0) or 0)
        taxes_total = float(getattr(doc, "total_taxes_and_charges", 0) or 0)
        if net_total > 0 and taxes_total > 0:
            return taxes_total / net_total
    except Exception:
        pass
    return 0.0

def _parse_item_tax_rate(item) -> float:
    try:
        txt = getattr(item, "item_tax_rate", "") or ""
        if txt:
            d = json.loads(txt)
            for _, v in d.items():
                try:
                    return float(v or 0)
                except Exception:
                    continue
    except Exception:
        pass
    return 0.0

def _invoice_uuid(doc) -> str:
    existing = (getattr(doc, "jofotara_uuid", "") or "").strip()
    if existing:
        return existing
    seed = f"{doc.doctype}:{doc.name}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

# =========================
# XML Builder
# =========================

def build_invoice_xml(name: str) -> str:
    """
    يبني XML بصيغة UBL 2.1 متوافق مع JoFotara 1.4
    مع ترتيب العناصر كما يتطلب الـ XSD:
    ... Header + Parties + (Allowance/Tax/LegalMonetaryTotals) ثم InvoiceLine(s)
    """
    doc = frappe.get_doc("Sales Invoice", name)
    s = _get_settings()

    currency = (doc.currency or "JOD").strip() or "JOD"
    activity_number = re.sub(r"\D", "", (getattr(s, "activity_number", "") or ""))
    if not activity_number:
        frappe.throw("JoFotara Settings: Activity Number مطلوب (أرقام فقط).")

    supplier_name, supplier_tax = _get_company_info(doc.company)
    customer_name, customer_tax = _get_customer_info(doc)

    is_cn = _is_credit_note(doc)
    inv_type_code = CREDIT_NOTE if is_cn else INVOICE
    pay_name_code = _payment_method_code(doc)  # "011"/"021"

    money = _money_fields(doc)
    issue_date = (str(getattr(doc, "posting_date", "")) or datetime.today().date().isoformat())
    doc_id = doc.name
    uuid_val = _invoice_uuid(doc)

    # ===== تمريرة 1: حضّر بيانات البنود (بدون كتابة XML) =====
    tax_ratio = _line_tax_ratio(doc)
    prepped_lines: List[Dict] = []
    total_tax_accum = Decimal("0.0")
    line_index = 0
    for it in (doc.items or []):
        line_index += 1
        qty = Decimal(str(getattr(it, "qty", 0) or 0))
        net_rate = Decimal(str(getattr(it, "net_rate", 0) or getattr(it, "rate", 0) or 0))
        net_amount = Decimal(str(getattr(it, "net_amount", 0) or getattr(it, "amount", 0) or 0))
        item_disc = Decimal(str(getattr(it, "discount_amount", 0) or 0))
        explicit_tax_rate = _parse_item_tax_rate(it)  # %
        if explicit_tax_rate > 0:
            line_tax_amt = (net_amount - item_disc) * Decimal(str(explicit_tax_rate / 100.0))
        else:
            line_tax_amt = (net_amount - item_disc) * Decimal(str(tax_ratio))

        total_tax_accum += line_tax_amt
        unit = _uom_code(getattr(it, "uom", None))
        iname = (
            getattr(it, "item_name", "")
            or getattr(it, "item_code", "")
            or getattr(it, "description", "")
            or "Item"
        )

        prepped_lines.append({
            "idx": line_index,
            "qty": qty,
            "net_rate": net_rate,
            "net_amount_after_disc": net_amount - item_disc,
            "item_disc": item_disc,
            "tax_amt": line_tax_amt,
            "unit": unit,
            "iname": iname,
        })

    # المجموع الضريبي على مستوى الفاتورة (لو ERPNext ما حسبه)
    try:
        taxes_total_doc = Decimal(str(getattr(doc, "total_taxes_and_charges", 0) or 0))
    except Exception:
        taxes_total_doc = Decimal("0.0")
    taxes_final = taxes_total_doc if taxes_total_doc > 0 else total_tax_accum
    disc_total = money["discount_total"]

    # ===== تمريرة 2: كتابة XML بالترتيب الصحيح =====
    A: List[str] = []
    def add(line: str):
        A.append(line)

    add(f'<Invoice xmlns="{NS_INVOICE}" xmlns:cac="{NS_CAC}" xmlns:cbc="{NS_CBC}">')

    # --- Basic Header ---
    add(f'  <cbc:ID>{_escape(doc_id)}</cbc:ID>')
    add(f'  <cbc:UUID>{_escape(uuid_val)}</cbc:UUID>')
    add(f'  <cbc:IssueDate>{_escape(issue_date)}</cbc:IssueDate>')
    add(f'  <cbc:InvoiceTypeCode name="{_escape(pay_name_code)}">{inv_type_code}</cbc:InvoiceTypeCode>')
    note_txt = (getattr(doc, "remarks", "") or getattr(doc, "note", "") or "").strip()
    if note_txt:
        add(f'  <cbc:Note>{_escape(note_txt)}</cbc:Note>')
    add(f'  <cbc:DocumentCurrencyCode>{_escape(currency)}</cbc:DocumentCurrencyCode>')
    add(f'  <cbc:TaxCurrencyCode>{_escape(currency)}</cbc:TaxCurrencyCode>')

    # ICV (اختياري)
    try:
        icv = str(getattr(doc, "amended_from", "") or getattr(doc, "docstatus", 1))
    except Exception:
        icv = "1"
    add('  <cac:AdditionalDocumentReference>')
    add('    <cbc:ID>ICV</cbc:ID>')
    add(f'    <cbc:UUID>{_escape(icv)}</cbc:UUID>')
    add('  </cac:AdditionalDocumentReference>')

    # --- Supplier ---
    add('  <cac:AccountingSupplierParty>')
    add('    <cac:Party>')
    if supplier_tax:
        add('      <cac:PartyTaxScheme>')
        add(f'        <cbc:CompanyID>{_escape(supplier_tax)}</cbc:CompanyID>')
        add('        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>')
        add('      </cac:PartyTaxScheme>')
    add('      <cac:PartyLegalEntity>')
    add(f'        <cbc:RegistrationName>{_escape(supplier_name)}</cbc:RegistrationName>')
    add('      </cac:PartyLegalEntity>')
    add('    </cac:Party>')
    add('  </cac:AccountingSupplierParty>')

    # --- Customer (يجب قبل SellerSupplierParty) ---
    add('  <cac:AccountingCustomerParty>')
    add('    <cac:Party>')
    if customer_tax:
        add('      <cac:PartyIdentification>')
        add(f'        <cbc:ID schemeID="TN">{_escape(customer_tax)}</cbc:ID>')
        add('      </cac:PartyIdentification>')
    add('      <cac:PartyLegalEntity>')
    add(f'        <cbc:RegistrationName>{_escape(customer_name)}</cbc:RegistrationName>')
    add('      </cac:PartyLegalEntity>')
    add('    </cac:Party>')
    add('  </cac:AccountingCustomerParty>')

    # --- Activity Number ---
    add('  <cac:SellerSupplierParty>')
    add('    <cac:Party>')
    add('      <cac:PartyIdentification>')
    add(f'        <cbc:ID>{_escape(activity_number)}</cbc:ID>')
    add('      </cac:PartyIdentification>')
    add('    </cac:Party>')
    add('  </cac:SellerSupplierParty>')

    # --- (اختياري) PaymentMeans/Terms ممكن نضيفها لاحقًا لو اللائحة تشترطها ---

    # --- Allowance/Charge (خصم على مستوى الفاتورة) ---
    if disc_total and disc_total > 0:
        add('  <cac:AllowanceCharge>')
        add('    <cbc:ChargeIndicator>false</cbc:ChargeIndicator>')
        add('    <cbc:AllowanceChargeReason>discount</cbc:AllowanceChargeReason>')
        add(f'    <cbc:Amount currencyID="{_escape(currency)}">{_fmt(disc_total, 3)}</cbc:Amount>')
        add('  </cac:AllowanceCharge>')

    # --- TaxTotal (إجمالي) ---
    add('  <cac:TaxTotal>')
    add(f'    <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(taxes_final, 3)}</cbc:TaxAmount>')
    add('  </cac:TaxTotal>')

    # --- LegalMonetaryTotal ---
    add('  <cac:LegalMonetaryTotal>')
    add(f'    <cbc:TaxExclusiveAmount currencyID="{_escape(currency)}">{_fmt(money["tax_exclusive"], 3)}</cbc:TaxExclusiveAmount>')
    add(f'    <cbc:TaxInclusiveAmount currencyID="{_escape(currency)}">{_fmt(money["tax_inclusive"], 3)}</cbc:TaxInclusiveAmount>')
    if disc_total and disc_total > 0:
        add(f'    <cbc:AllowanceTotalAmount currencyID="{_escape(currency)}">{_fmt(disc_total, 3)}</cbc:AllowanceTotalAmount>')
    add(f'    <cbc:PayableAmount currencyID="{_escape(currency)}">{_fmt(money["payable"], 3)}</cbc:PayableAmount>')
    add('  </cac:LegalMonetaryTotal>')

    # --- InvoiceLine(s) (لازم تيجي بعد LegalMonetaryTotal حسب الـ XSD) ---
    for L in prepped_lines:
        add('  <cac:InvoiceLine>')
        add(f'    <cbc:ID>{L["idx"]}</cbc:ID>')
        add(f'    <cbc:InvoicedQuantity unitCode="{_escape(L["unit"])}">{_fmt(L["qty"], 3)}</cbc:InvoicedQuantity>')
        add(f'    <cbc:LineExtensionAmount currencyID="{_escape(currency)}">{_fmt(L["net_amount_after_disc"], 3)}</cbc:LineExtensionAmount>')
        add('    <cac:TaxTotal>')
        add(f'      <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(L["tax_amt"], 3)}</cbc:TaxAmount>')
        add('    </cac:TaxTotal>')
        if L["item_disc"] and L["item_disc"] > 0:
            add('    <cac:AllowanceCharge>')
            add('      <cbc:ChargeIndicator>false</cbc:ChargeIndicator>')
            add('      <cbc:AllowanceChargeReason>discount</cbc:AllowanceChargeReason>')
            add(f'      <cbc:Amount currencyID="{_escape(currency)}">{_fmt(L["item_disc"], 3)}</cbc:Amount>')
            add('    </cac:AllowanceCharge>')
        add('    <cac:Item>')
        add(f'      <cbc:Name>{_escape(L["iname"])}</cbc:Name>')
        add('    </cac:Item>')
        add('    <cac:Price>')
        add(f'      <cbc:PriceAmount currencyID="{_escape(currency)}">{_fmt(L["net_rate"], 3)}</cbc:PriceAmount>')
        add('    </cac:Price>')
        add('  </cac:InvoiceLine>')

    add('</Invoice>')

    xml = "\n".join(A)

    # (اختياري) احفظ معاينة في Settings لو عندك الحقل
    try:
        if s.meta.has_field("last_xml"):
            s.db_set("last_xml", xml[:100000])
    except Exception:
        pass

    return xml
