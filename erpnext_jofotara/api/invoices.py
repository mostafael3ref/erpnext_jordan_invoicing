import json
import frappe
from frappe import _
from frappe.utils import cint
from .client import auth_headers, post, post_xml
from .transform import invoice_to_json, invoice_to_upl21_xml_base64

def _get_settings():
    return frappe.get_single("JoFotara Settings")

def _ensure_fields(doc):
    for fn in ("jofotara_status","jofotara_uuid","jofotara_qr"):
        if not doc.meta.get_field(fn):
            # في حال لم تتولد الحقول (لو after_install لم يعمل)
            try:
                doc.db_additionals()
            except Exception:
                pass

@frappe.whitelist()
def send_invoice(name: str):
    doc = frappe.get_doc("Sales Invoice", name)
    settings = _get_settings()
    _ensure_fields(doc)

    headers = auth_headers(settings)

    if (settings.payload_format or "XML_UPL_2_1") == "XML_UPL_2_1":
        # XML Base64 in JSON envelope (شائع في بعض التكاملات)
        payload_b64 = invoice_to_upl21_xml_base64(doc, settings)
        # معظم البوابات تقبل JSON بحقل data/base64؛ غيّر المفتاح إذا كان مختلفًا
        body = {"activityNumber": settings.activity_number, "data": payload_b64}
        resp = post(settings, settings.submit_url, json=body, headers=headers)
    else:
        body = invoice_to_json(doc, settings)
        resp = post(settings, settings.submit_url, json=body, headers=headers)

    if resp.status_code >= 400:
        doc.db_set("jofotara_status", "Failed")
        frappe.throw(_("JoFotara submit failed: {0}").format(resp.text))

    try:
        out = resp.json()
    except Exception:
        out = {"raw": resp.text}

    uuid = (out.get("uuid") or out.get("invoiceUUID") or "")
    qr   = (out.get("qrCode") or out.get("qr") or "")
    if uuid:
        doc.db_set("jofotara_uuid", uuid)
    if qr:
        doc.db_set("jofotara_qr", qr)
    doc.db_set("jofotara_status", "Sent")
    return out

@frappe.whitelist()
def cancel_invoice(name: str, reason: str = ""):
    doc = frappe.get_doc("Sales Invoice", name)
    settings = _get_settings()
    headers = auth_headers(settings)
    payload = {"invoiceNumber": doc.name, "uuid": doc.get("jofotara_uuid"), "reason": reason}
    resp = post(settings, settings.cancel_url, json=payload, headers=headers)
    if resp.status_code >= 400:
        frappe.throw(_("JoFotara cancel failed: {0}").format(resp.text))
    return resp.json()

@frappe.whitelist()
def query_status(name: str):
    doc = frappe.get_doc("Sales Invoice", name)
    settings = _get_settings()
    headers = auth_headers(settings)
    payload = {"invoiceNumber": doc.name, "uuid": doc.get("jofotara_uuid")}
    resp = post(settings, settings.query_url, json=payload, headers=headers)
    if resp.status_code >= 400:
        frappe.throw(_("JoFotara query failed: {0}").format(resp.text))
    return resp.json()

def on_submit_send(doc, method=None):
    try:
        settings = _get_settings()
    except Exception:
        return
    if not settings or not cint(settings.send_on_submit):
        return
    try:
        send_invoice(doc.name)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "JoFotara on_submit_send failed")
        try:
            doc.db_set("jofotara_status", "Failed")
        except Exception:
            pass
