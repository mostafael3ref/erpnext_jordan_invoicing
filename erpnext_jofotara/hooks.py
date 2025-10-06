from . import __version__ as app_version

app_name = "erpnext_jofotara"
app_title = "ERPNext JoFotara"
app_publisher = "Mustafa Al-Areef"
app_description = "Integration with Jordan JoFotara"
app_email = "dev@example.com"
app_license = "MIT"

# مهم: يضمن وجود ERPNext قبل تثبيت الاب (يتفادى أخطاء Sales Invoice)
required_apps = ["erpnext"]

# لا نستخدم Fixtures الآن لتفادي كسر التثبيت؛ بننشئ الحقول في after_migrate
fixtures = []

# JS لاحقًا إن احتجت
doctype_js = {}

# إرسال تلقائي عند اعتماد الفاتورة (لو مفعّل في الإعدادات داخل الدالة)
doc_events = {
    "Sales Invoice": {
        "on_submit": "erpnext_jofotara.api.invoices.on_submit_send"
    }
}

# الأفضل بعد الهجرة بدل after_install
after_migrate = ["erpnext_jofotara.install.after_migrate"]

# (اختياري) جدول مهام لإعادة المحاولة أو مزامنة دورية
scheduler_events = {
    "hourly": [
        "erpnext_jofotara.api.invoices.retry_pending_jobs"
    ]
}
