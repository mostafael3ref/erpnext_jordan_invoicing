from frappe import _

def get_data():
    return [
        {
            "label": _("JoFotara"),
            "items": [
                {
                    "type": "doctype",
                    "name": "JoFotara Settings",
                    "label": _("JoFotara Settings"),
                    "description": _("Configure JoFotara credentials and endpoints"),
                    "onboard": 1,
                },
            ],
        }
    ]
