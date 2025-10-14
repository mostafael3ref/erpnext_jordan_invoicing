# erpnext_jofotara/api/transform.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Tuple

import json
import re
import uuid
from datetime import datetime

import frappe

__all__ = ["build_invoice_xml"]

INVOICE = "388"
CREDIT_NOTE = "381"

NS_INVOICE = 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'
NS_CAC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
NS_CBC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'

def _get_settings():
    return frappe.get_single("JoFotara Settings")

def _fmt(n: Decimal | float | int | None, places: int = 3) -> str:
    try:
        return f"{Decimal(str(n or 0)).quantize(Decimal('1.' + ('0'*places)), rounding=ROUND_HALF_UP)}"
    except Exception:
        return f"{Decimal('0').quantize(Decimal('1.' + ('0'*places)))}"

def _escape(v: str | None) -> str:
    if not v:
        return ""
    return (v.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
              .replace('"',"&quot;").replace("'","&apos;"))

def _uom_code(u: str | None) -> str:
    key = (u or "").strip().lower()
    mapping = {
        "unit":"PCE","units":"PCE","each":"PCE","pcs":"PCE","piece":"PCE","nos":"PCE","قطعة":"PCE","وحدة":"PCE",
        "box":"BOX","علبة":"BOX",
        "kg":"KGM","كيلو":"KGM","kilogram":"KGM",
        "g":"GRM","جرام":"GRM",
        "m":"MTR","meter":"MTR","متر":"MTR",
        "cm":"CMT","سم":"CMT","mm":"MMT",
        "sq m":"MTK","m2":"MTK","م٢":"MTK","متر مربع":"MTK",
        "l":"LTR","liter":"LTR","لتر":"LTR",
        "hour":"HUR","ساعة":"HUR","day":"DAY","يوم":"DAY",
    }
    return mapping.get(key, "PCE")

def _get_company_info(company: str) -> Tuple[str, str]:
    try:
        comp = frappe.get_doc("Company", company)
        name = (getattr(comp,"company_name","") or comp.name or "").strip()
        tax = (getattr(comp,"tax_id","") or getattr(comp,"company_tax_id","") or "").strip()
        if not tax:
            s = _get_settings()
            tax = (getattr(s,"seller_tax_number","") or "").strip()
        return name, tax
    except Exception:
        s = _get_settings()
        return (company or "Company"), (getattr(s,"seller_tax_number","") or "")

def _get_customer_info(doc) -> Tuple[str,str]:
    tax = (getattr(doc,"tax_id","") or getattr(doc,"customer_tax_id","") or "").strip()
    cname = (getattr(doc,"customer_name","") or getattr(doc,"customer","") or "").strip()
    if not tax or not cname:
        try:
            cust = frappe.get_doc("Customer", doc.customer)
            if not cname:
                cname = (getattr(cust,"customer_name","") or cust.name or "").strip()
            if not tax:
                tax = (getattr(cust,"tax_id","") or getattr(cust,"national_id","") or "").strip()
        except Exception:
            pass
    return (cname or "Consumer"), (tax or "")

def _payment_method_code(doc) -> str:
    try:
        outstanding = float(getattr(doc,"outstanding_amount",0) or 0)
        is_pos = int(getattr(doc,"is_pos",0) or 0)
        if outstanding <= 0.0001 or is_pos:
            return "011"
    except Exception:
        pass
    return "021"

def _is_credit_note(doc) -> bool:
    if int(getattr(doc,"is_return",0) or 0) == 1:
        return True
    if getattr(doc,"return_against",None):
        return True
    try:
        if float(doc.base_grand_total or 0) < 0:
            return True
    except Exception:
        pass
    return False

def _parse_item_tax_rate(item) -> float:
    """Extract % from item_tax_rate JSON if present."""
    try:
        txt = getattr(item,"item_tax_rate","") or ""
        if txt:
            d = json.loads(txt)
            for _, v in d.items():
                return float(v or 0)
    except Exception:
        pass
    return 0.0

def _invoice_uuid(doc) -> str:
    existing = (getattr(doc,"jofotara_uuid","") or "").strip()
    if existing:
        return existing
    seed = f"{doc.doctype}:{doc.name}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

def build_invoice_xml(name: str) -> str:
    doc = frappe.get_doc("Sales Invoice", name)
    s = _get_settings()

    currency = (doc.currency or "JOD").strip() or "JOD"
    activity_number = re.sub(r"\D","",(getattr(s,"activity_number","") or ""))
    if not activity_number:
        frappe.throw("JoFotara Settings: Activity Number مطلوب (أرقام فقط).")

    supplier_name, supplier_tax = _get_company_info(doc.company)
    customer_name, customer_tax = _get_customer_info(doc)

    is_cn = _is_credit_note(doc)
    inv_type_code = CREDIT_NOTE if is_cn else INVOICE
    pay_name_code = _payment_method_code(doc)

    issue_date = (str(getattr(doc,"posting_date","")) or datetime.today().date().isoformat())
    doc_id = doc.name
    uuid_val = _invoice_uuid(doc)

    # ===== تحضير البنود + توزيع الخصم العام Pro-Rata =====
    raw_lines = list(doc.items or [])
    sum_net = Decimal("0.0")
    for it in raw_lines:
        sum_net += Decimal(str(getattr(it,"net_amount",0) or getattr(it,"amount",0) or 0))

    invoice_discount = Decimal(str(getattr(doc,"discount_amount",0) or 0))
    distributed_left = invoice_discount

    prepped_lines: List[Dict] = []
    total_tax = Decimal("0.0")
    total_exclusive = Decimal("0.0")

    # لتحديد نسبة الضريبة الافتراضية من مستند ERPNext إن لم تتوفر على السطر
    doc_taxes_total = Decimal(str(getattr(doc,"total_taxes_and_charges",0) or 0))
    default_ratio = (doc_taxes_total / sum_net) if (sum_net > 0 and doc_taxes_total > 0) else Decimal("0.0")

    for idx, it in enumerate(raw_lines, start=1):
        qty = Decimal(str(getattr(it,"qty",0) or 0))
        base_net_rate = Decimal(str(getattr(it,"net_rate",0) or getattr(it,"rate",0) or 0))
        base_net_amount = Decimal(str(getattr(it,"net_amount",0) or getattr(it,"amount",0) or 0))

        line_disc_field = Decimal(str(getattr(it,"discount_amount",0) or 0))  # خصم موجود أصلاً على السطر
        # خصم موزع من خصم الفاتورة
        pro_rata = Decimal("0.0")
        if invoice_discount > 0 and sum_net > 0:
            if idx < len(raw_lines):
                pro_rata = (base_net_amount / sum_net * invoice_discount).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
                distributed_left -= pro_rata
            else:
                # آخر سطر ياخد المتبقي لإزالة فروقات التقريب
                pro_rata = (distributed_left if distributed_left > 0 else Decimal("0.0"))

        net_after_disc = base_net_amount - line_disc_field - pro_rata
        if net_after_disc < 0:
            net_after_disc = Decimal("0.0")

        # نسبة الضريبة للسطر
        explicit_rate = Decimal(str(_parse_item_tax_rate(it)))
        rate_ratio = (explicit_rate / Decimal("100")) if explicit_rate > 0 else default_ratio

        line_tax = (net_after_disc * rate_ratio).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

        total_exclusive += net_after_disc
        total_tax += line_tax

        uom = _uom_code(getattr(it,"uom",None))
        iname = (getattr(it,"item_name","") or getattr(it,"item_code","") or getattr(it,"description","") or "Item")

        # لو عندك معدل بعد الخصم تحتاجه للسعر
        net_rate_after_disc = (net_after_disc / qty).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP) if qty > 0 else base_net_rate

        prepped_lines.append({
            "idx": idx,
            "qty": qty,
            "unit": uom,
            "name": iname,
            "line_exclusive": net_after_disc,
            "line_tax": line_tax,
            "price_after_disc": net_rate_after_disc,
            "line_disc_total": (line_disc_field + pro_rata),
        })

    # إجماليات نهائية متسقة مع البنود
    tax_exclusive = total_exclusive
    tax_inclusive = (total_exclusive + total_tax)
    payable = tax_inclusive  # لا توجد دفعات مسبقة/حجز/استقطاع في هذا الإصدار

    # ===== كتابة XML حسب ترتيب الـ XSD =====
    A: List[str] = []
    def add(x: str): A.append(x)

    add(f'<Invoice xmlns="{NS_INVOICE}" xmlns:cac="{NS_CAC}" xmlns:cbc="{NS_CBC}">')

    add(f'  <cbc:ID>{_escape(doc_id)}</cbc:ID>')
    add(f'  <cbc:UUID>{_escape(uuid_val)}</cbc:UUID>')
    add(f'  <cbc:IssueDate>{_escape(issue_date)}</cbc:IssueDate>')
    add(f'  <cbc:InvoiceTypeCode name="{_escape(pay_name_code)}">{inv_type_code}</cbc:InvoiceTypeCode>')
    note_txt = (getattr(doc,"remarks","") or getattr(doc,"note","") or "").strip()
    if note_txt:
        add(f'  <cbc:Note>{_escape(note_txt)}</cbc:Note>')
    add(f'  <cbc:DocumentCurrencyCode>{_escape(currency)}</cbc:DocumentCurrencyCode>')
    add(f'  <cbc:TaxCurrencyCode>{_escape(currency)}</cbc:TaxCurrencyCode>')

    # ICV (اختياري)
    try:
        icv = str(getattr(doc,"amended_from","") or getattr(doc,"docstatus",1))
    except Exception:
        icv = "1"
    add('  <cac:AdditionalDocumentReference>')
    add('    <cbc:ID>ICV</cbc:ID>')
    add(f'    <cbc:UUID>{_escape(icv)}</cbc:UUID>')
    add('  </cac:AdditionalDocumentReference>')

    # Supplier
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

    # Customer (قبل SellerSupplierParty)
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

    # Activity Number
    add('  <cac:SellerSupplierParty>')
    add('    <cac:Party>')
    add('      <cac:PartyIdentification>')
    add(f'        <cbc:ID>{_escape(activity_number)}</cbc:ID>')
    add('      </cac:PartyIdentification>')
    add('    </cac:Party>')
    add('  </cac:SellerSupplierParty>')

    # مفيش AllowanceCharge على مستوى الفاتورة — تم التوزيع على البنود

    # TaxTotal (overall) مع TaxSubtotal VAT
    add('  <cac:TaxTotal>')
    add(f'    <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(total_tax,3)}</cbc:TaxAmount>')
    add('    <cac:TaxSubtotal>')
    add(f'      <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(total_tax,3)}</cbc:TaxAmount>')
    add('      <cac:TaxCategory>')
    add('        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>')
    add('      </cac:TaxCategory>')
    add('    </cac:TaxSubtotal>')
    add('  </cac:TaxTotal>')

    # LegalMonetaryTotal
    add('  <cac:LegalMonetaryTotal>')
    add(f'    <cbc:TaxExclusiveAmount currencyID="{_escape(currency)}">{_fmt(tax_exclusive,3)}</cbc:TaxExclusiveAmount>')
    add(f'    <cbc:TaxInclusiveAmount currencyID="{_escape(currency)}">{_fmt(tax_inclusive,3)}</cbc:TaxInclusiveAmount>')
    add(f'    <cbc:PayableAmount currencyID="{_escape(currency)}">{_fmt(payable,3)}</cbc:PayableAmount>')
    add('  </cac:LegalMonetaryTotal>')

    # InvoiceLine(s) — مع TaxSubtotal VAT وخصم السطر (إن وُجد بعد التوزيع)
    for L in prepped_lines:
        add('  <cac:InvoiceLine>')
        add(f'    <cbc:ID>{L["idx"]}</cbc:ID>')
        add(f'    <cbc:InvoicedQuantity unitCode="{_escape(L["unit"])}">{_fmt(L["qty"],3)}</cbc:InvoicedQuantity>')
        add(f'    <cbc:LineExtensionAmount currencyID="{_escape(currency)}">{_fmt(L["line_exclusive"],3)}</cbc:LineExtensionAmount>')
        add('    <cac:TaxTotal>')
        add(f'      <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(L["line_tax"],3)}</cbc:TaxAmount>')
        add('      <cac:TaxSubtotal>')
        add(f'        <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(L["line_tax"],3)}</cbc:TaxAmount>')
        add('        <cac:TaxCategory>')
        add('          <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>')
        add('        </cac:TaxCategory>')
        add('      </cac:TaxSubtotal>')
        add('    </cac:TaxTotal>')
        if L["line_disc_total"] and L["line_disc_total"] > 0:
            add('    <cac:AllowanceCharge>')
            add('      <cbc:ChargeIndicator>false</cbc:ChargeIndicator>')
            add('      <cbc:AllowanceChargeReason>discount</cbc:AllowanceChargeReason>')
            add(f'      <cbc:Amount currencyID="{_escape(currency)}">{_fmt(L["line_disc_total"],3)}</cbc:Amount>')
            add('    </cac:AllowanceCharge>')
        add('    <cac:Item>')
        add(f'      <cbc:Name>{_escape(L["name"])}</cbc:Name>')
        add('    </cac:Item>')
        add('    <cac:Price>')
        add(f'      <cbc:PriceAmount currencyID="{_escape(currency)}">{_fmt(L["price_after_disc"],3)}</cbc:PriceAmount>')
        add('    </cac:Price>')
        add('  </cac:InvoiceLine>')

    add('</Invoice>')

    xml = "\n".join(A)

    try:
        if s.meta.has_field("last_xml"):
            s.db_set("last_xml", xml[:100000])
    except Exception:
        pass

    return xml
