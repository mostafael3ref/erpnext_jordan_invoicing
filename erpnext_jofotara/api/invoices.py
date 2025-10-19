# -*- coding: utf-8 -*-
# erpnext_jofotara/api/invoices.py
from __future__ import annotations

import json
from typing import Any, Dict
from urllib.parse import quote

import requests
import frappe
from frappe import _
from frappe.utils import now

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
    """تحديث حالة التكامل على الفاتورة."""
    try:
        if doc.meta.has_field("jofotara_status"):
            doc.db_set("jofotara_status", status)
        if err and doc.meta.has_field("jofotara_error"):
            doc.db_set("jofotara_error", err[:1000])
    except Exception:
        pass


def _save_xml_snapshot(doc, xml_str: str) -> None:
    """احفظ نسخة من XML كمرفق على الفاتورة."""
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


def _generate_qr_image_bytes(data: str) -> bytes:
    """
    توليد صورة QR بدون مكتبات خارجية.
    نجرب أكثر من مزوّد حتى لو أحدهم عطّل أو رجّع CORS/حجب (يعمل على Frappe Cloud).
    """
    try:
        payload = quote((data or "").strip(), safe="")

        providers = [
            f"https://chart.googleapis.com/chart?cht=qr&chs=250x250&chld=L|0&chl={payload}",
            f"https://quickchart.io/qr?size=250&text={payload}",
        ]

        for url in providers:
            try:
                resp = requests.get(url, timeout=10)
                if resp.ok and resp.content and resp.headers.get("Content-Type", "").startswith("image/"):
                    return resp.content
            except Exception:
                # جرّب المزوّد اللي بعده
                continue

        return b""
    except Exception:
        return b""


def _save_qr_image_on_invoice(inv_doc) -> None:
    """
    توليد صورة QR من نص الـ QR (payload) الموجود بالحقل jofotara_qr،
    حفظها كمرفق PNG، وتخزين رابطها في Attach Image: jofotara_qr_image.
    """
    try:
        if not inv_doc.meta.has_field("jofotara_qr"):
            return

        qr_text = (getattr(inv_doc, "jofotara_qr", "") or "").strip()
        if not qr_text:
            return

        content = _generate_qr_image_bytes(qr_text)
        if not content:
            return

        filedoc = frappe.get_doc({
            "doctype": "File",
            "file_name": f"{inv_doc.name}-qr.png",
            "is_private": 1,
            "content": content,
            "attached_to_doctype": "Sales Invoice",
            "attached_to_name": inv_doc.name,
        }).insert(ignore_permissions=True)

        if inv_doc.meta.has_field("jofotara_qr_image"):
            inv_doc.db_set("jofotara_qr_image", filedoc.file_url)

    except Exception:
        frappe.log_error(frappe.get_traceback(), "JoFotara - save QR image")


def _apply_response_to_invoice(doc, resp: Dict[str, Any]) -> None:
    """تطبيق الرد من JoFotara وتحديث الحقول والصورة."""
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

    # لو فيه QR نصي، حوّله لصورة واحفظها
    if qr:
        _save_qr_image_on_invoice(doc)

    # Submitted عند النجاح / Error عند الفشل
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
    """إرسال فاتورة Sales Invoice واحدة إلى JoFotara يدويًا."""
    doc = frappe.get_doc("Sales Invoice", name)

    xml = build_invoice_xml(doc.name)
    if not xml:
        frappe.throw(_("Failed to build UBL 2.1 XML for this invoice."))

    xml_min = _minify_xml(xml)
    _save_xml_snapshot(doc, xml_min)
    b64 = to_b64(xml_min)

    try:
        resp = post_invoice(b64)
    except Exception as e:
        _set_status(doc, "Error", err=str(e))
        frappe.log_error(frappe.get_traceback(), "JoFotara Send Now Error")
        raise

    _apply_response_to_invoice(doc, resp)

    frappe.msgprint(_("JoFotara: Invoice submitted successfully."), alert=1, indicator="green")
    return resp


def on_submit_sales_invoice(doc, method: str | None = None) -> None:
    """Hook عند Submit للفاتورة — يرسل تلقائيًا لو الخيار مفعّل في الإعدادات."""
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


# Backward-compatible alias
def on_submit_send(doc, method=None):
    return on_submit_sales_invoice(doc, method)


@frappe.whitelist()
def attach_qr_image(name: str):
    """
    (يدويًا) ولّد صورة QR على السيرفر واربطها بالفاتورة.
    مفيد لو عندك فواتير قديمة فيها نص QR بدون صورة.
    """
    doc = frappe.get_doc("Sales Invoice", name)
    qr_text = (getattr(doc, "jofotara_qr", "") or "").strip()
    if not qr_text:
        frappe.throw(_("No QR payload found on this invoice."))

    content = _generate_qr_image_bytes(qr_text)
    if not content:
        frappe.throw(_("Could not fetch QR image from providers."))

    filedoc = frappe.get_doc({
        "doctype": "File",
        "file_name": f"{doc.name}-qr.png",
        "is_private": 1,
        "content": content,
        "attached_to_doctype": "Sales Invoice",
        "attached_to_name": doc.name,
    }).insert(ignore_permissions=True)

    if doc.meta.has_field("jofotara_qr_image"):
        doc.db_set("jofotara_qr_image", filedoc.file_url)

    return {"file_url": filedoc.file_url}


@frappe.whitelist()
def retry_pending_jobs():
    # مستقبلًا: تقدر تضيف هنا منطق إعادة الإرسال التلقائي
    pass
