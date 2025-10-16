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

Q3 = Decimal("0.001")

def _get_settings(): return frappe.get_single("JoFotara Settings")
def _q3(x) -> Decimal: return Decimal(str(x or 0)).quantize(Q3, rounding=ROUND_HALF_UP)
def _fmt(x, places=3): return f"{_q3(x):.{places}f}"

def _escape(s: str | None) -> str:
    if not s: return ""
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
              .replace('"',"&quot;").replace("'","&apos;"))

def _uom_code(u):
    m = {"unit":"PCE","units":"PCE","each":"PCE","pcs":"PCE","piece":"PCE","nos":"PCE","قطعة":"PCE","وحدة":"PCE",
         "kg":"KGM","كيلو":"KGM","kilogram":"KGM","g":"GRM","جرام":"GRM","m":"MTR","meter":"MTR","متر":"MTR",
         "cm":"CMT","سم":"CMT","mm":"MMT","sq m":"MTK","m2":"MTK","م٢":"MTK","متر مربع":"MTK",
         "l":"LTR","liter":"LTR","لتر":"LTR","hour":"HUR","ساعة":"HUR","day":"DAY","يوم":"DAY"}
    return m.get((u or "").strip().lower(), "PCE")

def _get_company_info(company: str) -> Tuple[str,str]:
    try:
        comp = frappe.get_doc("Company", company)
        name = (getattr(comp,"company_name","") or comp.name or "").strip()
        tax  = (getattr(comp,"tax_id","") or getattr(comp,"company_tax_id","") or "").strip()
        if not tax:
            s=_get_settings(); tax=(getattr(s,"seller_tax_number","") or "").strip()
        return name, tax
    except Exception:
        s=_get_settings()
        return (company or "Company"), (getattr(s,"seller_tax_number","") or "")

def _get_customer_info(doc) -> Tuple[str,str]:
    tax=(getattr(doc,"tax_id","") or getattr(doc,"customer_tax_id","") or "").strip()
    cname=(getattr(doc,"customer_name","") or getattr(doc,"customer","") or "").strip()
    if not tax or not cname:
        try:
            cust=frappe.get_doc("Customer", doc.customer)
            if not cname: cname=(getattr(cust,"customer_name","") or cust.name or "").strip()
            if not tax:  tax =(getattr(cust,"tax_id","") or getattr(cust,"national_id","") or "").strip()
        except Exception: pass
    return (cname or "Consumer"), (tax or "")

def _payment_method_code(doc) -> str:
    try:
        if float(getattr(doc,"outstanding_amount",0) or 0) <= 0.0001 or int(getattr(doc,"is_pos",0) or 0):
            return "011"  # Cash
    except Exception: pass
    return "021"        # Credit

def _is_credit(doc) -> bool:
    if int(getattr(doc,"is_return",0) or 0) == 1: return True
    if getattr(doc,"return_against",None): return True
    try:
        if float(doc.base_grand_total or 0) < 0: return True
    except Exception: pass
    return False

def _parse_item_tax_rates(item) -> Dict[str,float]:
    out={}
    try:
        txt=getattr(item,"item_tax_rate","") or ""
        if txt:
            d=json.loads(txt)
            for k,v in d.items(): out[(k or "").strip().lower()]=float(v or 0)
    except Exception: pass
    return out

def _invoice_uuid(doc) -> str:
    existing=(getattr(doc,"jofotara_uuid","") or "").strip()
    return existing or str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc.doctype}:{doc.name}"))

def build_invoice_xml(name: str) -> str:
    doc=frappe.get_doc("Sales Invoice", name)
    s=_get_settings()

    currency=(doc.currency or "JOD").strip() or "JOD"
    activity=re.sub(r"\D","",(getattr(s,"activity_number","") or ""))
    if not activity: frappe.throw("JoFotara Settings: Activity Number مطلوب (أرقام فقط).")

    supplier_name, supplier_tax=_get_company_info(doc.company)
    customer_name, customer_tax=_get_customer_info(doc)

    inv_type=CREDIT_NOTE if _is_credit(doc) else INVOICE
    issue_date=str(getattr(doc,"posting_date","") or datetime.today().date().isoformat())
    doc_id=doc.name
    uuid_val=_invoice_uuid(doc)

    # ------- Items & taxes ----------
    items=list(doc.items or [])
    sum_net=Decimal("0.0")
    for it in items: sum_net += Decimal(str(getattr(it,"net_amount",0) or getattr(it,"amount",0) or 0))

    invoice_discount=Decimal(str(getattr(doc,"discount_amount",0) or 0))
    disc_left=invoice_discount

    doc_total_tax = Decimal(str(getattr(doc,"total_taxes_and_charges",0) or 0))
    default_ratio = (doc_total_tax/sum_net) if (sum_net>0 and doc_total_tax>0) else Decimal("0.0")

    prepped=[]
    total_excl=Decimal("0.0")
    total_vat=Decimal("0.0")
    total_special=Decimal("0.0")
    taxable_vat=Decimal("0.0")
    taxable_special=Decimal("0.0")

    for idx,it in enumerate(items, start=1):
        qty=_q3(getattr(it,"qty",0))
        base_net_amount=Decimal(str(getattr(it,"net_amount",0) or getattr(it,"amount",0) or 0))
        line_disc_field=Decimal(str(getattr(it,"discount_amount",0) or 0))

        # وزّع خصم الفاتورة
        pro=Decimal("0.0")
        if invoice_discount>0 and sum_net>0:
            if idx < len(items):
                pro=(base_net_amount/sum_net*invoice_discount).quantize(Q3, rounding=ROUND_HALF_UP)
                disc_left -= pro
            else:
                pro=(disc_left if disc_left>0 else Decimal("0.0"))

        line_excl=_q3(base_net_amount - line_disc_field - pro)
        if line_excl < 0: line_excl=Decimal("0.0")

        rates=_parse_item_tax_rates(it)
        vat_rate=spl_rate=Decimal("0.0")
        for k,v in rates.items():
            lk=k.lower()
            if "special" in lk or "خاص" in lk: spl_rate=Decimal(str(v or 0))
            elif "vat" in lk or "value" in lk or "ضريبة" in lk: vat_rate=Decimal(str(v or 0))
        if vat_rate==0 and spl_rate==0 and default_ratio>0:
            vat_rate=(default_ratio*100)

        line_vat=_q3(line_excl*(vat_rate/Decimal("100")))
        line_special=_q3(line_excl*(spl_rate/Decimal("100")))

        total_excl += line_excl
        total_vat  += line_vat
        total_special += line_special
        if vat_rate>0: taxable_vat += line_excl
        if spl_rate>0: taxable_special += line_excl

        price_after_disc=_q3((line_excl/qty) if qty>0 else 0)
        prepped.append({
            "idx":idx,"qty":qty,"unit":_uom_code(getattr(it,"uom",None)),
            "name":(getattr(it,"item_name","") or getattr(it,"item_code","") or getattr(it,"description","") or "Item"),
            "line_excl":line_excl,"line_vat":line_vat,"line_special":line_special,
            "vat_percent":vat_rate,"special_percent":spl_rate,
            "price_after_disc":price_after_disc,
            "line_disc_total":_q3(line_disc_field + pro),
        })

    tax_exclusive=_q3(total_excl)
    tax_total=_q3(total_vat + total_special)
    tax_inclusive=_q3(tax_exclusive + tax_total)

    A=[]
    def add(x): A.append(x)

    add(f'<Invoice xmlns="{NS_INVOICE}" xmlns:cac="{NS_CAC}" xmlns:cbc="{NS_CBC}">')
    add(f'  <cbc:ID>{_escape(doc_id)}</cbc:ID>')
    add(f'  <cbc:UUID>{_escape(uuid_val)}</cbc:UUID>')
    add(f'  <cbc:IssueDate>{_escape(issue_date)}</cbc:IssueDate>')
    pay_code=_payment_method_code(doc)
    add(f'  <cbc:InvoiceTypeCode name="{_escape(pay_code)}">{inv_type}</cbc:InvoiceTypeCode>')
    note=(getattr(doc,"remarks","") or getattr(doc,"note","") or "").strip()
    if note: add(f'  <cbc:Note>{_escape(note)}</cbc:Note>')
    add(f'  <cbc:DocumentCurrencyCode>{_escape(currency)}</cbc:DocumentCurrencyCode>')
    add(f'  <cbc:TaxCurrencyCode>{_escape(currency)}</cbc:TaxCurrencyCode>')
    add('  <cac:AdditionalDocumentReference>')
    add('    <cbc:ID>ICV</cbc:ID>')
    add(f'    <cbc:UUID>{_escape(str(getattr(doc,"docstatus",1)))}</cbc:UUID>')
    add('  </cac:AdditionalDocumentReference>')

    # Supplier
    add('  <cac:AccountingSupplierParty><cac:Party>')
    if supplier_tax:
        add('    <cac:PartyTaxScheme><cbc:CompanyID>'+_escape(supplier_tax)+'</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>')
    add('    <cac:PartyLegalEntity><cbc:RegistrationName>'+_escape(supplier_name)+'</cbc:RegistrationName></cac:PartyLegalEntity>')
    add('  </cac:Party></cac:AccountingSupplierParty>')

    # Customer
    add('  <cac:AccountingCustomerParty><cac:Party>')
    if customer_tax:
        add('    <cac:PartyIdentification><cbc:ID schemeID="TN">'+_escape(customer_tax)+'</cbc:ID></cac:PartyIdentification>')
    add('    <cac:PartyLegalEntity><cbc:RegistrationName>'+_escape(customer_name)+'</cbc:RegistrationName></cac:PartyLegalEntity>')
    add('  </cac:Party></cac:AccountingCustomerParty>')

    # Activity Number
    add('  <cac:SellerSupplierParty><cac:Party><cac:PartyIdentification><cbc:ID>'+_escape(activity)+'</cbc:ID></cac:PartyIdentification></cac:Party></cac:SellerSupplierParty>')

    # --- TaxTotal (VAT + Special) ---
    add('  <cac:TaxTotal>')
    add(f'    <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(tax_total)}</cbc:TaxAmount>')

    # VAT subtotal
    add('    <cac:TaxSubtotal>')
    add(f'      <cbc:TaxableAmount currencyID="{_escape(currency)}">{_fmt(taxable_vat)}</cbc:TaxableAmount>')
    add(f'      <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(total_vat)}</cbc:TaxAmount>')
    add('      <cac:TaxCategory><cbc:ID>S</cbc:ID>')
    vat_percent=((total_vat/taxable_vat*100) if taxable_vat>0 else Decimal("0"))
    add(f'        <cbc:Percent>{_fmt(vat_percent)}</cbc:Percent>')
    add('        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory>')
    add('    </cac:TaxSubtotal>')

    # Special subtotal — حاضر حتى لو صفر
    sp_taxable = taxable_special if total_special>0 else Decimal("0")
    sp_amount  = total_special     if total_special>0 else Decimal("0")
    sp_percent = ((total_special/taxable_special*100) if taxable_special>0 else Decimal("0"))
    add('    <cac:TaxSubtotal>')
    add(f'      <cbc:TaxableAmount currencyID="{_escape(currency)}">{_fmt(sp_taxable)}</cbc:TaxableAmount>')
    add(f'      <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(sp_amount)}</cbc:TaxAmount>')
    add('      <cac:TaxCategory><cbc:ID>S</cbc:ID>')
    add(f'        <cbc:Percent>{_fmt(sp_percent)}</cbc:Percent>')
    add('        <cac:TaxScheme><cbc:ID>ST</cbc:ID></cac:TaxScheme></cac:TaxCategory>')
    add('    </cac:TaxSubtotal>')

    add('  </cac:TaxTotal>')

    # --- LegalMonetaryTotal ---
    add('  <cac:LegalMonetaryTotal>')
    add(f'    <cbc:TaxExclusiveAmount currencyID="{_escape(currency)}">{_fmt(tax_exclusive)}</cbc:TaxExclusiveAmount>')
    add(f'    <cbc:TaxInclusiveAmount currencyID="{_escape(currency)}">{_fmt(tax_inclusive)}</cbc:TaxInclusiveAmount>')
    allowance=_q3(invoice_discount)  # لازم يظهر حتى لو صفر
    add(f'    <cbc:AllowanceTotalAmount currencyID="{_escape(currency)}">{_fmt(allowance)}</cbc:AllowanceTotalAmount>')
    payable=_q3(tax_inclusive - allowance)
    add(f'    <cbc:PayableAmount currencyID="{_escape(currency)}">{_fmt(payable)}</cbc:PayableAmount>')
    add('  </cac:LegalMonetaryTotal>')

    # Lines
    for L in prepped:
        add('  <cac:InvoiceLine>')
        add(f'    <cbc:ID>{L["idx"]}</cbc:ID>')
        add(f'    <cbc:InvoicedQuantity unitCode="{_escape(L["unit"])}">{_fmt(L["qty"])}</cbc:InvoicedQuantity>')
        add(f'    <cbc:LineExtensionAmount currencyID="{_escape(currency)}">{_fmt(L["line_excl"])}</cbc:LineExtensionAmount>')

        add('    <cac:TaxTotal>')
        line_tax=_q3(L["line_vat"] + L["line_special"])
        add(f'      <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(line_tax)}</cbc:TaxAmount>')
        if L["line_vat"] > 0:
            add('      <cac:TaxSubtotal><cbc:TaxableAmount currencyID="'+_escape(currency)+'">'+_fmt(L["line_excl"])+'</cbc:TaxableAmount>'
                '<cbc:TaxAmount currencyID="'+_escape(currency)+'">'+_fmt(L["line_vat"])+'</cbc:TaxAmount>'
                '<cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>'+_fmt(L["vat_percent"])+'</cbc:Percent>'
                '<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal>')
        if L["line_special"] > 0:
            add('      <cac:TaxSubtotal><cbc:TaxableAmount currencyID="'+_escape(currency)+'">'+_fmt(L["line_excl"])+'</cbc:TaxableAmount>'
                '<cbc:TaxAmount currencyID="'+_escape(currency)+'">'+_fmt(L["line_special"])+'</cbc:TaxAmount>'
                '<cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>'+_fmt(L["special_percent"])+'</cbc:Percent>'
                '<cac:TaxScheme><cbc:ID>ST</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal>')
        add('    </cac:TaxTotal>')

        if L["line_disc_total"] and L["line_disc_total"]>0:
            add('    <cac:AllowanceCharge><cbc:ChargeIndicator>false</cbc:ChargeIndicator>'
                '<cbc:AllowanceChargeReason>discount</cbc:AllowanceChargeReason>'
                f'<cbc:Amount currencyID="{_escape(currency)}">{_fmt(L["line_disc_total"])}</cbc:Amount></cac:AllowanceCharge>')

        add('    <cac:Item><cbc:Name>'+_escape(L["name"])+'</cbc:Name></cac:Item>')
        add('    <cac:Price><cbc:PriceAmount currencyID="'+_escape(currency)+'">'+_fmt(L["price_after_disc"])+'</cbc:PriceAmount></cac:Price>')
        add('  </cac:InvoiceLine>')

    add('</Invoice>')
    xml="\n".join(A)
    try:
        if s.meta.has_field("last_xml"): s.db_set("last_xml", xml[:100000])
    except Exception: pass
    return xml
