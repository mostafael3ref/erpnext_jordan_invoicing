import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

def after_install():
    custom_fields = {
        "Sales Invoice": [
            dict(fieldname="jofotara_status", label="JoFotara Status", fieldtype="Select",
                 options="\nPending\nSent\nFailed", insert_after="naming_series"),
            dict(fieldname="jofotara_uuid", label="JoFotara UUID", fieldtype="Data",
                 insert_after="jofotara_status", read_only=1),
            dict(fieldname="jofotara_qr", label="JoFotara QR", fieldtype="Small Text",
                 insert_after="jofotara_uuid", read_only=1),
        ]
    }
    create_custom_fields(custom_fields, ignore_validate=True)
