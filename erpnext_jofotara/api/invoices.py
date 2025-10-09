# erpnext_jofotara/api/invoices.py
import base64, json, requests
from urllib.parse import urljoin
import frappe
from frappe import _
from erpnext_jofotara.install import ensure_custom_fields

def _full_url(base, path):
    return path if (path or "").startswith("http") else urljoin(base.rstrip("/") + "/", (path or "").lstrip("/"))

def _get_settings():
    return frappe.get_single("JoFotara Settings")  # اسم الدوكتايب عندك "JoFotara" أو "JoFotara" حسب اختيارك

def on_submit_send(doc, method=None):
    s = _get_settings()
    # احترام الإعداد
    if not s.get("send_on_submit"):
        return
    # ما تبعتش مرتجع
    if getattr(doc, "is_return", 0):
        return

    try:
        ensure_custom_fields()

        # ⚠️ لازم يكون عندك XML UBL جاهز. مؤقتًا: خليه من حقل مخصص أو ملفاتك.
        xml_str = getattr(doc, "jofotara_xml", None)  # لو عامل Custom Field لتجربة سريعة
        if not xml_str:
            # TODO: هنا ضيف دالة توليد UBL 2.1 من الفاتورة لو جاهز
            frappe.throw(_("Missing UBL XML (field jofotara_xml). Please generate UBL 2.1 and try again."))

        xml_bytes = xml_str.encode("utf-8")
        payload = {"invoice": base64.b64encode(xml_bytes).decode()}

        url = _full_url(s.base_url, s.submit_url or "/core/invoices/")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Client-Id": s.client_id,
            "Secret-Key": s.secret_key,
            "Accept-Language": "ar",
        }

        r = requests.post(url, json=payload, headers=headers, timeout=90)
        if r.status_code >= 400:
            raise frappe.ValidationError(f"JoFotara API error {r.status_code}: {r.text}")

        resp = r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text}
        handle_submit_response(doc, resp)

    except Exception as e:
        ensure_custom_fields()
        if doc.meta.has_field("jofotara_status"):
            doc.db_set("jofotara_status", "Error")
        frappe.log_error(frappe.get_traceback(), "JoFotara Submit Error")
        frappe.throw(_("JoFotara submission failed: {0}").format(str(e)))

def handle_submit_response(doc, resp: dict):
    """حدّث الحقول حسب رد JoFotara"""
    ensure_custom_fields()

    # حاول تلاقي UUID و الـ QR من مفاتيح شائعة
    uuid = resp.get("uuid") or resp.get("invoiceUUID") or resp.get("invoice_uuid") or resp.get("id")
    qr   = resp.get("qr")   or resp.get("qrCode")      or resp.get("qr_code")       or resp.get("qrcode")

    status = "Submitted" if (uuid or qr or str(resp).lower().find("success") >= 0) else "Error"

    if doc.meta.has_field("jofotara_status"):
        doc.db_set("jofotara_status", status)
    if uuid and doc.meta.has_field("jofotara_uuid"):
        doc.db_set("jofotara_uuid", uuid)
    # ملاحظة: لو JoFotara بيرجع الـ QR كـ Base64 PNG، نخزّنه كما هو ونطبعه
    if qr and doc.meta.has_field("jofotara_qr"):
        doc.db_set("jofotara_qr", qr)

    # سجّل الرد للمتابعة
    doc.add_comment("Comment", text=frappe.as_json(resp, indent=2))
