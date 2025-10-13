# erpnext_jofotara/api/transform.py
from __future__ import annotations

from decimal import Decimal
from typing import Tuple, List, Dict, Any
import uuid as _uuid

import frappe
from frappe.utils import getdate

__all__ = ["build_invoice_xml"]

# ---------------------------
# ثوابت حسب الدليل
# ---------------------------
INVOICE = "388"      # فاتورة جديدة
CREDIT_NOTE = "381"  # إشعار دائن

CURRENCY = "JOD"
TAX_CURRENCY = "JOD"

NSMAP = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
}

# ---------------------------
# Utilities
# ---------------------------

def _fmt(n: Any, places: int = 3) -> str:
    """تنسيق أرقام آمن."""
    try:
        return f"{float(n):.{places}f}"
    except Exception:
        return f"{0.0:.{places}f}"

def _dec(x: Any) -> Decimal:
    try:
        return Decimal(str(x or "0"))
    except Exception:
        return Decimal("0")

def _uom_code(uom: str | None) -> str:
    """تحويل وحدات شائعة إلى رمز قياسي (PCE = قطعة)."""
    if not uom:
        return "PCE"
    key = (uom or "").strip().lower()
    mapping = {
        "unit": "PCE", "units": "PCE", "each": "PCE", "pcs": "PCE", "piece": "PCE", "nos": "PCE",
        "وحدة": "PCE", "قطعة": "PCE",
        "kg": "KGM", "g": "GRM", "m": "MTR", "cm": "CMT", "mm": "MMT", "box": "BX",
        "m2": "MTK", "m²": "MTK", "sqm": "MTK",
        "l": "LTR", "lt": "LTR", "liter": "LTR",
    }
    return mapping.get(key, "PCE")

def _get_settings():
    return frappe.get_single("JoFotara Settings")

def _uuid_icv(doc) -> Tuple[str, str]:
    """
    يولّد/يستنتج:
      - UUID (فريد للفاتورة) — إن لم يوجد بالحقل.
      - ICV عدّاد تسلسلي (إن لم يوجد حقل عندك نستخدم رقم من الاسم).
    لو عندك حقول مخصصة jofotara_uuid / jofotara_icv سيأخذها أولاً.
    """
    # UUID
    uuid_val = ""
    for fn in ("jofotara_uuid", "uuid", "einv_uuid"):
        if doc.meta.has_field(fn) and getattr(doc, fn, None):
            uuid_val = str(getattr(doc, fn))
            break
    if not uuid_val:
        uuid_val = str(_uuid.uuid4())

    # ICV
    icv = ""
    for fn in ("jofotara_icv", "icv", "invoice_counter"):
        if doc.meta.has_field(fn) and getattr(doc, fn, None):
            icv = str(getattr(doc, fn))
            break
    if not icv:
        # استنتاج بسيط من الاسم (أرقام فقط)، وإلا 1
        import re
        nums = "".join(re.findall(r"\d+", doc.name or ""))
        icv = nums or "1"

    return uuid_val, icv

def _is_cash_invoice(doc) -> bool:
    """
    تحديد طريقة الدفع لتعبئة name في InvoiceTypeCode:
      011 نقدي / 021 آجل (عامّة)
    دالة بسيطة: لو mode_of_payment موجود أو paid_amount≥outstanding تُعتبر نقدي.
    """
    try:
        if getattr(doc, "is_pos", 0):
            return True
        if getattr(doc, "payments", None):
            for p in doc.payments:
                if (p.amount or 0) > 0:
                    return True
        paid = _dec(getattr(doc, "paid_amount", 0))
        outstanding = _dec(getattr(doc, "outstanding_amount", 0))
        if paid >= outstanding:
            return True
    except Exception:
        pass
    return False

def _tax_category_and_rate(doc) -> Tuple[str, Decimal]:
    """
    استنتاج نوع الضريبة ونسبتها من بنود الضرائب في الفاتورة.
    - category: S (خاضع) / Z (معفى) / O (صفرية)
    - rate: النسبة %
    """
    rate = Decimal("0")
    if getattr(doc, "taxes", None):
        for tx in doc.taxes:
            r = _dec(getattr(tx, "rate", 0))
            if r > 0:
                rate = r
                break

    if rate > 0:
        return "S", rate
    # لو rate==0 قد تكون صفرية أو معفاة — نضع افتراضي صفرية
    return "O", Decimal("0")

def _doc_type_and_name_attr(doc) -> Tuple[str, str]:
    """
    يحدد:
      - cbc:InvoiceTypeCode (388/381)
      - name attribute لقيمة الدفع (011 نقدي / 021 آجل)
    """
    itc = CREDIT_NOTE if getattr(doc, "is_return", 0) else INVOICE
    name_attr = "011" if _is_cash_invoice(doc) else "021"
    return itc, name_attr

def _seller_info(doc) -> Tuple[str, str, str]:
    """
    يعيد: (registration_name, tax_number, activity_number)
    يفضّل الحقول من JoFotara Settings.
    """
    s = _get_settings()

    reg_name = (getattr(s, "seller_name", None) or getattr(doc, "company", None) or "Seller").strip()
    tax_no = (getattr(s, "seller_tax_number", None) or getattr(doc, "tax_id", None) or "").strip()
    activity = (getattr(s, "activity_number", None) or "").strip()

    return reg_name, tax_no, activity

def _customer_info(doc) -> Dict[str, str]:
    """
    محاولة جمع بيانات المشتري.
    يعيد dict مثل: {"name": "...", "id": "...", "scheme": "TN"}
    """
    name = (getattr(doc, "customer_name", None) or getattr(doc, "customer", None) or "عميل نقدي").strip()
    # رقم تعريف المشتري (اختياري)
    scheme = ""
    cid = ""

    # جرّب قراءة من الحقول الشائعة
    for fn in ("customer_tax_id", "tax_id", "buyer_tax_no", "national_id", "vat_tin"):
        val = getattr(doc, fn, None)
        if val:
            cid = str(val).strip()
            break

    # إن وجد رقم ضريبي نفترض TN، وإلا فارغ
    if cid:
        scheme = "TN"

    return {"name": name, "id": cid, "scheme": scheme}

def _lines(doc) -> List[Dict[str, Any]]:
    """تفاصيل البنود: السعر، الكمية، الضريبة، الخصم…"""
    rows: List[Dict[str, Any]] = []
    category, rate = _tax_category_and_rate(doc)

    for i, d in enumerate(getattr(doc, "items", []) or [], start=1):
        qty = _dec(getattr(d, "qty", 0))
        rate_u = _dec(getattr(d, "rate", 0))
        disc = _dec(getattr(d, "discount_amount", 0))

        # قيمة السطر قبل الضريبة وبعد الخصم
        line_net = qty * rate_u - disc
        if line_net < 0:
            line_net = Decimal("0")

        tax_amt = (line_net * rate / Decimal("100")) if rate > 0 else Decimal("0")
        line_total = line_net + tax_amt

        rows.append({
            "idx": i,
            "name": getattr(d, "item_name", None) or getattr(d, "item_code", None) or f"Item {i}",
            "qty": qty,
            "uom": _uom_code(getattr(d, "uom", None)),
            "rate": rate_u,
            "discount": disc,
            "line_net": line_net,
            "tax_amount": tax_amt,
            "line_total": line_total,
            "tax_category": category,
            "tax_rate": rate,
        })
    return rows

def _totals_from_lines(lines: List[Dict[str, Any]]) -> Dict[str, Decimal]:
    net = sum((l["line_net"] for l in lines), Decimal("0"))
    tax = sum((l["tax_amount"] for l in lines), Decimal("0"))
    grand = net + tax
    return {"net": net, "tax": tax, "grand": grand}

def _credit_note_reference(doc) -> str:
    """
    لو إشعار دائن، رجّع مرجع الفاتورة الأصلية (ID/UUID إن متوافر).
    نستخدم الحقول الشائعة: return_against / amended_from + jofotara_uuid.
    """
    if not getattr(doc, "is_return", 0):
        return ""
    ref = ""
    if getattr(doc, "return_against", None):
        ref = str(doc.return_against)
    elif getattr(doc, "amended_from", None):
        ref = str(doc.amended_from)
    return ref

# ---------------------------
# XML Builder (يدويًا بسلاسل نصية)
# ---------------------------

def build_invoice_xml(name: str) -> str:
    """
    يبني XML بصيغة UBL 2.1 كما يطلب JoFotara.
    يعتمد على Sales Invoice بالاسم (name).
    """
    doc = frappe.get_doc("Sales Invoice", name)

    # رؤوس/تعريفات
    invoice_type_code, name_attr = _doc_type_and_name_attr(doc)
    issue_date = str(getdate(getattr(doc, "posting_date", getdate())))
    note = (getattr(doc, "remarks", None) or getattr(doc, "note", None) or "").strip()

    inv_id = doc.name
    uuid_val, icv = _uuid_icv(doc)

    # البائع والمشتري
    seller_name, seller_tax, activity_no = _seller_info(doc)
    cust = _customer_info(doc)

    # البنود والإجماليات
    lines = _lines(doc)
    totals = _totals_from_lines(lines)
    tax_category, tax_rate = _tax_category_and_rate(doc)

    # مرجع الإشعار الدائن لو موجود
    cn_ref = _credit_note_reference(doc)

    # بناء XML
    # ملاحظة: نستخدم namespaces كما بالدليل؛ JoFotara يتسامح مع ترتيب الوسوم طالما UBL صحيح.
    xml = []
    A = xml.append

    A(f'<?xml version="1.0" encoding="UTF-8"?>')
    A(f'<Invoice xmlns="{NSMAP["inv"]}" '
      f'xmlns:cac="{NSMAP["cac"]}" '
      f'xmlns:cbc="{NSMAP["cbc"]}">')

    # --- A. Basic information ---
    A(f'  <cbc:ID>{inv_id}</cbc:ID>')
    A(f'  <cbc:UUID>{uuid_val}</cbc:UUID>')
    A(f'  <cbc:IssueDate>{issue_date}</cbc:IssueDate>')
    A(f'  <cbc:InvoiceTypeCode name="{name_attr}">{invoice_type_code}</cbc:InvoiceTypeCode>')

    if note:
        A(f'  <cbc:Note>{frappe.safe_encode(note).decode("utf-8")}</cbc:Note>')

    A(f'  <cbc:DocumentCurrencyCode>{CURRENCY}</cbc:DocumentCurrencyCode>')
    A(f'  <cbc:TaxCurrencyCode>{TAX_CURRENCY}</cbc:TaxCurrencyCode>')

    # ICV
    A('  <cac:AdditionalDocumentReference>')
    A('    <cbc:ID>ICV</cbc:ID>')
    A(f'    <cbc:UUID>{icv}</cbc:UUID>')
    A('  </cac:AdditionalDocumentReference>')

    # --- B. Seller (Supplier) ---
    A('  <cac:AccountingSupplierParty>')
    A('    <cac:Party>')
    if seller_tax:
        A('      <cac:PartyTaxScheme>')
        A(f'        <cbc:CompanyID>{seller_tax}</cbc:CompanyID>')
        A('      </cac:PartyTaxScheme>')
    A('      <cac:PartyLegalEntity>')
    A(f'        <cbc:RegistrationName>{seller_name}</cbc:RegistrationName>')
    A('      </cac:PartyLegalEntity>')
    A('    </cac:Party>')
    A('  </cac:AccountingSupplierParty>')

    # --- C. Customer (Buyer) ---
    A('  <cac:AccountingCustomerParty>')
    A('    <cac:Party>')
    if cust.get("id") and cust.get("scheme"):
        A('      <cac:PartyIdentification>')
        A(f'        <cbc:ID schemeID="{cust["scheme"]}">{cust["id"]}</cbc:ID>')
        A('      </cac:PartyIdentification>')
    A('      <cac:PartyLegalEntity>')
    A(f'        <cbc:RegistrationName>{cust["name"]}</cbc:RegistrationName>')
    A('      </cac:PartyLegalEntity>')
    A('    </cac:Party>')
    A('  </cac:AccountingCustomerParty>')

    # --- D. Activity Number ---
    if activity_no:
        A('  <cac:SellerSupplierParty>')
        A('    <cac:Party>')
        A('      <cac:PartyIdentification>')
        A(f'        <cbc:ID>{activity_no}</cbc:ID>')
        A('      </cac:PartyIdentification>')
        A('    </cac:Party>')
        A('  </cac:SellerSupplierParty>')

    # --- Credit Note Reference (إن وجِد) ---
    if invoice_type_code == CREDIT_NOTE and cn_ref:
        A('  <cac:BillingReference>')
        A('    <cac:InvoiceDocumentReference>')
        A(f'      <cbc:ID>{cn_ref}</cbc:ID>')
        A('    </cac:InvoiceDocumentReference>')
        A('  </cac:BillingReference>')

    # --- E. Tax Total (مجمّع) ---
    if totals["tax"] > 0:
        A('  <cac:TaxTotal>')
        A(f'    <cbc:TaxAmount currencyID="{CURRENCY}">{_fmt(totals["tax"], 3)}</cbc:TaxAmount>')
        A('    <cac:TaxSubtotal>')
        A(f'      <cbc:TaxableAmount currencyID="{CURRENCY}">{_fmt(totals["net"], 3)}</cbc:TaxableAmount>')
        A(f'      <cbc:TaxAmount currencyID="{CURRENCY}">{_fmt(totals["tax"], 3)}</cbc:TaxAmount>')
        A('      <cac:TaxCategory>')
        A(f'        <cbc:ID>{tax_category}</cbc:ID>')
        A(f'        <cbc:Percent>{_fmt(tax_rate, 3)}</cbc:Percent>')
        A('      </cac:TaxCategory>')
        A('    </cac:TaxSubtotal>')
        A('  </cac:TaxTotal>')

    # --- F. Monetary Totals ---
    A('  <cac:LegalMonetaryTotal>')
    A(f'    <cbc:LineExtensionAmount currencyID="{CURRENCY}">{_fmt(totals["net"], 3)}</cbc:LineExtensionAmount>')
    A(f'    <cbc:TaxExclusiveAmount currencyID="{CURRENCY}">{_fmt(totals["net"], 3)}</cbc:TaxExclusiveAmount>')
    A(f'    <cbc:TaxInclusiveAmount currencyID="{CURRENCY}">{_fmt(totals["grand"], 3)}</cbc:TaxInclusiveAmount>')
    A(f'    <cbc:PayableAmount currencyID="{CURRENCY}">{_fmt(totals["grand"], 3)}</cbc:PayableAmount>')
    A('  </cac:LegalMonetaryTotal>')

    # --- G. Lines ---
    for l in lines:
        A('  <cac:InvoiceLine>')
        A(f'    <cbc:ID>{l["idx"]}</cbc:ID>')
        A(f'    <cbc:InvoicedQuantity unitCode="{l["uom"]}">{_fmt(l["qty"], 3)}</cbc:InvoicedQuantity>')
        A(f'    <cbc:LineExtensionAmount currencyID="{CURRENCY}">{_fmt(l["line_net"], 3)}</cbc:LineExtensionAmount>')

        # الضريبة على مستوى السطر (مطلوبة لإيضاح التصنيف)
        if l["tax_rate"] >= 0:
            A('    <cac:TaxTotal>')
            A(f'      <cbc:TaxAmount currencyID="{CURRENCY}">{_fmt(l["tax_amount"], 3)}</cbc:TaxAmount>')
            A('      <cac:TaxSubtotal>')
            A(f'        <cbc:TaxableAmount currencyID="{CURRENCY}">{_fmt(l["line_net"], 3)}</cbc:TaxableAmount>')
            A(f'        <cbc:TaxAmount currencyID="{CURRENCY}">{_fmt(l["tax_amount"], 3)}</cbc:TaxAmount>')
            A('        <cac:TaxCategory>')
            A(f'          <cbc:ID>{l["tax_category"]}</cbc:ID>')
            A(f'          <cbc:Percent>{_fmt(l["tax_rate"], 3)}</cbc:Percent>')
            A('        </cac:TaxCategory>')
            A('      </cac:TaxSubtotal>')
            A('    </cac:TaxTotal>')

        # تفاصيل السلعة
        A('    <cac:Item>')
        A(f'      <cbc:Name>{l["name"]}</cbc:Name>')
        A('    </cac:Item>')

        # السعر
        A('    <cac:Price>')
        A(f'      <cbc:PriceAmount currencyID="{CURRENCY}">{_fmt(l["rate"], 3)}</cbc:PriceAmount>')
        A('    </cac:Price>')

        A('  </cac:InvoiceLine>')

    A('</Invoice>')

    return "\n".join(xml)
