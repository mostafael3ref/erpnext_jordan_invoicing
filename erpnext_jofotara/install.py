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
            fieldtype="Small Text",  # أو Long Text لو حابب
            read_only=1,
            no_copy=1,
            insert_after="jofotara_uuid",
        ),
        # اختياري للتجربة اليدوية أو مراجعة الـ XML المولّد
        # dict(
        #     fieldname="jofotara_xml",
        #     label="JoFotara UBL XML",
        #     fieldtype="Long Text",
        #     insert_after="jofotara_qr",
        # ),
    ]
}

def ensure_custom_fields():
    if not frappe.db.exists("DocType", "Sales Invoice"):
        return
    # تقدر تشيل الشرط وتستدعي مباشرةً لتحديث/إضافة الحقول دائمًا
    needed = ("jofotara_status", "jofotara_uuid", "jofotara_qr")
    missing = [
        fn for fn in needed
        if not frappe.db.exists("Custom Field", {"dt": "Sales Invoice", "fieldname": fn})
    ]
    if missing:
        create_custom_fields(_FIELDS, ignore_validate=True)
        frappe.clear_cache(doctype="Sales Invoice")

def after_install():
    ensure_custom_fields()

def after_migrate():
    ensure_custom_fields()
