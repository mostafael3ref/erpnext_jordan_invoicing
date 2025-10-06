import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

def after_migrate():
    if not frappe.db.exists("DocType", "Sales Invoice"):
        return
    fields = {
        "Sales Invoice": [
            dict(fieldname="jofotara_status", label="JoFatora Status",
                 fieldtype="Select", options="\nPending\nSubmitted\nError", insert_after="naming_series"),
            dict(fieldname="jofotara_uuid", label="JoFatora UUID", fieldtype="Data", read_only=1),
            dict(fieldname="jofotara_qr", label="JoFatora QR", fieldtype="Small Text", read_only=1),
        ]
    }
    create_custom_fields(fields, ignore_validate=True)
