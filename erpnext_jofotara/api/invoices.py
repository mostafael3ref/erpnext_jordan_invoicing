import json
import frappe
from frappe import _
from frappe.utils import now

# إعداد بسيط للتفعيل/التعطيل (تقدر لاحقًا تعمل Doctype Settings)
def is_auto_send_enabled() -> bool:
    # مستقبلاً: اقرأ من Doctype "JoFatora Settings"
    return True

def on_submit_send(doc, method=None):
    """Hook: يُستدعى عند اعتماد Sales Invoice"""
    try:
        if not is_auto_send_enabled():
            return

        # ابني الحمولة (Payload) من الفاتورة
        payload = build_jofotara_payload(doc)

        # أرسلها (استبدل هذا بدالة حقيقية تتصل بـ JoFatora)
        resp = submit_to_jofotara(payload)

        # عالج الرد وخزّنه
        handle_submit_response(doc, resp)

        frappe.msgprint(_("JoFatora: Invoice submitted successfully"), alert=1, indicator="green")

    except Exception as e:
        # سجّل الخطأ وحدّث الحالة
        doc.db_set("jofotara_status", "Error")
        frappe.log_error(frappe.get_traceback(), "JoFatora Submit Error")
        frappe.throw(_("JoFatora submission failed: {0}").format(str(e)))

def build_jofotara_payload(doc):
    """حوّل Sales Invoice إلى JSON أو XML حسب متطلبات JoFatora.
       هنا مثال JSON مبسّط؛ عدّله لتطابق مخطط النظام الأردني."""
    items = []
    for it in doc.items:
        items.append({
            "item_code": it.item_code,
            "item_name": it.item_name,
            "qty": float(it.qty),
            "rate": float(it.rate),
            "amount": float(it.amount),
            "uom": it.uom,
        })

    payload = {
        "invoice_no": doc.name,
        "posting_date": str(doc.posting_date),
        "customer": doc.customer,
        "company": doc.company,
        "currency": doc.currency,
        "total": float(doc.total),
        "grand_total": float(doc.grand_total),
        "taxes": [
            {
                "description": tx.description,
                "rate": float(tx.rate or 0),
                "amount": float(tx.tax_amount or 0),
            } for tx in (doc.taxes or [])
        ],
        "items": items,
    }
    return payload

def submit_to_jofotara(payload: dict) -> dict:
    """استبدل هذا بتنفيذ حقيقي:
       - قراءة الإعدادات (URLs, client_id, client_secret)
       - إنشاء توكن إن لزم
       - إرسال الطلب عبر requests
       - إرجاع الرد كـ dict
    """
    # مثال محاكاة
    return {
        "status": "success",
        "uuid": "JOFOTARA-UUID-123",
        "qr": "BASE64-QR-STRING",
        "timestamp": now(),
        "raw": payload,  # لأغراض الاختبار فقط
    }

def handle_submit_response(doc, resp: dict):
    """تحديث الحقول وتسجيل السجل (Log)"""
    status = "Submitted" if resp.get("status") == "success" else "Error"
    uuid = resp.get("uuid")
    qr = resp.get("qr")

    doc.db_set("jofotara_status", status)
    if uuid:
        doc.db_set("jofotara_uuid", uuid)
    if qr:
        doc.db_set("jofotara_qr", qr)

    # اختياري: حفظ الرد الخام في جدولة Logs (Child Table) لو أضفتها لاحقًا
    # أو احفظه في Communication:
    comm = frappe.get_doc({
        "doctype": "Communication",
        "communication_type": "Automated Message",
        "subject": f"JoFatora Submit • {doc.name}",
        "content": f"<pre>{frappe.as_json(resp, indent=2)}</pre>",
        "reference_doctype": "Sales Invoice",
        "reference_name": doc.name,
    })
    comm.insert(ignore_permissions=True)

@frappe.whitelist()
def retry_pending_jobs():
    """مثال: تعيد محاولة أي فواتير حالتها Pending (لو اعتمدت هذا السيناريو)"""
    # هنا ممكن تدور على فواتير Pending وترسلها من جديد
    pass
