from . import __version__ as app_version

app_name = "jofotara"
app_title = "ERPNext JoFotara"
app_publisher = "Mustafa Al-Areef"
app_description = "Integration with Jordan JoFotara"
app_email = "dev@example.com"
app_license = "MIT"

# Include DocType JavaScript if needed later
doctype_js = {}

# Fixtures: none (we’ll create custom fields in install.py)
fixtures = []

# Doc Events: auto-send on submit (configurable; check setting inside handler)
doc_events = {
    "Sales Invoice": {
        "on_submit": "erpnext_jofotara.api.invoices.on_submit_send"
    }
}

after_install = "erpnext_jofotara.install.after_install"
