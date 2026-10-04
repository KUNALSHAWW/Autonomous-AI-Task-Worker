"""Generates real PDF documents (invoices, policy, vendor forms) with fpdf2.

Three invoice layouts exist so the agent cannot rely on one fixed position
for the total or the due date.
"""
from __future__ import annotations

from datetime import date

from fpdf import FPDF

from .world import COMPANY, Invoice, Vendor


def _fmt_date(d: date, style: int) -> str:
    if style == 0:
        return d.strftime("%B %d, %Y")
    if style == 1:
        return d.strftime("%d %b %Y")
    return d.isoformat()


def _money(x: float, cur: str = "USD") -> str:
    sym = {"USD": "$", "EUR": "EUR ", "GBP": "GBP "}.get(cur, "")
    return f"{sym}{x:,.2f}"


def _pdf() -> FPDF:
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    return pdf


def invoice_pdf(inv: Invoice, vendor: Vendor) -> bytes:
    t = inv.template
    pdf = _pdf()
    if t == 0:
        pdf.set_font("Helvetica", "B", 18)
        pdf.cell(0, 10, vendor.name, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=9)
        pdf.cell(0, 5, f"billing@{vendor.domain}  |  www.{vendor.domain}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, "INVOICE", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        for k, v in [
            ("Invoice No.", inv.number),
            ("Invoice Date", _fmt_date(inv.invoice_date, 0)),
            ("Due Date", _fmt_date(inv.due_date, 0)),
            ("Bill To", COMPANY + ", Accounts Payable"),
        ]:
            pdf.cell(40, 6, k + ":")
            pdf.cell(0, 6, v, new_x="LMARGIN", new_y="NEXT")
    elif t == 1:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(100, 8, "Tax Invoice")
        pdf.cell(0, 8, vendor.name, align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=9)
        pdf.cell(0, 5, f"Customer: {COMPANY}", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, f"Reference: {inv.number}    Issued: {_fmt_date(inv.invoice_date, 1)}", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, f"Payment terms: Net {(inv.due_date - inv.invoice_date).days} days, "
                       f"payable by {_fmt_date(inv.due_date, 1)}", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 9, f"{vendor.name} - Statement of Charges", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        pdf.multi_cell(0, 6, f"Document number: {inv.number}\nDate of issue: {_fmt_date(inv.invoice_date, 2)}\n"
                             f"Please remit payment no later than {_fmt_date(inv.due_date, 2)}.\n"
                             f"Billed to: {COMPANY}")
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(95, 7, "Description", border=1)
    pdf.cell(20, 7, "Qty", border=1, align="R")
    pdf.cell(35, 7, "Unit price", border=1, align="R")
    pdf.cell(35, 7, "Line total", border=1, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    for desc, q, p in inv.lines:
        pdf.cell(95, 7, desc, border=1)
        pdf.cell(20, 7, str(q), border=1, align="R")
        pdf.cell(35, 7, _money(p), border=1, align="R")
        pdf.cell(35, 7, _money(q * p), border=1, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    rows = [("Subtotal", inv.subtotal), (f"Tax ({inv.tax_rate * 100:.0f}%)", inv.tax)]
    total_label = ["Total Due", "Amount payable", "Balance due"][t]
    rows.append((total_label, inv.total))
    for label, val in rows:
        bold = label == total_label
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        pdf.cell(150, 7, label, align="R")
        pdf.cell(35, 7, _money(val, inv.currency), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", size=8)
    pdf.multi_cell(0, 4, f"Pay by bank transfer to account {vendor.bank}. Quote the invoice number with your payment.")
    return bytes(pdf.output())


def text_pdf(title: str, paragraphs: list[str]) -> bytes:
    pdf = _pdf()
    pdf.set_font("Helvetica", "B", 15)
    pdf.multi_cell(0, 8, title)
    pdf.ln(3)
    pdf.set_font("Helvetica", size=10)
    for p in paragraphs:
        pdf.multi_cell(0, 5.5, p)
        pdf.ln(2)
    return bytes(pdf.output())
