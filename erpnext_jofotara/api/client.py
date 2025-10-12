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
    """
    يبني رؤوس JoFotara:
      - Client-Id / Secret-Key (أو Alt Auth بالـ Device)
      - Activity-Number أرقام فقط (1..15)
    """
    import re

    use_oauth2 = int(getattr(s, "use_oauth2", 0) or 0)

    client_id = (getattr(s, "client_id", None) or "").strip()
    client_secret = (s.get_password("secret_key", raise_exception=False) or "").strip()

    device_user = (getattr(s, "device_user", None) or "").strip()
    device_secret = (s.get_password("device_secret", raise_exception=False) or "").strip()

    if use_oauth2:
        if not client_id or not client_secret:
            frappe.throw("JoFotara Settings: الرجاء تعبئة Client ID و Secret Key (Enable OAuth2 مفعّل).")
    else:
        if not client_id and device_user:
            client_id = device_user
        if not client_secret and device_secret:
            client_secret = device_secret

    if not client_id or not client_secret:
        frappe.throw("JoFotara Settings: وفّر Client ID/Secret أو Device User/Secret.")

    # ✅ تنسيق Activity-Number: أرقام فقط 1..15
    raw_activity = (getattr(s, "activity_number", None) or "").strip()
    activity = re.sub(r"\D", "", raw_activity)
    if not (1 <= len(activity) <= 15):
        frappe.throw("JoFotara Settings: Activity Number مطلوب، أرقام فقط، من 1 إلى 15 رقم.")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "ar",
        "Client-Id": client_id,
        "Secret-Key": client_secret,
        "Activity-Number": activity,
        "Key": activity,  # بعض البيئات تتوقعه كـ Key أيضًا
    }

    return headers



# =========================
# Public functions
# =========================

def to_b64(xml_str: str) -> str:
    """حوّل XML إلى Base64 ASCII كما يطلب JoFotara."""
    return base64.b64encode(xml_str.encode("utf-8")).decode("ascii")


def post_invoice(b64xml: str) -> dict:
    """
    إرسال الفاتورة حسب الدليل 1.4:
      POST { "invoice": "<Base64(XML)>" }
      إلى: base_url + submit_url
      مع رؤوس Client-Id/Secret-Key
    """
    s = _get_settings()

    # استخدم الحقول الموجودة في DocType (مش endpoint_base/invoices_path)
    base = (getattr(s, "base_url", None) or "https://backend.jofotara.gov.jo").strip()
    path = (getattr(s, "submit_url", None) or "/core/invoices/").strip()
    url = _full_url(base, path)

    payload = {"invoice": b64xml}
    headers = _build_headers(s)

    frappe.logger().info({
        "jofotara_url": url,
        "headers": _mask_headers(headers),
        "payload_keys": list(payload.keys())
    })

    try:
        # استخدم json=payload عشان يحدد Content-Length و JSON تلقائيًا
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
    except Exception as e:
        frappe.throw(f"JoFotara network error: {e}")

    # حاول تقرأ JSON وإلا رجّع النص
    try:
        data = resp.json()
    except Exception:
        data = {"text": resp.text or ""}

    # خزّن آخر رد للمراجعة السريعة في Settings
    try:
        s.db_set("last_response", json.dumps(data, ensure_ascii=False)[:1400])
    except Exception:
        pass

    if resp.status_code >= 400:
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
