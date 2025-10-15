# erpnext_jofotara/api/transform.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Tuple
import json, re, uuid
from datetime import datetime
import frappe

__all__ = ["build_invoice_xml"]

INVOICE = "388"
CREDIT_NOTE = "381"

NS_INVOICE = 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'
NS_CAC     = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
NS_CBC     = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'

Q3 = Decimal("0.001")  # 3 decimals

# ---------------- Helpers ----------------
def _get_settings():
    return frappe.get_single("JoFotara Settings")

def _q3(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(Q3, rounding=ROUND_HALF_UP)

def _fmt(x, places: int = 3) -> str:
    return f"{_q3(x):.{places}f}"

def _escape(s: str | None) -> str:
    if not s:
        return ""
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
             .replace('"',"&quot;").replace("'","&apos;"))

def _uom_code(u: str | None) -> str:
    key = (u or "").strip().lower()
    m = {
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
    return m.get(key, "PCE")

def _get_company_info(company: str) -> Tuple[str, str]:
    try:
        comp = frappe.get_doc("Company", company)
        name = (getattr(comp,"company_name","") or comp.name or "").strip()
        tax  = (getattr(comp,"tax_id","") or getattr(comp,"company_tax_id","") or "").strip()
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
            if not cname: cname = (getattr(cust,"customer_name","") or cust.name or "").strip()
            if not tax:  tax  = (getattr(cust,"tax_id","") or getattr(cust,"national_id","") or "").strip()
        except Exception:
            pass
    return (cname or "Consumer"), (tax or "")

def _payment_method_code(doc) -> str:
    try:
        if float(getattr(doc,"outstanding_amount",0) or 0) <= 0.0001 or int(getattr(doc,"is_pos",0) or 0):
            return "011"  # Cash
    except Exception:
        pass
    return "021"        # Credit

def _is_credit(doc) -> bool:
    if int(getattr(doc,"is_return",0) or 0) == 1: return True
    if getattr(doc,"return_against",None): return True
    try:
        if float(doc.base_grand_total or 0) < 0: return True
    except Exception: pass
    return False

def _parse_item_tax_rates(item) -> Dict[str, float]:
    """ يرجّع {"vat ...": %, "special ...": %, ...} حسب أسماء الضرائب في item_tax_rate """
    out: Dict[str, float] = {}
    try:
        txt = getattr(item, "item_tax_rate", "") or ""
        if txt:
            d = json.loads(txt)
            for k, v in d.items():
                out[(k or "").strip().lower()] = float(v or 0)
    except Exception:
        pass
    return out

def _invoice_uuid(doc) -> str:
    existing = (getattr(doc,"jofotara_uuid","") or "").strip()
    if existing: return existing
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc.doctype}:{doc.name}"))

# -------------- Builder --------------
def build_invoice_xml(name: str) -> str:
    doc = frappe.get_doc("Sales Invoice", name)
    s = _get_settings()

    currency = (doc.currency or "JOD").strip() or "JOD"
    activity_number = re.sub(r"\D","",(getattr(s,"activity_number","") or ""))
    if not activity_number:
        frappe.throw("JoFotara Settings: Activity Number مطلوب (أرقام فقط).")

    supplier_name, supplier_tax = _get_company_info(doc.company)
    customer_name, customer_tax = _get_customer_info(doc)

    inv_type_code = CREDIT_NOTE if _is_credit(doc) else INVOICE
    pay_name_code = _payment_method_code(doc)

    issue_date = (str(getattr(doc,"posting_date","")) or datetime.today().date().isoformat())
    doc_id     = doc.name
    uuid_val   = _invoice_uuid(doc)

    # ===== تحضير البنود + توزيع خصم الفاتورة =====
    items = list(doc.items or [])
    sum_net_base = Decimal("0.0")
    for it in items:
        sum_net_base += Decimal(str(getattr(it,"net_amount",0) or getattr(it,"amount",0) or 0))

    inv_disc = Decimal(str(getattr(doc,"discount_amount",0) or 0))
    disc_left = inv_disc

    # نسبة عامة لو مفيش per-line
    doc_total_tax = Decimal(str(getattr(doc,"total_taxes_and_charges",0) or 0))
    default_ratio = (doc_total_tax / sum_net_base) if (sum_net_base > 0 and doc_total_tax > 0) else Decimal("0.0")

    prepped: List[Dict] = []
    total_vat = Decimal("0.0")
    total_special = Decimal("0.0")
    total_excl = Decimal("0.0")
    taxable_vat_sum = Decimal("0.0")
    taxable_special_sum = Decimal("0.0")

    for idx, it in enumerate(items, start=1):
        qty = _q3(getattr(it,"qty",0))
        base_net_rate   = Decimal(str(getattr(it,"net_rate",0) or getattr(it,"rate",0) or 0))
        base_net_amount = Decimal(str(getattr(it,"net_amount",0) or getattr(it,"amount",0) or 0))

        line_disc_field = Decimal(str(getattr(it,"discount_amount",0) or 0))
        pro_rata = Decimal("0.0")
        if inv_disc > 0 and sum_net_base > 0:
            if idx < len(items):
                pro_rata = (base_net_amount / sum_net_base * inv_disc).quantize(Q3, rounding=ROUND_HALF_UP)
                disc_left -= pro_rata
            else:
                pro_rata = (disc_left if disc_left > 0 else Decimal("0.0"))

        line_excl = base_net_amount - line_disc_field - pro_rata
        if line_excl < 0: line_excl = Decimal("0.0")
        line_excl = _q3(line_excl)

        rates = _parse_item_tax_rates(it)
        vat_rate = Decimal("0.0")
        spl_rate = Decimal("0.0")
        for k,v in rates.items():
            lk = k.lower()
            if "special" in lk or "خاص" in lk:
                spl_rate = Decimal(str(v or 0))
            elif "vat" in lk or "value" in lk or "ضريبة" in lk:
                vat_rate = Decimal(str(v or 0))
        if vat_rate == 0 and spl_rate == 0 and default_ratio > 0:
            vat_rate = (default_ratio * 100)

        line_vat     = _q3(line_excl * (vat_rate/Decimal("100")))
        line_special = _q3(line_excl * (spl_rate/Decimal("100")))

        total_excl    += line_excl
        total_vat     += line_vat
        total_special += line_special
        if vat_rate > 0: taxable_vat_sum += line_excl
        if spl_rate > 0: taxable_special_sum += line_excl

        uom = _uom_code(getattr(it,"uom",None))
        iname = (getattr(it,"item_name","") or getattr(it,"item_code","") or getattr(it,"description","") or "Item")
        price_after_disc = _q3((line_excl / qty) if qty > 0 else base_net_rate)

        prepped.append({
            "idx": idx,
            "qty": qty,
            "unit": uom,
            "name": iname,
            "line_excl": line_excl,
            "line_vat": line_vat,
            "line_special": line_special,
            "vat_percent": vat_rate,          # لِـ cbc:Percent
            "special_percent": spl_rate,      # لِـ cbc:Percent
            "price_after_disc": price_after_disc,
            "line_disc_total": _q3(line_disc_field + pro_rata),
        })

    tax_exclusive = _q3(total_excl)
    tax_total     = _q3(total_vat + total_special)
    tax_inclusive = _q3(tax_exclusive + tax_total)

    # ===== التقريب (Rounded Total) =====
    rounded_total = Decimal(str(getattr(doc, "rounded_total", 0) or 0))
    rounding_adj  = Decimal(str(getattr(doc, "rounding_adjustment", 0) or 0))
    use_rounding  = (rounded_total != 0) or (rounding_adj != 0)

    if use_rounding:
        payable_rounding = _q3(rounded_total - tax_inclusive)
        payable_amount   = _q3(rounded_total)
    else:
        payable_rounding = Decimal("0.000")
        payable_amount   = _q3(tax_inclusive)

    # ========== XML ==========
    A: List[str] = []
    def add(x: str): A.append(x)

    add(f'<Invoice xmlns="{NS_INVOICE}" xmlns:cac="{NS_CAC}" xmlns:cbc="{NS_CBC}">')
    add(f'  <cbc:ID>{_escape(doc_id)}</cbc:ID>')
    add(f'  <cbc:UUID>{_escape(uuid_val)}</cbc:UUID>')
    add(f'  <cbc:IssueDate>{_escape(issue_date)}</cbc:IssueDate>')
    add(f'  <cbc:InvoiceTypeCode name="{_escape(pay_name_code)}">{INVOICE if inv_type_code==INVOICE else CREDIT_NOTE}</cbc:InvoiceTypeCode>')
    note_txt = (getattr(doc,"remarks","") or getattr(doc,"note","") or "").strip()
    if note_txt: add(f'  <cbc:Note>{_escape(note_txt)}</cbc:Note>')
    add(f'  <cbc:DocumentCurrencyCode>{_escape(currency)}</cbc:DocumentCurrencyCode>')
    add(f'  <cbc:TaxCurrencyCode>{_escape(currency)}</cbc:TaxCurrencyCode>')

    # ICV اختياري
    try: icv = str(getattr(doc,"amended_from","") or getattr(doc,"docstatus",1))
    except Exception: icv = "1"
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

    # Customer
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

    # --- TaxTotal (overall): أرسل فقط الضرائب الفعلية (Special يُرسل فقط لو > 0) ---
    add('  <cac:TaxTotal>')
    add(f'    <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(tax_total)}</cbc:TaxAmount>')

    # VAT Subtotal
    add('    <cac:TaxSubtotal>')
    add(f'      <cbc:TaxableAmount currencyID="{_escape(currency)}">{_fmt(taxable_vat_sum)}</cbc:TaxableAmount>')
    add(f'      <cbc:TaxAmount    currencyID="{_escape(currency)}">{_fmt(total_vat)}</cbc:TaxAmount>')
    add('      <cac:TaxCategory>')
    add('        <cbc:ID>S</cbc:ID>')
    percent_vat = ((total_vat / taxable_vat_sum * 100) if taxable_vat_sum > 0 else Decimal("0"))
    add(f'        <cbc:Percent>{_fmt(percent_vat)}</cbc:Percent>')
    add('        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>')
    add('      </cac:TaxCategory>')
    add('    </cac:TaxSubtotal>')

    # Special Subtotal فقط لو فعليًا > 0
    if total_special > 0:
        add('    <cac:TaxSubtotal>')
        add(f'      <cbc:TaxableAmount currencyID="{_escape(currency)}">{_fmt(taxable_special_sum)}</cbc:TaxableAmount>')
        add(f'      <cbc:TaxAmount    currencyID="{_escape(currency)}">{_fmt(total_special)}</cbc:TaxAmount>')
        add('      <cac:TaxCategory>')
        add('        <cbc:ID>S</cbc:ID>')
        percent_sp = ((total_special / taxable_special_sum * 100) if taxable_special_sum > 0 else Decimal("0"))
        add(f'        <cbc:Percent>{_fmt(percent_sp)}</cbc:Percent>')
        add('        <cac:TaxScheme><cbc:ID>Special</cbc:ID></cac:TaxScheme>')
        add('      </cac:TaxCategory>')
        add('    </cac:TaxSubtotal>')

    add('  </cac:TaxTotal>')

    # LegalMonetaryTotal
    add('  <cac:LegalMonetaryTotal>')
    add(f'    <cbc:TaxExclusiveAmount currencyID="{_escape(currency)}">{_fmt(tax_exclusive)}</cbc:TaxExclusiveAmount>')
    add(f'    <cbc:TaxInclusiveAmount currencyID="{_escape(currency)}">{_fmt(tax_inclusive)}</cbc:TaxInclusiveAmount>')
    if use_rounding and payable_rounding != 0:
        add(f'    <cbc:PayableRoundingAmount currencyID="{_escape(currency)}">{_fmt(payable_rounding)}</cbc:PayableRoundingAmount>')
    add(f'    <cbc:PayableAmount currencyID="{_escape(currency)}">{_fmt(payable_amount)}</cbc:PayableAmount>')
    add('  </cac:LegalMonetaryTotal>')

    # InvoiceLine(s)
    for L in prepped:
        add('  <cac:InvoiceLine>')
        add(f'    <cbc:ID>{L["idx"]}</cbc:ID>')
        add(f'    <cbc:InvoicedQuantity unitCode="{_escape(L["unit"])}">{_fmt(L["qty"])}</cbc:InvoicedQuantity>')
        add(f'    <cbc:LineExtensionAmount currencyID="{_escape(currency)}">{_fmt(L["line_excl"])}</cbc:LineExtensionAmount>')
        add('    <cac:TaxTotal>')
        line_tax_total = _q3(L["line_vat"] + L["line_special"])
        add(f'      <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(line_tax_total)}</cbc:TaxAmount>')
        if L["line_vat"] > 0:
            add('      <cac:TaxSubtotal>')
            add(f'        <cbc:TaxableAmount currencyID="{_escape(currency)}">{_fmt(L["line_excl"])}</cbc:TaxableAmount>')
            add(f'        <cbc:TaxAmount    currencyID="{_escape(currency)}">{_fmt(L["line_vat"])}</cbc:TaxAmount>')
            add('        <cac:TaxCategory>')
            add('          <cbc:ID>S</cbc:ID>')
            add(f'          <cbc:Percent>{_fmt(L["vat_percent"])}</cbc:Percent>')
            add('          <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>')
            add('        </cac:TaxCategory>')
            add('      </cac:TaxSubtotal>')
        if L["line_special"] > 0:
            add('      <cac:TaxSubtotal>')
            add(f'        <cbc:TaxableAmount currencyID="{_escape(currency)}">{_fmt(L["line_excl"])}</cbc:TaxableAmount>')
            add(f'        <cbc:TaxAmount    currencyID="{_escape(currency)}">{_fmt(L["line_special"])}</cbc:TaxAmount>')
            add('        <cac:TaxCategory>')
            add('          <cbc:ID>S</cbc:ID>')
            add(f'          <cbc:Percent>{_fmt(L["special_percent"])}</cbc:Percent>')
            add('          <cac:TaxScheme><cbc:ID>Special</cbc:ID></cac:TaxScheme>')
            add('        </cac:TaxCategory>')
            add('      </cac:TaxSubtotal>')
        add('    </cac:TaxTotal>')
        if L["line_disc_total"] and L["line_disc_total"] > 0:
            add('    <cac:AllowanceCharge>')
            add('      <cbc:ChargeIndicator>false</cbc:ChargeIndicator>')
            add('      <cbc:AllowanceChargeReason>discount</cbc:AllowanceChargeReason>')
            add(f'      <cbc:Amount currencyID="{_escape(currency)}">{_fmt(L["line_disc_total"])}</cbc:Amount>')
            add('    </cac:AllowanceCharge>')
        add('    <cac:Item>')
        add(f'      <cbc:Name>{_escape(L["name"])}</cbc:Name>')
        add('    </cac:Item>')
        add('    <cac:Price>')
        add(f'      <cbc:PriceAmount currencyID="{_escape(currency)}">{_fmt(L["price_after_disc"])}</cbc:PriceAmount>')
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
