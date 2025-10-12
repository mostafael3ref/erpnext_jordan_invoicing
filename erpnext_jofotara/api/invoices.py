# erpnext_jofotara/api/invoices.py
from __future__ import annotations

import json
from typing import Any, Dict

import frappe
from frappe import _
from frappe.utils import now

# نعتمد على client.py و transform.py داخل نفس الباكدج
from .client import post_invoice, to_b64  # post_invoice(b64xml) -> dict
from .transform import build_invoice_xml   # build_invoice_xml(sales_invoice_name) -> xml string


# =========================
# Utilities
# =========================

def _get_settings():
    """Fetch JoFotara Settings single doctype."""
    return frappe.get_single("JoFotara Settings")


def _minify_xml(xml_str: str) -> str:
    """إزالة المسافات والأسطر غير الضرورية (يحافظ على المحتوى)."""
    if not xml_str:
        return xml_str
    s = xml_str.replace("\r", "").replace("\n", "").replace("\t", "").strip()
    # لا نستخدم regex ثقيل هنا لتفادي كسر وسوم ضمن نصوص، هذا كافي للـ UBL المبني تلقائياً
    while "  " in s:
        s = s.replace("  ", " ")
    s = s.replace("> <", "><")
    return s


def _store_response_preview_in_settings(resp: Dict[str, Any]) -> None:
    """خزن آخر رد (مختصر) في JoFotara Settings ليسهل الديبغ من الديسكتوب."""
    try:
        s = _get_settings()
        s.db_set("last_response", json.dumps(resp, ensure_ascii=False)[:1400])
    except Exception:
        pass


def _set_status(doc, status: str, err: str | None = None) -> None:
    """تحديث حالة التكامل على الفاتورة (آمن حتى لو الحقول غير موجودة)."""
    try:
        if doc.meta.has_field("jofotara_status"):
            doc.db_set("jofotara_status", status)
        if err and doc.meta.has_field("jofotara_error"):
            doc.db_set("jofotara_error", err[:1000])
    except Exception:
        # ما نوقف التنفيذ بسبب فشل تحديث حالة العرض فقط
        pass


def _apply_response_to_invoice(doc, resp: Dict[str, Any]) -> None:
    """مطابقة رد JoFotara وتخزين UUID/QR + الختم الزمني."""
    uuid = (
        resp.get("EINV_INV_UUID")
        or resp.get("UUID")
        or resp.get("invoice_uuid")
        or resp.get("invoiceUUID")
        or resp.get("id")
        or ""
    )
    qr = (
        resp.get("EINV_QR")
        or resp.get("qr")
        or resp.get("qrCode")
        or resp.get("qr_code")
        or ""
    )

    try:
        if uuid and doc.meta.has_field("jofotara_uuid"):
            doc.db_set("jofotara_uuid", uuid)
        if qr and doc.meta.has_field("jofotara_qr"):
            doc.db_set("jofotara_qr", qr)
        if doc.meta.has_field("jofotara_sent_at"):
            doc.db_set("jofotara_sent_at", now())
    except Exception:
        pass

    # حدّث الحالة
    _set_status(doc, "Success" if uuid or qr else "Error")

    # أضف تعليقًا بنص الرد (مفيد للرجوع)
    try:
        doc.add_comment("Comment", text=json.dumps(resp, ensure_ascii=False, indent=2))
    except Exception:
        pass

    # خزّن معاينة الرد في Settings
    _store_response_preview_in_settings(resp)


# =========================
# Public API
# =========================

@frappe.whitelist()
def send_now(name: str) -> Dict[str, Any]:
    """
    إرسال فاتورة Sales Invoice واحدة إلى JoFotara يدويًا.
    المتطلبات حسب الدليل:
      - XML بصيغة UBL 2.1
      - تحويل Base64
      - POST إلى /core/invoices/ مع {"invoice":"<b64>"}
      - رؤوس: Client-Id, Secret-Key, Content-Type: application/json
    """
    # 1) إحضار الفاتورة
    doc = frappe.get_doc("Sales Invoice", name)

    # 2) توليد XML (يُفترض أن build_invoice_xml يراعي 388 للفاتورة و381 للإشعار الدائن)
    xml = build_invoice_xml(doc.name)
    if not xml:
        frappe.throw(_("Failed to build UBL 2.1 XML for this invoice."))

    # 3) تحسين بسيط ثم Base64
    xml_min = _minify_xml(xml)
    b64 = to_b64(xml_min)

    # 4) الإرسال عبر عميل HTTP (يراعي الإعدادات والرؤوس حسب الدليل)
    try:
        resp = post_invoice(b64)
    except Exception as e:
        # سجّل الخطأ على الفاتورة للشفافية
        _set_status(doc, "Error", err=str(e))
        frappe.log_error(frappe.get_traceback(), "JoFotara Send Now Error")
        raise

    # 5) تطبيق الرد على الفاتورة
    _apply_response_to_invoice(doc, resp)

    # 6) رسالة تأكيد لطيفة
    frappe.msgprint(_("JoFotara: Invoice submitted successfully."), alert=1, indicator="green")
    return resp


def on_submit_sales_invoice(doc, method: str | None = None) -> None:
    """
    Hook يُستدعى عند اعتماد Sales Invoice.
    - لو JoFotara Settings فيه auto_send_on_submit = 1 → يرسل مباشرة.
    - يتجنب الإرسال لو الفاتورة مرتجعة ولكن المنطق التفصيلي (381) يجب أن يكون في build_invoice_xml.
    """
    try:
        s = _get_settings()
        if not getattr(s, "auto_send_on_submit", 0):
            return

        # أرسل الآن
        send_now(doc.name)

    except Exception as e:
        # لا نكسر دورة الاعتماد بسبب التكامل؛ نسجل الخطأ ونحدّث الحالة فقط
        _set_status(doc, "Error", err=str(e))
        frappe.log_error(frappe.get_traceback(), "JoFotara on_submit error")
