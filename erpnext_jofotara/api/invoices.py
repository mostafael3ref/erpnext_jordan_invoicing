# erpnext_jofotara/api/invoices.py
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import frappe
from frappe import _
from frappe.utils import nowdate, now_datetime, cint, flt

import requests

# =========================================
# إعدادات عامة
# =========================================

JOD = "JOD"  # عملة الأردن الافتراضية

# كود نوع الفاتورة/المستند في JoFotara
# 388 = Payment (كمثال شائع للدفعات/سند قبض)
INVOICE_TYPE_CODE_PAYMENT = "388"

# مفاتيح الاستجابة المعيارية في بوابة JoFotara (حسب السجلات التي أرسلتها)
RESP_KEY_RESULTS = "EINV_RESULTS"
RESP_KEY_UUID = "EINV_INV_UUID"
RESP_KEY_NUM = "EINV_NUM"
RESP_KEY_QR = "EINV_QR"


# =========================================
# Helpers
# =========================================

def _full_url(base: str, path: str) -> str:
    """Safely join base URL + path."""
    if (path or "").startswith("http"):
        return path
    return urljoin((base or "").rstrip("/") + "/", (path or "").lstrip("/"))


def _get_settings():
    """
    إحضار سند الإعدادات المخصص:
    DocType: "JoFotara Settings"
    الحقول المتوقعة (كمثال):
      - base_url (Data)
      - client_id (Data)
      - secret_key (Password)
      - activity_number (Data)   ← يظهر في سجلاتك "Activity-Number"
      - key (Data)               ← يظهر في سجلاتك "Key"
      - accept_language (Select/Data) (افتراضي "ar")
      - enable_debug (Check)
    """
    return frappe.get_single("JoFotara Settings")


def _mask_headers(h: dict) -> dict:
    masked = dict(h or {})
    for k in ("Secret-Key", "Authorization"):
        if k in masked and masked[k]:
            masked[k] = "********"
    return masked


def _decimal(val: Any, precision: int = 3) -> Decimal:
    try:
        return (Decimal(str(val or 0))).quantize(Decimal(10) ** -precision)
    except Exception:
        return Decimal(0)


def _build_headers(s) -> Dict[str, str]:
    """
    تهيئة الرؤوس كما ورد في سجلاتك:
      {
        "Accept": "application/json",
        "Accept-Language": "ar",
        "Activity-Number": "15935566",
        "ActivityNumber": "15935566",  (اختياري لو كانت البوابة تحتاجه)
        "Client-Id": "<client_id>",
        "Content-Type": "application/json",
        "Key": "<key>",
        "Secret-Key": "<secret_from_password_field>"
      }
    """
    client_id = (s.client_id or "").strip()
    secret_key = (s.get_password("secret_key", raise_exception=False) or "").strip()
    activity_number = (getattr(s, "activity_number", "") or "").strip()
    key = (getattr(s, "key", "") or "").strip()
    accept_language = (getattr(s, "accept_language", "") or "ar").strip() or "ar"

    headers = {
        "Accept": "application/json",
        "Accept-Language": accept_language,
        "Content-Type": "application/json",
    }

    if activity_number:
        headers["Activity-Number"] = activity_number
        # بعض البوابات تحتاج الصيغة بدون شرطة أيضًا
        headers["ActivityNumber"] = activity_number

    if client_id:
        headers["Client-Id"] = client_id

    if key:
        headers["Key"] = key

    if secret_key:
        headers["Secret-Key"] = secret_key

    return headers


def _first_linked_sales_invoice(payment: frappe._dict) -> Optional[str]:
    """
    يُرجع أول فاتورة مبيعات مرتبطة بسند القبض (Payment Entry).
    يعتمد على child table: references (Payment Reference)
    """
    for ref in (payment.get("references") or []):
        if ref.get("reference_doctype") == "Sales Invoice" and ref.get("reference_name"):
            return ref.get("reference_name")
    return None


def _get_party_tax_id(party_type: str, party: str) -> str:
    """
    يحاول استخراج الرقم الضريبي للعميل/المورد من DocType الطرف (Customer / Supplier)
    أو من العنوان المرتبط إذا كان محفوظًا هناك.
    """
    tax_id = ""
    if party_type == "Customer":
        cust = frappe.get_doc("Customer", party)
        tax_id = (cust.tax_id or "").strip()
    elif party_type == "Supplier":
        sup = frappe.get_doc("Supplier", party)
        tax_id = (sup.tax_id or "").strip()

    # محاولة من العناوين
    if not tax_id:
        try:
            addr_link = frappe.get_all(
                "Dynamic Link",
                filters={"link_doctype": party_type, "link_name": party, "parenttype": "Address"},
                fields=["parent"]
            )
            if addr_link:
                addr = frappe.get_doc("Address", addr_link[0].parent)
                tax_id = (addr.tax_id or "").strip()
        except Exception:
            pass

    return tax_id


def _round(n: Any) -> float:
    try:
        return float(flt(n, 3))
    except Exception:
        return 0.0


# =========================================
# Payload Builders (Payment → JoFotara invoice JSON)
# =========================================

def _build_supplier_dict(company: frappe._dict) -> Dict[str, Any]:
    """
    بيانات البائع (المنشأة) من Company.
    يُفضّل ضبط الحقول التالية في Company:
      - tax_id (الرقم الضريبي)
      - country / default_currency
      - default_receivable_account / default_payable_account
      - address / phone (اختياري)
    """
    tax_id = (company.tax_id or "").strip()
    return {
        "name": (company.company_name or company.name),
        "taxNumber": tax_id,
        "country": (company.country or "JO"),
    }


def _build_buyer_dict(payment: frappe._dict, sales_invoice: Optional[frappe._dict]) -> Dict[str, Any]:
    """
    بيانات المشتري (العميل) تُؤخذ من Payment أو من Sales Invoice إن وُجدت.
    """
    party_type = payment.party_type
    party = payment.party
    buyer_name = party

    if party_type == "Customer":
        try:
            cust = frappe.get_doc("Customer", party)
            buyer_name = cust.customer_name or cust.name
        except Exception:
            pass

    # الرقم الضريبي
    buyer_tax = _get_party_tax_id(party_type, party)

    currency = JOD
    if sales_invoice:
        currency = sales_invoice.get("currency") or currency

    return {
        "name": buyer_name,
        "taxNumber": buyer_tax,
        "country": "JO",
        "currency": currency,
    }


def _build_lines_from_payment(payment: frappe._dict, sales_invoice: Optional[frappe._dict]) -> List[Dict[str, Any]]:
    """
    يبني بنود الفاتورة (سند الدفع كخط واحد افتراضيًا).
    - لو كانت الدفعة مرتبطة بفاتورة بيع، نأخذ الوصف/المرجع منها.
    - قيمة البند = مبلغ الدفعة بالعملة.
    """
    description = _("Payment Receipt")
    if sales_invoice:
        description = _("Payment against Sales Invoice {0}").format(sales_invoice.name)

    amount = payment.get("paid_amount") or payment.get("received_amount") or 0.0
    currency = payment.get("paid_from_account_currency") or payment.get("paid_to_account_currency") \
               or (sales_invoice.get("currency") if sales_invoice else JOD) or JOD

    # الضريبة: الدفعة غالبًا لا تُنشئ ضريبة إضافية — لكن بعض البوابات تريد حقولًا صريحة "0".
    vat_rate = 0.0
    vat_amount = 0.0
    line_total_excl = _round(amount)
    line_total_incl = _round(amount + vat_amount)

    return [{
        "description": description,
        "quantity": 1,
        "unitPrice": _round(amount),
        "totalExcludingTax": _round(line_total_excl),
        "taxRate": _round(vat_rate),
        "taxAmount": _round(vat_amount),
        "totalIncludingTax": _round(line_total_incl),
        "currency": currency,
    }]


def _build_totals(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_excl = _round(sum(l.get("totalExcludingTax", 0) for l in lines))
    total_tax = _round(sum(l.get("taxAmount", 0) for l in lines))
    total_incl = _round(sum(l.get("totalIncludingTax", 0) for l in lines))
    return {
        "totalExcludingTax": total_excl,
        "totalTax": total_tax,
        "totalIncludingTax": total_incl,
    }


def _build_invoice_payload_from_payment(payment_name: str) -> Dict[str, Any]:
    """
    يبني حمولة JSON النهائية لإرسالها إلى JoFotara عبر endpoint /core/invoices/ (حسب سجلاتك).
    المفتاح الأعلى يجب أن يكون "invoice".
    """
    payment = frappe.get_doc("Payment Entry", payment_name)

    # محاولة إيجاد فاتورة مبيعات مرتبطة لاستخراج بعض الحقول المرجعية
    sinv_name = _first_linked_sales_invoice(payment)
    sales_invoice = frappe.get_doc("Sales Invoice", sinv_name) if sinv_name else None

    company = frappe.get_doc("Company", payment.company)

    supplier = _build_supplier_dict(company)
    buyer = _build_buyer_dict(payment, sales_invoice)
    lines = _build_lines_from_payment(payment, sales_invoice)
    totals = _build_totals(lines)

    doc_currency = buyer.get("currency") or (sales_invoice.get("currency") if sales_invoice else JOD) or JOD

    # IssueDate: تاريخ الدفعة
    issue_date = payment.get("posting_date") or nowdate()

    # رقم المستند (ID) و UUID و ICV — إن لم تكن لديك سلسلة رقمية من JoFotara،
    # أرسل رقم سند ERP كـ ID، واترك UUID فارغًا ليديره النظام.
    doc_id = payment.name
    doc_uuid = ""   # اتركه فارغًا إن كان النظام يعينه
    icv = str(payment.name)  # عدّاد داخلي (Internal Counter Value) — يمكنك حفظه كـ naming_series counter لو رغبت

    # ملاحظة الفاتورة
    note = payment.get("remarks") or payment.get("user_remark") or ""

    payload_invoice = {
        # بيانات أساسية
        "id": doc_id,
        "uuid": doc_uuid,
        "issueDate": str(issue_date),
        "invoiceTypeCode": INVOICE_TYPE_CODE_PAYMENT,  # مهم لتجاوز خطأ invoiceTypeCode
        "note": note,

        # العملات
        "documentCurrencyCode": doc_currency,
        "taxCurrencyCode": doc_currency,

        # مرجع العداد الداخلي
        "additionalDocumentReference": [
            {"id": "ICV", "uuid": icv}
        ],

        # الأطراف
        "supplier": supplier,
        "buyer": buyer,

        # البنود والإجماليات
        "lines": lines,
        "totals": totals,

        # معلومات السداد (اختياري — توضيحي)
        "paymentMeans": {
            "code": (payment.mode_of_payment or "CASH"),
            "paidAmount": _round(payment.get("paid_amount") or payment.get("received_amount") or 0.0),
        },
    }

    return {"invoice": payload_invoice}


# =========================================
# HTTP Client
# =========================================

def _post_invoice(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    يرسل الطلب إلى JoFotara.
    يعتمد على حقل base_url في JoFotara Settings.
    Endpoint المتوقع من سجلاتك: /core/invoices/
    """
    settings = _get_settings()
    base_url = (settings.base_url or "").strip()
    if not base_url:
        frappe.throw(_("Please set Base URL in JoFotara Settings"))

    url = _full_url(base_url, "/core/invoices/")
    headers = _build_headers(settings)

    debug_info = {
        "url": url,
        "headers": _mask_headers(headers),
        "payload_keys": list(payload.keys()),
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
    except Exception as ex:
        frappe.log_error(
            title="[JoFotara] Connection error",
            message=f"DEBUG: {json.dumps(debug_info, ensure_ascii=False)}\nERROR: {frappe.get_traceback()}"
        )
        frappe.throw(_("Connection error while contacting JoFotara"))

    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {"raw": text}

    # سجّل دائمًا
    frappe.logger().info(f"[JoFotara] Request DEBUG: {json.dumps(debug_info, ensure_ascii=False)}")
    frappe.logger().info(f"[JoFotara] Response ({resp.status_code}): {text[:4000]}")

    if resp.status_code >= 400:
        # ارمِ استثناءً واضحًا يتضمن أخطاء البوابة إن وُجدت
        msg = _extract_error_message(data) or _(f"HTTP {resp.status_code} error from JoFotara")
        frappe.throw(msg)

    return data


def _extract_error_message(data: Dict[str, Any]) -> str:
    """
    يُحاول استخلاص رسالة خطأ واضحة من هيكل الاستجابة القياسي.
    """
    if not isinstance(data, dict):
        return ""

    # أمثلة محتملة:
    # {
    #   "EINV_INV_UUID": null,
    #   "EINV_NUM": null,
    #   "EINV_QR": null,
    #   "EINV_RESULTS": {
    #     "ERRORS": [
    #       {"EINV_CATEGORY": "invoice", "EINV_CODE": "invoiceTypeCode", "EINV_MESSAGE": "رسالة ..."}
    #     ]
    #   }
    # }
    results = data.get(RESP_KEY_RESULTS) or {}
    errors = results.get("ERRORS") or []
    if errors:
        parts = []
        for e in errors:
            cat = e.get("EINV_CATEGORY") or ""
            code = e.get("EINV_CODE") or ""
            msg = e.get("EINV_MESSAGE") or ""
            parts.append(f"[{cat}/{code}] {msg}")
        return " ; ".join(parts)

    # إن لم يوجد RESULTS.Errors — حاول طباعة raw
    raw = data.get("raw")
    if raw:
        return str(raw)[:1000]

    return ""


# =========================================
# Public APIs
# =========================================

@frappe.whitelist()
def send_payment_to_jofotara(payment_name: str) -> Dict[str, Any]:
    """
    تُستدعى لإرسال سند قبض (Payment Entry) إلى JoFotara.
    - تبني الحمولة من الدفعة
    - ترسل إلى /core/invoices/
    - تحفظ UUID/QR/NUM في حقول مخصّصة على Payment Entry إن كانت متوفّرة.
    """
    if not payment_name:
        frappe.throw(_("Payment Entry name is required"))

    # بناء الحمولة
    payload = _build_invoice_payload_from_payment(payment_name)

    # الإرسال
    response = _post_invoice(payload)

    # حفظ نتائج مهمة على السند (إن أنشأت هذه الحقول في Payment Entry عبر Custom Fields)
    pe = frappe.get_doc("Payment Entry", payment_name)
    pe.db_set("jofotara_uuid", response.get(RESP_KEY_UUID), update_modified=False)
    pe.db_set("jofotara_qr", response.get(RESP_KEY_QR), update_modified=False)
    pe.db_set("jofotara_number", response.get(RESP_KEY_NUM), update_modified=False)
    pe.db_set("jofotara_last_response", json.dumps(response, ensure_ascii=False), update_modified=False)

    frappe.db.commit()

    return {
        "ok": True,
        "uuid": response.get(RESP_KEY_UUID),
        "number": response.get(RESP_KEY_NUM),
        "qr": response.get(RESP_KEY_QR),
        "raw": response,
    }


@frappe.whitelist()
def build_payment_payload_preview(payment_name: str) -> Dict[str, Any]:
    """
    دالة لفحص الحمولة قبل الإرسال (Debug).
    """
    if not payment_name:
        frappe.throw(_("Payment Entry name is required"))
    return _build_invoice_payload_from_payment(payment_name)
