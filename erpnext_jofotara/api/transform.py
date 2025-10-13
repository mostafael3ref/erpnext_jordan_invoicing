# erpnext_jofotara/api/transform.py
from __future__ import annotations

from decimal import Decimal
from typing import Tuple, List, Dict, Any
import uuid as _uuid

import frappe
from frappe.utils import getdate

__all__ = ["build_invoice_xml"]

# ========= Constants =========
INVOICE = "388"      # New invoice
CREDIT_NOTE = "381"  # Credit note

CURRENCY = "JOD"
TAX_CURRENCY = "JOD"

NSMAP = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
}

# ========= Helpers =========

def _fmt(n: Any, places: int = 3) -> str:
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
    """Get/derive UUID & ICV (counter)."""
    # UUID
    uuid_val = ""
    for fn in ("jofotara_uuid", "uuid", "einv_uuid"):
        if doc.meta.has_field(fn) and getattr(doc, fn, None):
            uuid_val = str(getattr(doc, fn))
            break
    if not uuid_val:
        uuid_val = str(_uuid.uuid4())

    # ICV (counter)
    icv = ""
    for fn in ("jofotara_icv", "icv", "invoice_counter"):
        if doc.meta.has_field(fn) and getattr(doc, fn, None):
            icv = str(getattr(doc, fn))
            break
    if not icv:
        import re
        nums = "".join(re.findall(r"\d+", doc.name or ""))
        icv = nums or "1"

    return uuid_val, icv

def _is_cash_invoice(doc) -> bool:
    """Cash if POS/payments exist or fully paid."""
    try:
        if getattr(doc, "is_pos", 0):
            return True
        if getattr(doc, "payments", None):
            for p in doc.payments:
                if (p.amount or 0) > 0:
                    return True
        paid = _dec(getattr(doc, "paid_amount", 0))
        outstanding = _dec(getattr(doc, "outstanding_amount", 0))
        if paid >= outstanding and outstanding <= 0:
            return True
    except Exception:
        pass
    return False

def _invoice_name_attr(doc) -> str:
    """
    Map settings.invoice_template -> name attribute on cbc:InvoiceTypeCode
    income: 011/021, sales: 012/022
    """
    s = _get_settings()
    tmpl = (getattr(s, "invoice_template", "sales") or "sales").lower()
    cash = _is_cash_invoice(doc)
    mapping = {
        "income": ("011", "021"),
        "sales":  ("012", "022"),
    }
    a, b = mapping.get(tmpl, mapping["sales"])
    return a if cash else b

def _seller_info(doc) -> Tuple[str, str, str]:
    """Return (registration_name, tax_number, activity_number)."""
    s = _get_settings()
    reg_name = (getattr(doc, "company", None) or "Seller").strip()
    tax_no = (getattr(s, "seller_tax_number", None) or getattr(doc, "tax_id", None) or "").strip()
    activity = (getattr(s, "activity_number", None) or "").strip()
    return reg_name, tax_no, activity

def _customer_info(doc) -> Dict[str, str]:
    """Collect buyer info with best-effort scheme."""
    name = (getattr(doc, "customer_name", None) or getattr(doc, "customer", None) or "عميل نقدي").strip()
    scheme = ""
    cid = ""
    for fn in ("customer_tax_id", "tax_id", "buyer_tax_no", "national_id", "vat_tin"):
        val = getattr(doc, fn, None)
        if val:
            cid = str(val).strip()
            break
    if cid:
        scheme = "TN"  # treat as tax number by default
    return {"name": name, "id": cid, "scheme": scheme}

def _tax_category_and_rate(doc) -> Tuple[str, Decimal]:
    """Return tax category (S/O/Z) and percent from taxes table."""
    rate = Decimal("0")
    if getattr(doc, "taxes", None):
        for tx in doc.taxes:
            r = _dec(getattr(tx, "rate", 0))
            if r > 0:
                rate = r
                break
    if rate > 0:
        return "S", rate
    return "O", Decimal("0")  # zero-rated default if no positive rate

def _lines(doc) -> List[Dict[str, Any]]:
    """Build line dicts with discount & tax per-line."""
    rows: List[Dict[str, Any]] = []
    category, rate = _tax_category_and_rate(doc)
    for i, d in enumerate(getattr(doc, "items", []) or [], start=1):
        qty = _dec(getattr(d, "qty", 0))
        unit_rate = _dec(getattr(d, "rate", 0))
        discount = _dec(getattr(d, "discount_amount", 0))
        base_amount = qty * unit_rate
        line_net = base_amount - discount
        if line_net < 0:
            line_net = Decimal("0")
        tax_amt = (line_net * rate / Decimal("100")) if rate > 0 else Decimal("0")
        rows.append({
            "idx": i,
            "name": getattr(d, "item_name", None) or getattr(d, "item_code", None) or f"Item {i}",
            "qty": qty,
            "uom": _uom_code(getattr(d, "uom", None)),
            "rate": unit_rate,
            "base_amount": base_amount,
            "discount": discount,
            "line_net": line_net,
            "tax_amount": tax_amt,
            "line_total": line_net + tax_amt,
            "tax_category": category,
            "tax_rate": rate,
        })
    return rows

def _totals_from_lines(lines: List[Dict[str, Any]]) -> Dict[str, Decimal]:
    net = sum((l["line_net"] for l in lines), Decimal("0"))
    tax = sum((l["tax_amount"] for l in lines), Decimal("0"))
    grand = net + tax
    return {"net": net, "tax": tax, "grand": grand}

def _credit_note_reference(doc) -> Tuple[str, str]:
    """Return (orig_id, orig_uuid) for credit notes."""
    if not getattr(doc, "is_return", 0):
        return "", ""
    orig_id, orig_uuid = "", ""
    base = None
    try:
        if getattr(doc, "return_against", None):
            base = frappe.get_doc("Sales Invoice", doc.return_against)
            orig_id = doc.return_against
        elif getattr(doc, "amended_from", None):
            base = frappe.get_doc("Sales Invoice", doc.amended_from)
            orig_id = doc.amended_from
        if base:
            for fn in ("jofotara_uuid", "uuid", "einv_uuid"):
                if base.meta.has_field(fn) and getattr(base, fn, None):
                    orig_uuid = str(getattr(base, fn)); break
    except Exception:
        pass
    return orig_id, orig_uuid

# ========= XML Builder =========

def build_invoice_xml(name: str) -> str:
    """Build UBL 2.1 XML from Sales Invoice (name)."""
    doc = frappe.get_doc("Sales Invoice", name)

    issue_date = str(getdate(getattr(doc, "posting_date", getdate())))
    note = (getattr(doc, "remarks", None) or getattr(doc, "note", None) or "").strip()

    inv_id = doc.name
    inv_type_code = CREDIT_NOTE if getattr(doc, "is_return", 0) else INVOICE
    name_attr = _invoice_name_attr(doc)

    uuid_val, icv = _uuid_icv(doc)
    seller_name, seller_tax, activity_no = _seller_info(doc)
    cust = _customer_info(doc)

    lines = _lines(doc)
    totals = _totals_from_lines(lines)
    tax_category, tax_rate = _tax_category_and_rate(doc)
    orig_id, orig_uuid = _credit_note_reference(doc)

    xml: List[str] = []
    A = xml.append

    A('<?xml version="1.0" encoding="UTF-8"?>')
    A(f'<Invoice xmlns="{NSMAP["inv"]}" xmlns:cac="{NSMAP["cac"]}" xmlns:cbc="{NSMAP["cbc"]}">')

    # A. Basic
    A(f'  <cbc:ID>{inv_id}</cbc:ID>')
    A(f'  <cbc:UUID>{uuid_val}</cbc:UUID>')
    A(f'  <cbc:IssueDate>{issue_date}</cbc:IssueDate>')
    A(f'  <cbc:InvoiceTypeCode name="{name_attr}">{inv_type_code}</cbc:InvoiceTypeCode>')
    if note:
        A(f'  <cbc:Note>{frappe.safe_encode(note).decode("utf-8")}</cbc:Note>')
    A(f'  <cbc:DocumentCurrencyCode>{CURRENCY}</cbc:DocumentCurrencyCode>')
    A(f'  <cbc:TaxCurrencyCode>{TAX_CURRENCY}</cbc:TaxCurrencyCode>')

    # ICV
    A('  <cac:AdditionalDocumentReference>')
    A('    <cbc:ID>ICV</cbc:ID>')
    A(f'    <cbc:UUID>{icv}</cbc:UUID>')
    A('  </cac:AdditionalDocumentReference>')

    # B. Seller (AccountingSupplierParty)  ✅ ضع ActivityNumber هنا
    A('  <cac:AccountingSupplierParty>')
    A('    <cac:Party>')
    if activity_no:
        A('      <cac:PartyIdentification>')
        A(f'        <cbc:ID schemeID="ActivityNumber">{activity_no}</cbc:ID>')
        A('      </cac:PartyIdentification>')
    if seller_tax:
        A('      <cac:PartyTaxScheme>')
        A(f'        <cbc:CompanyID>{seller_tax}</cbc:CompanyID>')
        A('      </cac:PartyTaxScheme>')
    A('      <cac:PartyLegalEntity>')
    A(f'        <cbc:RegistrationName>{seller_name}</cbc:RegistrationName>')
    A('      </cac:PartyLegalEntity>')
    A('    </cac:Party>')
    A('  </cac:AccountingSupplierParty>')

    # C. Customer
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

    # Credit note reference
    if inv_type_code == CREDIT_NOTE and (orig_id or orig_uuid):
        A('  <cac:BillingReference>')
        A('    <cac:InvoiceDocumentReference>')
        if orig_id:
            A(f'      <cbc:ID>{orig_id}</cbc:ID>')
        if orig_uuid:
            A(f'      <cbc:UUID>{orig_uuid}</cbc:UUID>')
        A('    </cac:InvoiceDocumentReference>')
        A('  </cac:BillingReference>')

    # E. Tax totals
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

    # F. Monetary totals
    A('  <cac:LegalMonetaryTotal>')
    A(f'    <cbc:LineExtensionAmount currencyID="{CURRENCY}">{_fmt(totals["net"], 3)}</cbc:LineExtensionAmount>')
    A(f'    <cbc:TaxExclusiveAmount currencyID="{CURRENCY}">{_fmt(totals["net"], 3)}</cbc:TaxExclusiveAmount>')
    A(f'    <cbc:TaxInclusiveAmount currencyID="{CURRENCY}">{_fmt(totals["grand"], 3)}</cbc:TaxInclusiveAmount>')
    A(f'    <cbc:PayableAmount currencyID="{CURRENCY}">{_fmt(totals["grand"], 3)}</cbc:PayableAmount>')
    A('  </cac:LegalMonetaryTotal>')

    # G. Lines
    for l in lines:
        A('  <cac:InvoiceLine>')
        A(f'    <cbc:ID>{l["idx"]}</cbc:ID>')
        A(f'    <cbc:InvoicedQuantity unitCode="{l["uom"]}">{_fmt(l["qty"], 3)}</cbc:InvoicedQuantity>')
        A(f'    <cbc:LineExtensionAmount currencyID="{CURRENCY}">{_fmt(l["line_net"], 3)}</cbc:LineExtensionAmount>')

        # AllowanceCharge (discount at line level)
        if l["discount"] > 0:
            A('    <cac:AllowanceCharge>')
            A('      <cbc:ChargeIndicator>false</cbc:ChargeIndicator>')
            A(f'      <cbc:Amount currencyID="{CURRENCY}">{_fmt(l["discount"], 3)}</cbc:Amount>')
            A(f'      <cbc:BaseAmount currencyID="{CURRENCY}">{_fmt(l["base_amount"], 3)}</cbc:BaseAmount>')
            A('    </cac:AllowanceCharge>')

        # Tax at line level
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

        # Item
        A('    <cac:Item>')
        A(f'      <cbc:Name>{l["name"]}</cbc:Name>')
        A('    </cac:Item>')

        # Price
        A('    <cac:Price>')
        A(f'      <cbc:PriceAmount currencyID="{CURRENCY}">{_fmt(l["rate"], 3)}</cbc:PriceAmount>')
        A('    </cac:Price>')

        A('  </cac:InvoiceLine>')

    A('</Invoice>')
    return "\n".join(xml)
