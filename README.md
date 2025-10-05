# ERPNext JoFotara Connector (Jordan E-Invoicing)

An ERPNext app to integrate Sales Invoices with Jordan's National E-Invoicing (JoFatora).

## Features
- Configurable auth: OAuth2 (Client Credentials) or Device User + Secret.
- Configurable endpoints (Token / Submit / Cancel / Query).
- Sends Base64-encoded XML (UPL 2.1 style) or JSON (toggle), returns UUID & QR if provided.
- Auto-send on Submit (optional).
- Custom fields on Sales Invoice: jofotara_status, jofotara_uuid, jofotara_qr.

## Install
```bash
bench get-app erpnext_jofotara https://github.com/mostafael3ref/erpnext_jofotara.git
bench --site <your-site> install-app erpnext_jofotara
