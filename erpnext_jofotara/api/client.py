# erpnext_jofotara/api/client.py
from __future__ import annotations

import base64
import json
from urllib.parse import urljoin

import requests
import frappe


# =========================
# Helpers
# =========================

def _get_settings():
    return frappe.get_single("JoFotara Settings")


def _full_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/") + "/"
    path = (path or "").lstrip("/")
    return urljoin(base, path)


def _mask_headers(h: dict) -> dict:
    masked = dict(h or {})
    for k in ("Secret-Key", "Authorization", "Device-Secret"):
        if k in masked and masked[k]:
            masked[k] = "********"
    return masked


def _build_headers(s) -> dict:
    client_id = (s.client_id or "").strip()
    client_secret = (s.get_password("secret_key", raise_exception=False) or "").strip()

    if not client_id or not client_secret:
        frappe.throw("JoFotara Settings: Client-Id/Secret-Key are required.")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "ar",
        "Client-Id": client_id,
        "Secret-Key": client_secret,
    }

    # رؤوس اختيارية لو موجودة بالإعدادات
    activity = (getattr(s, "activity_number", None) or "").strip()
    if activity:
        headers["Activity-Number"] = activity
        headers["Key"] = activity  # بعض البيئات تطلبه

    return headers


# =========================
# Public functions
# =========================

def to_b64(xml_str: str) -> str:
    """حوّل XML إلى Base64 ASCII كما يطلب JoFotara."""
    return base64.b64encode(xml_str.encode("utf-8")).decode("ascii")


def post_invoice(b64xml: str) -> dict:
    """
    يرسل الفاتورة إلى JoFotara حسب الدليل 1.4:
      - POST { "invoice": "<Base64(XML)>" }
      - رؤوس: Client-Id, Secret-Key, Content-Type: application/json
      - المسار الافتراضي: https://backend.jofotara.gov.jo/core/invoices/
    """
    s = _get_settings()
    base = (getattr(s, "endpoint_base", None) or "https://backend.jofotara.gov.jo").strip()
    path = (getattr(s, "invoices_path", None) or "/core/invoices/").strip()
    url = _full_url(base, path)

    payload = {"invoice": b64xml}
    headers = _build_headers(s)

    # لوج نظيف لا يظهر الأسرار
    frappe.logger().info({
        "jofotara_url": url,
        "headers": _mask_headers(headers),
        "payload_keys": list(payload.keys())
    })

    try:
        resp = requests.post(url, data=json.dumps(payload), headers=headers, timeout=30)
    except Exception as e:
        frappe.throw(f"JoFotara network error: {e}")

    # حاول JSON وإلا خذ النص الخام
    try:
        data = resp.json()
    except Exception:
        data = {"text": resp.text}

    # خزّن معاينة الرد في الإعدادات للمراجعة
    try:
        s.db_set("last_response", json.dumps(data, ensure_ascii=False)[:1400])
    except Exception:
        pass

    if resp.status_code >= 400:
        # سجل التفاصيل لمساعدتك في التصحيح
        frappe.log_error(
            title="JoFotara API Error",
            message=(
                f"URL: {url}\n"
                f"Status: {resp.status_code}\n"
                f"Request Headers (masked): {frappe.as_json(_mask_headers(headers))}\n"
                f"Payload keys: {list(payload.keys())}\n"
                f"Response Body:\n{frappe.as_json(data)}"
            ),
        )
        frappe.throw(f"JoFotara HTTP {resp.status_code}: {data}")

    return data
