# erpnext_jofotara/install.py
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

_FIELDS = {
    "Sales Invoice": [
        dict(
            fieldname="jofotara_status",
            label="JoFotara Status",
            fieldtype="Select",
            options="\nPending\nSubmitted\nError",
            default="Pending",
            read_only=1,
            no_copy=1,
            in_list_view=1,
            insert_after="naming_series",
        ),
        dict(
            fieldname="jofotara_uuid",
            label="JoFotara UUID",
            fieldtype="Data",
            read_only=1,
            no_copy=1,
            insert_after="jofotara_status",
        ),
        dict(
            fieldname="jofotara_qr",
            label="JoFotara QR",
            fieldtype="Small Text",
            read_only=1,
            no_copy=1,
            insert_after="jofotara_uuid",
        ),
        dict(
            fieldname="jofotara_qr_image",
            label="JoFotara QR Image",
            fieldtype="Attach Image",
            insert_after="jofotara_qr",
            allow_on_submit=1,
        ),
        # (اختياري)
        # dict(
        #     fieldname="jofotara_xml",
        #     label="JoFotara UBL XML",
        #     fieldtype="Long Text",
        #     insert_after="jofotara_qr",
        # ),
    ]
}

def ensure_custom_fields():
    """Create/Update custom fields idempotently every time."""
    if not frappe.db.exists("DocType", "Sales Invoice"):
        return
    # important: update=True يحدّث الحقول لو كانت موجودة
    create_custom_fields(_FIELDS, ignore_validate=True, update=True)
    frappe.clear_cache(doctype="Sales Invoice")

def after_install():
    ensure_custom_fields()

def after_migrate():
    ensure_custom_fields()
