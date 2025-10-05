import frappe
from lxml import etree
import base64

def invoice_to_json(doc, settings):
    items = []
    tax_rate = _guess_tax_rate(doc)
    for it in doc.items:
        tax_amount = float(it.amount) * (tax_rate/100.0)
        items.append({
            "description": it.item_name,
            "quantity": float(it.qty),
            "unitPrice": float(it.rate),
            "taxRate": tax_rate,
            "taxAmount": round(tax_amount, 3),
        })
    return {
        "activityNumber": settings.activity_number,
        "invoice": {
            "invoiceNumber": doc.name,
            "invoiceDate": str(doc.posting_date),
            "customerName": doc.customer_name,
            "customerTaxNumber": getattr(doc, "tax_id", "") or "",
            "totalAmount": float(doc.grand_total),
            "taxAmount": float(doc.total_taxes_and_charges or 0),
            "invoiceLines": items
        }
    }

def _guess_tax_rate(doc):
    # بسيطة: خذ أول نسبة ضرائب من جدول الضرائب
    try:
        for t in doc.taxes or []:
            if t.rate:
                return float(t.rate)
    except Exception:
        pass
    return 0.0

def invoice_to_upl21_xml_base64(doc, settings):
    """
    يبني XML بسيط بنمط UPL 2.1 (تبسيط مبدئي) ثم يعيده Base64
    عدّل التاجات بحسب المخطط النهائي الذي تتسلمه من ISTD/JoFotara.
    """
    tax_rate = _guess_tax_rate(doc)
    root = etree.Element("Invoice")
    etree.SubElement(root, "ActivityNumber").text = settings.activity_number
    etree.SubElement(root, "InvoiceNumber").text = doc.name
    etree.SubElement(root, "InvoiceDate").text = str(doc.posting_date)
    etree.SubElement(root, "CustomerName").text = doc.customer_name
    etree.SubElement(root, "CustomerTaxNumber").text = getattr(doc, "tax_id", "") or ""
    lines = etree.SubElement(root, "Lines")
    for it in doc.items:
        line = etree.SubElement(lines, "Line")
        etree.SubElement(line, "Description").text = it.item_name
        etree.SubElement(line, "Quantity").text = str(float(it.qty))
        etree.SubElement(line, "UnitPrice").text = str(float(it.rate))
        etree.SubElement(line, "TaxRate").text = str(tax_rate)
    etree.SubElement(root, "TaxAmount").text = str(float(doc.total_taxes_and_charges or 0))
    etree.SubElement(root, "TotalAmount").text = str(float(doc.grand_total))

    xml_bytes = etree.tostring(root, xml_declaration=True, encoding="utf-8")
    return base64.b64encode(xml_bytes).decode()
