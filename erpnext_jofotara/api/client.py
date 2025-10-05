import base64
import requests
import frappe

def _full_url(settings, path):
    base = (settings.base_url or "").rstrip("/")
    path = (path or "").lstrip("/")
    return f"{base}/{path}"

def get_oauth2_token(settings):
    url = _full_url(settings, settings.token_url)
    data = {
        "grant_type": "client_credentials",
        "client_id": settings.client_id,
        "client_secret": settings.get_password("secret_key"),
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(url, data=data, headers=headers, timeout=30)
    if resp.status_code >= 400:
        frappe.throw(f"JoFotara OAuth2 token failed: {resp.text}")
    token = resp.json().get("access_token")
    if not token:
        frappe.throw("JoFotara token missing in response")
    return token

def auth_headers(settings):
    if settings.use_oauth2:
        token = get_oauth2_token(settings)
        return {"Authorization": f"Bearer {token}"}
    # بديل “ربط الأجهزة” (يوزرميم + سيكريت في الهيدر)
    device_user = (settings.device_user or "").strip()
    device_secret = settings.get_password("device_secret") or ""
    if not device_user or not device_secret:
        frappe.throw("Device User/Secret not configured for header auth")
    basic = base64.b64encode(f"{device_user}:{device_secret}".encode()).decode()
    return {"Authorization": f"Basic {basic}"}

def post(settings, endpoint, json=None, data=None, headers=None):
    url = _full_url(settings, endpoint)
    base_headers = headers or {}
    base_headers.setdefault("Accept", "application/json")
    if json is not None:
        base_headers.setdefault("Content-Type", "application/json")
    resp = requests.post(url, json=json, data=data, headers=base_headers, timeout=60)
    return resp

def post_xml(settings, endpoint, xml_bytes, headers=None):
    url = _full_url(settings, endpoint)
    base_headers = headers or {}
    base_headers.setdefault("Content-Type", "application/xml")
    resp = requests.post(url, data=xml_bytes, headers=base_headers, timeout=60)
    return resp
