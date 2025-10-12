from . import __version__ as app_version

app_name = "erpnext_jofotara"
app_title = "ERPNext JoFotara"
app_publisher = "Mustafa Al-Areef"
app_description = "Integration with Jordan JoFotara"
app_email = "dev@example.com"
app_license = "MIT"

required_apps = ["erpnext"]
fixtures = []
doctype_js = {}

doc_events = {
    "Sales Invoice": {
        "on_submit": "erpnext_jofotara.api.invoices.on_submit_sales_invoice"
    }
}

after_migrate = ["erpnext_jofotara.install.after_migrate"]
after_install = "erpnext_jofotara.install.after_install"

scheduler_events = {
    "hourly": [
        "erpnext_jofotara.api.invoices.retry_pending_jobs"
    ]
}
