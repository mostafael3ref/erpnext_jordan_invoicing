# erpnext_jofotara/api/transform.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from html import escape as _html_escape
from typing import Tuple

import frappe
from frappe.utils import get_datetime

FMT3 = Decimal("0.001")  # 3 decimals per JoFotara samples (e.g. 0.160)


def _fmt(x: Decimal | float | int) -> str:
    if x is None:
        x = Decimal("0")
    if not isinstance(x, Decimal):
        x = Decimal(str(x))
    return str(x.quantize(FMT3, rounding=ROUND_HALF_UP))


def _escape(s: str | None) -> str:
    return _html_escape(s or "")


def _get_settings():
    return frappe.get_single("JoFotara Settings")


def _company_tax_id(company: str) -> str:
    # حاول تجيب الضريبة من Company أو من Settings كـ fallback
    try:
        comp = frappe.get_doc("Company", company)
        for f in ("tax_id", "company_tax_id", "tax_no", "tax_number"):
            if comp.meta.has_field(f) and getattr(comp, f, None):
                return str(getattr(comp, f)).strip()
    except Exception:
        pass
    try:
        s = _get_settings()
        if getattr(s, "seller_tax_number", None):
            return str(s.seller_tax_number).strip()
    except Exception:
        pass
    return ""


def _party_name_from_invoice(doc) -> Tuple[str, str]:
    """returns (supplier_name, customer_name) as legal names"""
    # Supplier is the company
    supplier_name = doc.company or ""
    try:
        comp = frappe.get_doc("Company", doc.company)
        if comp.company_name:
            supplier_name = comp.company_name
    except Exception:
        pass

    # Customer legal name
    customer_name = doc.customer_name or doc.customer or ""
    return supplier_name, customer_name


def _calc_totals(doc):
    """
    ارجع لنا:
      net_total, vat_amount, vat_percent, total_inclusive, payable, discount_total
      rounded_total, rounding_adjustment
    """
    # Items amounts في ERPNext عادة currency already rounded
    net_total = Decimal(str(doc.total or 0))  # قبل الضريبة
    # ضريبة القيمة المضافة
    vat_amount = Decimal("0")
    vat_percent = Decimal("0")
    if getattr(doc, "taxes", None):
        # نجمع ضريبة VAT فقط (On Net Total)
        for t in doc.taxes:
            rate = Decimal(str(t.rate or 0))
            amt = Decimal(str(t.tax_amount or 0))
            if rate != 0:
                vat_percent = rate
            vat_amount += amt

    # إجمالي بدون خصومات إضافية
    discount_total = Decimal(str(getattr(doc, "discount_amount", 0) or 0))
    # Inclusive = net + VAT - discount_total
    total_inclusive = net_total + vat_amount - discount_total

    # لو في rounding داخل ERPNext
    rounding_adjustment = Decimal(str(getattr(doc, "rounding_adjustment", 0) or 0))
    # rounded_total إن وجد، وإلا سيبها مساوية لـ total_inclusive
    rounded_total = Decimal(
        str(getattr(doc, "rounded_total", 0) or 0)
    ) if abs(rounding_adjustment) > Decimal("0.0005") else total_inclusive

    # payable = rounded_total عادةً (JoFotara يتوقع النهائي القابل للدفع)
    payable = rounded_total

    return (
        net_total, vat_amount, vat_percent,
        total_inclusive, payable, discount_total,
        rounded_total, rounding_adjustment
    )


def build_invoice_xml(sinv_name: str) -> str:
    """
    يبني UBL 2.1 وفق متطلبات JoFotara.
    - لا نضيف أي سطور Special Tax ST صفرية.
    - نطابق الحسابات بحيث:
        TaxTotal = مجموع VAT فقط (لأن ST غير مستخدمة هنا)
        LegalMonetaryTotal:
            TaxExclusiveAmount = net_total
            TaxInclusiveAmount = total_inclusive
            AllowanceTotalAmount = discount_total (إن وُجد)
            PayableAmount = payable (عادة equals rounded_total)
    """
    doc = frappe.get_doc("Sales Invoice", sinv_name)

    # بيانات أساسية
    currency = doc.currency or "JOD"
    issue_date = get_datetime(doc.posting_date).date().isoformat()
    company_tax = _company_tax_id(doc.company)
    supplier_name, customer_name = _party_name_from_invoice(doc)

    # نوع المستند: 388 للفاتورة، 381 لإشعار دائن (لو Is Return)
    is_return = int(getattr(doc, "is_return", 0) or 0)
    ubl_type_code = "381" if is_return else "388"
    # الاسم الداخلي حسب جداولهم (021/011.. الخ) غير مؤثر حسابياً
    type_name = "011" if not is_return else "021"

    (
        net_total, vat_amount, vat_percent,
        total_inclusive, payable, discount_total,
        rounded_total, rounding_adjustment
    ) = _calc_totals(doc)

    # في JoFotara الأمثلة 3-decimals
    qty_total = Decimal("0")
    for it in doc.items or []:
        qty_total += Decimal(str(it.qty or 0))

    # رقم النشاط (Activity-Number) لعنصر SellerSupplierParty
    activity_number = (getattr(_get_settings(), "activity_number", None) or "").strip()

    xml_lines: list[str] = []
    add = xml_lines.append

    add('<?xml version="1.0" encoding="utf-8"?>')
    add('<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" '
        'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
        'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">')

    # ======= A. Header =======
    add(f'  <cbc:ID>{_escape(doc.name)}</cbc:ID>')
    add(f'  <cbc:UUID>{_escape(str(doc.name))}</cbc:UUID>')
    add(f'  <cbc:IssueDate>{issue_date}</cbc:IssueDate>')
    add(f'  <cbc:InvoiceTypeCode name="{_escape(type_name)}">{ubl_type_code}</cbc:InvoiceTypeCode>')
    add('  <cbc:Note>No Remarks</cbc:Note>')
    add(f'  <cbc:DocumentCurrencyCode>{_escape(currency)}</cbc:DocumentCurrencyCode>')
    add(f'  <cbc:TaxCurrencyCode>{_escape(currency)}</cbc:TaxCurrencyCode>')

    # ICV (counter) كبداية ثابتة 1
    add('  <cac:AdditionalDocumentReference>')
    add('    <cbc:ID>ICV</cbc:ID>')
    add('    <cbc:UUID>1</cbc:UUID>')
    add('  </cac:AdditionalDocumentReference>')

    # ======= B. Parties =======
    # Supplier (AccountingSupplierParty)
    add('  <cac:AccountingSupplierParty>')
    add('    <cac:Party>')
    if company_tax:
        add('      <cac:PartyTaxScheme>')
        add(f'        <cbc:CompanyID>{_escape(company_tax)}</cbc:CompanyID>')
        add('        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>')
        add('      </cac:PartyTaxScheme>')
    add('      <cac:PartyLegalEntity>')
    add(f'        <cbc:RegistrationName>{_escape(supplier_name)}</cbc:RegistrationName>')
    add('      </cac:PartyLegalEntity>')
    add('    </cac:Party>')
    add('  </cac:AccountingSupplierParty>')

    # Customer (AccountingCustomerParty)
    add('  <cac:AccountingCustomerParty>')
    add('    <cac:Party>')
    add('      <cac:PartyLegalEntity>')
    add(f'        <cbc:RegistrationName>{_escape(customer_name)}</cbc:RegistrationName>')
    add('      </cac:PartyLegalEntity>')
    add('    </cac:Party>')
    add('  </cac:AccountingCustomerParty>')

    # SellerSupplierParty (Activity number as ID)
    if activity_number:
        add('  <cac:SellerSupplierParty>')
        add('    <cac:Party>')
        add('      <cac:PartyIdentification>')
        add(f'        <cbc:ID>{_escape(activity_number)}</cbc:ID>')
        add('      </cac:PartyIdentification>')
        add('    </cac:Party>')
        add('  </cac:SellerSupplierParty>')

    # ======= C. Tax Total (VAT only; no ST if zero) =======
    add('  <cac:TaxTotal>')
    add(f'    <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(vat_amount)}</cbc:TaxAmount>')
    # VAT Subtotal (always if VAT exists OR zero lines? نضيفه لو فيه VAT)
    if vat_amount != Decimal("0"):
        add('    <cac:TaxSubtotal>')
        add(f'      <cbc:TaxableAmount currencyID="{_escape(currency)}">{_fmt(net_total)}</cbc:TaxableAmount>')
        add(f'      <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(vat_amount)}</cbc:TaxAmount>')
        add('      <cac:TaxCategory>')
        add('        <cbc:ID>S</cbc:ID>')
        add(f'        <cbc:Percent>{_fmt(vat_percent)}</cbc:Percent>')
        add('        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>')
        add('      </cac:TaxCategory>')
        add('    </cac:TaxSubtotal>')
    # ❌ لا نضيف أي Subtotal لـ ST إطلاقًا طالما قيمته صفر (وده وضعنا الحالي)
    add('  </cac:TaxTotal>')

    # ======= D. Legal Monetary Totals =======
    add('  <cac:LegalMonetaryTotal>')
    # بعض نظم UBL تذكر LineExtensionAmount؛ الدليل الأردني يركز على TaxExclusive/Inclusive/Allowance/Payable
    add(f'    <cbc:TaxExclusiveAmount currencyID="{_escape(currency)}">{_fmt(net_total)}</cbc:TaxExclusiveAmount>')
    add(f'    <cbc:TaxInclusiveAmount currencyID="{_escape(currency)}">{_fmt(total_inclusive)}</cbc:TaxInclusiveAmount>')
    if discount_total and discount_total != Decimal("0"):
        add(f'    <cbc:AllowanceTotalAmount currencyID="{_escape(currency)}">{_fmt(discount_total)}</cbc:AllowanceTotalAmount>')
    add(f'    <cbc:PayableAmount currencyID="{_escape(currency)}">{_fmt(payable)}</cbc:PayableAmount>')
    add('  </cac:LegalMonetaryTotal>')

    # ======= E. Lines =======
    # نضيف سطر لكل item
    line_idx = 1
    for it in (doc.items or []):
        qty = Decimal(str(it.qty or 0))
        rate = Decimal(str(it.rate or 0))
        line_net = qty * rate  # بدون ضريبة
        add('  <cac:InvoiceLine>')
        add(f'    <cbc:ID>{line_idx}</cbc:ID>')
        add(f'    <cbc:InvoicedQuantity unitCode="PCE">{_fmt(qty)}</cbc:InvoicedQuantity>')
        add(f'    <cbc:LineExtensionAmount currencyID="{_escape(currency)}">{_fmt(line_net)}</cbc:LineExtensionAmount>')

        # Line Tax (VAT)
        if vat_percent and vat_percent != Decimal("0"):
            line_vat = (line_net * vat_percent / Decimal("100"))
            add('    <cac:TaxTotal>')
            add(f'      <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(line_vat)}</cbc:TaxAmount>')
            add('      <cac:TaxSubtotal>')
            add(f'        <cbc:TaxableAmount currencyID="{_escape(currency)}">{_fmt(line_net)}</cbc:TaxableAmount>')
            add(f'        <cbc:TaxAmount currencyID="{_escape(currency)}">{_fmt(line_vat)}</cbc:TaxAmount>')
            add('        <cac:TaxCategory>')
            add('          <cbc:ID>S</cbc:ID>')
            add(f'          <cbc:Percent>{_fmt(vat_percent)}</cbc:Percent>')
            add('          <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>')
            add('        </cac:TaxCategory>')
            add('      </cac:TaxSubtotal>')
            add('    </cac:TaxTotal>')

        # Item + Price
        item_name = it.item_name or it.item_code or "Item"
        add('    <cac:Item>')
        add(f'      <cbc:Name>{_escape(item_name)}</cbc:Name>')
        add('    </cac:Item>')
        add('    <cac:Price>')
        add(f'      <cbc:PriceAmount currencyID="{_escape(currency)}">{_fmt(rate)}</cbc:PriceAmount>')
        add('    </cac:Price>')

        add('  </cac:InvoiceLine>')
        line_idx += 1

    add('</Invoice>')
    return "\n".join(xml_lines)
