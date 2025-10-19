# -*- coding: utf-8 -*-
# erpnext_jofotara/api/invoices.py
from __future__ import annotations

import json
import base64
from typing import Any, Dict

import frappe
from frappe import _
from frappe.utils import now, get_qr_code  # نستخدم get_qr_code لتوليد صورة الـ QR من النص

# الاعتماد على نفس الباكدج
from .client import post_invoice, to_b64          # post_invoice(b64xml) -> dict
from .transform import build_invoice_xml          # build_invoice_xml(sales_invoice_name) -> xml string


# =========================
# Utilities
# =========================

def _get_settings():
    """Fetch JoFotara Settings single doctype."""
    return frappe.get_single("JoFotara Settings")


def _minify_xml(xml_str: str) -> str:
    """تنظيف XML من المسافات/الأسطر الزائدة مع الحفاظ على المحتوى."""
    if not xml_str:
        return xml_str
    s = xml_str.replace("\r", "").replace("\n", "").replace("\t", "").strip()
    while "  " in s:
        s = s.replace("  ", " ")
    s = s.replace("> <", "><")
    return s


def _store_response_preview_in_settings(resp: Dict[str, Any]) -> None:
    """خزن ملخص الرد في Settings لتسهيل الديبج."""
    try:
        s = _get_settings()
        s.db_set("last_response", json.dumps(resp, ensure_ascii=False)[:1400])
    except Exception:
        pass


def _set_status(doc, status: str, err: str | None = None) -> None:
    """
    تحديث حالة التكامل على الفاتورة.
    NOTE: خيارات الحقل عندك: Pending / Submitted / Error
    """
    try:
        if doc.meta.has_field("jofotara_status"):
            doc.db_set("jofotara_status", status)
        if err and doc.meta.has_field("jofotara_error"):
            doc.db_set("jofotara_error", err[:1000])
    except Exception:
        # ما نوقف التنفيذ بسبب فشل تحديث حالة العرض فقط
        pass


def _save_xml_snapshot(doc, xml_str: str) -> None:
    """
    احفظ نسخة من UBL XML على الفاتورة كمرفق،
    ولو فيه حقل jofotara_xml اكتبه أيضاً،
    وخزّن نسخة قصيرة في Settings (اختياري).
    """
    try:
        if doc.meta.has_field("jofotara_xml"):
            doc.db_set("jofotara_xml", xml_str)

        frappe.get_doc({
            "doctype": "File",
            "file_name": f"{doc.name}-ubl.xml",
            "content": xml_str,
            "is_private": 1,
            "attached_to_doctype": "Sales Invoice",
            "attached_to_name": doc.name,
        }).insert(ignore_permissions=True)

        try:
            s = _get_settings()
            if s.meta.has_field("last_xml"):
                s.db_set("last_xml", xml_str[:100000])
        except Exception:
            pass

    except Exception:
        frappe.log_error(frappe.get_traceback(), "JoFotara - save XML snapshot")


def _save_qr_image_on_invoice(inv_doc) -> None:
    """
    توليد صورة QR من نص الـ QR (payload) الموجود بالحقل jofotara_qr،
    حفظها كمرفق PNG، وتخزين رابطها في Attach Image: jofotara_qr_image.
    """
    try:
        # لازم يكون عندنا النص (payload) في الحقل jofotara_qr
        if not inv_doc.meta.has_field("jofotara_qr"):
            return

        qr_text = (getattr(inv_doc, "jofotara_qr", "") or "").strip()
        if not qr_text:
            return

        # 1) توليد Data-URI PNG من النص
        #    مثال: "data:image/png;base64,...."
        data_uri = get_qr_code(qr_text)
        prefix = "data:image/png;base64,"
        if data_uri.startswith(prefix):
            b64_png = data_uri[len(prefix):]
        else:
            # احتياط لو رجعت بدون البادئة
            b64_png = data_uri

        # 2) فك Base64 إلى بايتات PNG صحيحة
        try:
            content = base64.b64decode(b64_png)
        except Exception:
            # معالجة padding لو ناقص
            missing = len(b64_png) % 4
            if missing:
                b64_png += "=" * (4 - missing)
            content = base64.b64decode(b64_png)

        # 3) خزِّن الصورة كمرفق
        filedoc = frappe.get_doc({
            "doctype": "File",
            "file_name": f"{inv_doc.name}-qr.png",
            "is_private": 1,
            "content": content,
            "attached_to_doctype": "Sales Invoice",
            "attached_to_name": inv_doc.name,
        }).insert(ignore_permissions=True)

        # 4) خزِّن رابط الصورة في Attach Image (لازم يكون اسمه jofotara_qr_image)
        if inv_doc.meta.has_field("jofotara_qr_image"):
            inv_doc.db_set("jofotara_qr_image", filedoc.file_url)

    except Exception:
        # ما نكسر العملية لو فشل التخزين
        frappe.log_error(frappe.get_traceback(), "JoFotara - save QR image")


def _apply_response_to_invoice(doc, resp: Dict[str, Any]) -> None:
    """
    تطبيق الرد: حفظ UUID/QR/وقت الإرسال، توليد صورة الـ QR، تحديث الحالة،
    إضافة تعليق بالرد، وتخزين معاينة الرد في Settings.
    """
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

    # ⬇️ لو فيه QR نصي، حوّله لصورة واحفظها
    if qr:
        _save_qr_image_on_invoice(doc)

    # حدّث الحالة: Submitted عند النجاح / Error عند الفشل
    _set_status(doc, "Submitted" if (uuid or qr) else "Error")

    # تعليق بالرد (للمرجعية)
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
      - بناء UBL 2.1
      - Base64
      - POST {"invoice": "<b64>"} إلى /core/invoices/
    """
    # 1) الفاتورة
    doc = frappe.get_doc("Sales Invoice", name)

    # 2) توليد XML (يراعي 388 للفاتورة و 381 للاشعار الدائن)
    xml = build_invoice_xml(doc.name)
    if not xml:
        frappe.throw(_("Failed to build UBL 2.1 XML for this invoice."))

    # 3) Snapshot + Base64
    xml_min = _minify_xml(xml)
    _save_xml_snapshot(doc, xml_min)
    b64 = to_b64(xml_min)

    # 4) الإرسال
    try:
        resp = post_invoice(b64)
    except Exception as e:
        _set_status(doc, "Error", err=str(e))
        frappe.log_error(frappe.get_traceback(), "JoFotara Send Now Error")
        raise

    # 5) تطبيق الرد
    _apply_response_to_invoice(doc, resp)

    # 6) إشعار
    frappe.msgprint(_("JoFotara: Invoice submitted successfully."), alert=1, indicator="green")
    return resp


def on_submit_sales_invoice(doc, method: str | None = None) -> None:
    """
    Hook يُستدعى عند Submit للفاتورة — يرسل تلقائيًا لو الخيار مفعّل في الإعدادات.
    يدعم الاسمين: send_on_submit و auto_send_on_submit.
    """
    try:
        s = _get_settings()
        enabled = 0
        for fname in ("send_on_submit", "auto_send_on_submit"):
            if getattr(s, fname, None):
                enabled = int(getattr(s, fname) or 0)
                break
        if not enabled:
            return

        send_now(doc.name)

    except Exception as e:
        _set_status(doc, "Error", err=str(e))
        frappe.log_error(frappe.get_traceback(), "JoFotara on_submit error")


# Alias قديم للتوافق
def on_submit_send(doc, method=None):
    return on_submit_sales_invoice(doc, method)


@frappe.whitelist()
def retry_pending_jobs():
    # TODO: لو حبيت تعمل retries لاحقًا
    pass
