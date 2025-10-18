# -*- coding: utf-8 -*-
# erpnext_jofotara/api/invoices.py
from __future__ import annotations

import json
import base64
from typing import Any, Dict

import frappe
from frappe import _
from frappe.utils import now

from .client import post_invoice, to_b64        # post_invoice(b64xml) -> dict
from .transform import build_invoice_xml        # build_invoice_xml(sales_invoice_name) -> xml string


# =========================
# Utilities
# =========================

def _get_settings():
    return frappe.get_single("JoFotara Settings")


def _minify_xml(xml_str: str) -> str:
    if not xml_str:
        return xml_str
    s = xml_str.replace("\r", "").replace("\n", "").replace("\t", "").strip()
    while "  " in s:
        s = s.replace("  ", " ")
    s = s.replace("> <", "><")
    return s


def _store_response_preview_in_settings(resp: Dict[str, Any]) -> None:
    try:
        s = _get_settings()
        s.db_set("last_response", json.dumps(resp, ensure_ascii=False)[:1400])
    except Exception:
        pass


def _set_status(doc, status: str, err: str | None = None) -> None:
    try:
        if doc.meta.has_field("jofotara_status"):
            doc.db_set("jofotara_status", status)
        if err and doc.meta.has_field("jofotara_error"):
            doc.db_set("jofotara_error", err[:1000])
    except Exception:
        pass


def _save_xml_snapshot(doc, xml_str: str):
    """احفظ نسخة من الـ XML على الفاتورة، وكمان في الإعدادات (اختياري)."""
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
    اقرأ Base64 من jofotara_qr، اعمل منه ملف PNG،
    وخزِّن رابط الصورة في حقل Attach Image: jofotara_qr_image.
    """
    try:
        if not inv_doc.meta.has_field("jofotara_qr"):
            return
        raw = (getattr(inv_doc, "jofotara_qr", "") or "").strip()
        if not raw:
            return

        # نظّف واحذف بادئة data: لو موجودة
        raw = raw.replace("\n", "").replace("\r", "").replace(" ", "")
        if "," in raw:
            raw = raw.split(",", 1)[1]

        try:
            content = base64.b64decode(raw)
        except Exception:
            # أحياناً padding ناقص – جرّب إصلاحه
            missing = len(raw) % 4
            if missing:
                raw += "=" * (4 - missing)
            content = base64.b64decode(raw)

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
        # ما نكسر العملية لو فشل التخزين
        frappe.log_error(frappe.get_traceback(), "JoFotara - save QR image")


def _apply_response_to_invoice(doc, resp: Dict[str, Any]) -> None:
    """طبّق رد JoFotara (UUID/QR) واحفظ صورة الـ QR إن وُجدت."""
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

    # احفظ صورة الـ QR كمرفق (لو فيه QR)
    if qr:
        _save_qr_image_on_invoice(doc)

    _set_status(doc, "Success" if (uuid or qr) else "Error")

    try:
        doc.add_comment("Comment", text=json.dumps(resp, ensure_ascii=False, indent=2))
    except Exception:
        pass

    _store_response_preview_in_settings(resp)


# =========================
# Public API
# =========================

@frappe.whitelist()
def send_now(name: str) -> Dict[str, Any]:
    """إرسال فاتورة واحدة إلى JoFotara يدوياً."""
    # 1) الفاتورة
    doc = frappe.get_doc("Sales Invoice", name)

    # 2) توليد الـ UBL
    xml = build_invoice_xml(doc.name)
    if not xml:
        frappe.throw(_("Failed to build UBL 2.1 XML for this invoice."))

    # 3) سنابشوت + Base64
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

    # 5) طبّق الرد
    _apply_response_to_invoice(doc, resp)

    # 6) إشعار
    frappe.msgprint(_("JoFotara: Invoice submitted successfully."), alert=1, indicator="green")
    return resp


def on_submit_sales_invoice(doc, method: str | None = None) -> None:
    """Hook عند Submit للفاتورة—يبعت تلقائيًا لو الخيار مفعّل في الإعدادات."""
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


# alias قديم للتوافق
def on_submit_send(doc, method=None):
    return on_submit_sales_invoice(doc, method)


@frappe.whitelist()
def retry_pending_jobs():
    pass
