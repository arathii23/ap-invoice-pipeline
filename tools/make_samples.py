"""Generates sample invoices 10 (multi-page) and 11 (different layout) and registers their POs."""
import json, os, fitz
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def page(doc): return doc.new_page(width=595, height=842)
def t(pg, x, y, s, size=10, bold=False): pg.insert_text((x, y), s, fontsize=size, fontname="hebo" if bold else "helv")
def row(pg, y, desc, qty, unit, amt):
    t(pg, 50, y, desc); t(pg, 300, y, qty); t(pg, 360, y, unit); t(pg, 460, y, amt)

# 10: Acme, two pages, items continue on page 2
d = fitz.open(); p1 = page(d)
t(p1, 50, 60, "Acme Industrial Supplies Pvt Ltd", 14, True); t(p1, 50, 78, "21 Okhla Phase II, New Delhi 110020")
t(p1, 50, 94, "GSTIN: 07AABCA1234F1Z5"); t(p1, 50, 120, "TAX INVOICE", 12, True)
t(p1, 50, 140, "Invoice No: INV-ACM-0447"); t(p1, 50, 154, "Invoice Date: 19 Nov 2026"); t(p1, 50, 168, "PO Reference: PO-2026-1007")
t(p1, 50, 190, "Bill To: Zenith Retail Pvt Ltd"); t(p1, 50, 204, "Plot 14, Sector 62, Noida, UP 201301")
t(p1, 50, 235, "Description", bold=True); t(p1, 300, 235, "Qty", bold=True); t(p1, 360, 235, "Unit Price", bold=True); t(p1, 460, 235, "Amount", bold=True)
row(p1, 255, "Hydraulic hose HH-40", "20", "1,200.00", "24,000.00"); row(p1, 272, "Pressure valve PV-7", "10", "3,500.00", "35,000.00")
t(p1, 50, 800, "Page 1 of 2 - continued overleaf")
p2 = page(d); t(p2, 50, 60, "Acme Industrial Supplies Pvt Ltd - Invoice INV-ACM-0447 (continued)", 9)
t(p2, 50, 100, "Description", bold=True); t(p2, 300, 100, "Qty", bold=True); t(p2, 360, 100, "Unit Price", bold=True); t(p2, 460, 100, "Amount", bold=True)
row(p2, 120, "Gasket set GS-3", "40", "250.00", "10,000.00")
t(p2, 50, 160, "Subtotal"); t(p2, 460, 160, "69,000.00"); t(p2, 50, 176, "GST @ 18%"); t(p2, 460, 176, "12,420.00")
t(p2, 50, 192, "Total Due", bold=True); t(p2, 460, 192, "81,420.00", bold=True)
t(p2, 50, 225, "Payment details", bold=True); t(p2, 50, 241, "Bank A/C No: 50200012345678   IFSC: HDFC0001234")
t(p2, 50, 800, "Page 2 of 2")
d.save(os.path.join(BASE, "invoices", "10_acme_multipage.pdf"))

# 11: Greenfield, different layout and labels
d = fitz.open(); p = page(d)
t(p, 50, 60, "TAX INVOICE", 12, True); t(p, 50, 80, "Greenfield Packaging Co", 14, True); t(p, 50, 96, "Kharkhoda Industrial Area, Sonipat 131402")
t(p, 50, 112, "GSTIN/UIN: 09AAGCG3456P1Z1"); t(p, 50, 140, "Bill No.: GF-91044"); t(p, 50, 154, "Dated: 19/11/2026"); t(p, 50, 168, "Purchase Order No.: PO-2026-1008")
t(p, 50, 190, "Billed To: Zenith Retail Pvt Ltd")
t(p, 50, 225, "Particulars", bold=True); t(p, 300, 225, "Qty", bold=True); t(p, 360, 225, "Rate", bold=True); t(p, 460, 225, "Amount", bold=True)
row(p, 245, "Corrugated carton 5-ply", "800", "Rs. 45.00", "Rs. 36,000.00")
t(p, 50, 290, "Taxable Value:"); t(p, 460, 290, "Rs. 36,000.00"); t(p, 50, 306, "CGST @ 9%:"); t(p, 460, 306, "Rs. 3,240.00")
t(p, 50, 322, "SGST @ 9%:"); t(p, 460, 322, "Rs. 3,240.00"); t(p, 50, 340, "Grand Total:", bold=True); t(p, 460, 340, "Rs. 42,480.00", bold=True)
t(p, 50, 380, "Bank Details", bold=True); t(p, 50, 396, "Account Number: 33120078901234"); t(p, 50, 410, "IFSC Code: SBIN0006789")
d.save(os.path.join(BASE, "invoices", "11_greenfield_alt_layout.pdf"))

pos = json.load(open(os.path.join(BASE, "data", "pos.json")))
pos["PO-2026-1007"] = {"vendor": "V001", "desc": "Hydraulic hoses, valves and gaskets", "net": 69000, "tax": 18, "billed": 0, "status": "open",
    "lines": [["Hydraulic hose HH-40", 20, 1200], ["Pressure valve PV-7", 10, 3500], ["Gasket set GS-3", 40, 250]]}
pos["PO-2026-1008"] = {"vendor": "V004", "desc": "Corrugated cartons December batch", "net": 36000, "tax": 18, "billed": 0, "status": "open",
    "lines": [["Corrugated carton 5-ply", 800, 45]]}
json.dump(pos, open(os.path.join(BASE, "data", "pos.json"), "w"), indent=2)
print("ok")
