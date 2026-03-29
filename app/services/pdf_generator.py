"""Generate professional PDF invoices and quotes using fpdf2."""

from __future__ import annotations

import io
from typing import Any

from fpdf import FPDF

CURRENCY_SYMBOLS = {
    "GBP": "£", "USD": "$", "EUR": "€",
    "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "ZAR": "R",
}


def _cs(currency: str) -> str:
    return CURRENCY_SYMBOLS.get(currency, currency + " ")


class _InvoicePDF(FPDF):
    """Custom PDF with header/footer styling."""

    def __init__(self, title: str, biz_name: str) -> None:
        super().__init__()
        self._doc_title = title
        self._biz_name = biz_name

    def header(self) -> None:
        self.set_font("Helvetica", "B", 18)
        self.set_text_color(22, 163, 74)  # green-600
        self.cell(0, 10, self._biz_name, new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "B", 28)
        self.set_text_color(55, 65, 81)  # gray-700
        self.cell(0, 14, self._doc_title, new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-20)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(156, 163, 175)  # gray-400
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def _add_info_block(pdf: _InvoicePDF, label: str, lines: list[str]) -> None:
    """Add a labelled block of text (e.g. From / To)."""
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(107, 114, 128)
    pdf.cell(0, 5, label.upper(), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(55, 65, 81)
    for line in lines:
        if line:
            pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


def generate_invoice_pdf(
    business: dict[str, Any],
    customer: dict[str, Any] | None,
    invoice: dict[str, Any],
    line_items: list[dict[str, Any]],
) -> bytes:
    """Return PDF bytes for an invoice."""
    currency = invoice.get("currency", "GBP")
    sym = _cs(currency)
    tax_label = business.get("tax_label", "VAT")

    pdf = _InvoicePDF("INVOICE", business.get("business_name", ""))
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.add_page()

    # ── Invoice number & dates row ────────────────
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(55, 65, 81)
    pdf.cell(95, 6, f"Invoice #: {invoice.get('invoice_number', '')}")
    pdf.cell(95, 6, f"Date: {invoice.get('created_at', '')[:10]}", align="R", new_x="LMARGIN", new_y="NEXT")
    if invoice.get("due_date"):
        pdf.cell(95, 6, f"Status: {invoice.get('status', 'draft').upper()}")
        pdf.cell(95, 6, f"Due: {invoice['due_date'][:10]}", align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 6, f"Status: {invoice.get('status', 'draft').upper()}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ── From / To ─────────────────────────────────
    from_lines = [
        business.get("business_name", ""),
        business.get("business_address", ""),
        ", ".join(filter(None, [business.get("business_city", ""), business.get("business_postcode", "")])),
        f"{tax_label} No: {business.get('tax_number', '')}" if business.get("tax_number") else "",
        business.get("email", ""),
        business.get("phone_number", ""),
    ]

    to_lines = []
    if customer:
        to_lines = [
            customer.get("name", ""),
            customer.get("phone_number", ""),
        ]

    y_before = pdf.get_y()
    _add_info_block(pdf, "From", from_lines)
    y_after_from = pdf.get_y()

    if to_lines:
        pdf.set_y(y_before)
        pdf.set_x(110)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(0, 5, "TO", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(110)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(55, 65, 81)
        for line in to_lines:
            if line:
                pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
                pdf.set_x(110)
        pdf.ln(3)
    pdf.set_y(max(y_after_from, pdf.get_y()))
    pdf.ln(4)

    # ── Line items table ──────────────────────────
    pdf.set_fill_color(243, 244, 246)  # gray-100
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(75, 85, 99)
    pdf.cell(90, 8, "  Description", border=0, fill=True)
    pdf.cell(25, 8, "Qty", border=0, fill=True, align="C")
    pdf.cell(35, 8, "Unit Price", border=0, fill=True, align="R")
    pdf.cell(40, 8, "Amount", border=0, fill=True, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(55, 65, 81)
    for item in line_items:
        pdf.cell(90, 7, f"  {item.get('description', '')}")
        pdf.cell(25, 7, str(item.get("quantity", 1)), align="C")
        pdf.cell(35, 7, f"{sym}{item.get('unit_price', 0):.2f}", align="R")
        pdf.cell(40, 7, f"{sym}{item.get('total', 0):.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)

    # ── Totals ────────────────────────────────────
    pdf.set_x(120)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(40, 7, "Subtotal:")
    pdf.cell(30, 7, f"{sym}{invoice.get('subtotal', 0):.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    tax_rate = invoice.get("tax_rate", 0)
    tax_amount = invoice.get("tax_amount", 0)
    if tax_rate > 0:
        pdf.set_x(120)
        pdf.cell(40, 7, f"{tax_label} ({tax_rate}%):")
        pdf.cell(30, 7, f"{sym}{tax_amount:.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(120)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(40, 9, "Total:")
    pdf.cell(30, 9, f"{sym}{invoice.get('total', 0):.2f}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ── Payment terms ─────────────────────────────
    terms = invoice.get("payment_terms", "")
    if terms:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(0, 5, "PAYMENT TERMS", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(55, 65, 81)
        pdf.multi_cell(0, 5, terms)
        pdf.ln(3)

    # ── Bank details ──────────────────────────────
    bank = business.get("bank_details", "")
    if bank:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(0, 5, "BANK DETAILS", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(55, 65, 81)
        pdf.multi_cell(0, 5, bank)
        pdf.ln(3)

    # ── Accepted payment methods ──────────────────
    methods_str = business.get("accepted_payment_methods", "")
    if methods_str:
        method_labels = {
            "cash": "Cash", "bank_transfer": "Bank Transfer", "card": "Card",
            "paypal": "PayPal", "stripe": "Stripe", "other": "Other",
        }
        methods = [method_labels.get(m.strip(), m.strip()) for m in methods_str.split(",") if m.strip()]
        if methods:
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(107, 114, 128)
            pdf.cell(0, 5, "ACCEPTED PAYMENT METHODS", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(55, 65, 81)
            pdf.cell(0, 5, ", ".join(methods), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)

    # ── Payment link ──────────────────────────────
    pay_link = business.get("payment_link", "")
    if pay_link:
        # Build amount-aware link for PayPal.me style
        total = invoice.get("total", 0)
        if "paypal.me/" in pay_link.lower() and total > 0:
            link = pay_link.rstrip("/") + f"/{total:.2f}"
        else:
            link = pay_link
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(0, 5, "PAY ONLINE", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(22, 163, 74)  # green-600
        pdf.cell(0, 5, link, new_x="LMARGIN", new_y="NEXT", link=link)
        pdf.set_text_color(55, 65, 81)
        pdf.ln(3)

    # ── Notes ─────────────────────────────────────
    notes = invoice.get("notes", "")
    if notes:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(0, 5, "NOTES", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(55, 65, 81)
        pdf.multi_cell(0, 5, notes)

    return bytes(pdf.output())


def generate_quote_pdf(
    business: dict[str, Any],
    customer: dict[str, Any] | None,
    quote: dict[str, Any],
    line_items: list[dict[str, Any]],
) -> bytes:
    """Return PDF bytes for a quote."""
    currency = quote.get("currency", "GBP")
    sym = _cs(currency)
    tax_label = business.get("tax_label", "VAT")

    pdf = _InvoicePDF("QUOTE", business.get("business_name", ""))
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.add_page()

    # ── Quote number & dates ──────────────────────
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(55, 65, 81)
    pdf.cell(95, 6, f"Quote #: {quote.get('quote_number', '')}")
    pdf.cell(95, 6, f"Date: {quote.get('created_at', '')[:10]}", align="R", new_x="LMARGIN", new_y="NEXT")
    if quote.get("valid_until"):
        pdf.cell(95, 6, f"Status: {quote.get('status', 'draft').upper()}")
        pdf.cell(95, 6, f"Valid until: {quote['valid_until'][:10]}", align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 6, f"Status: {quote.get('status', 'draft').upper()}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ── From / To ─────────────────────────────────
    from_lines = [
        business.get("business_name", ""),
        business.get("business_address", ""),
        ", ".join(filter(None, [business.get("business_city", ""), business.get("business_postcode", "")])),
        f"{tax_label} No: {business.get('tax_number', '')}" if business.get("tax_number") else "",
        business.get("email", ""),
        business.get("phone_number", ""),
    ]

    to_lines = []
    if customer:
        to_lines = [customer.get("name", ""), customer.get("phone_number", "")]

    y_before = pdf.get_y()
    _add_info_block(pdf, "From", from_lines)
    y_after_from = pdf.get_y()

    if to_lines:
        pdf.set_y(y_before)
        pdf.set_x(110)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(0, 5, "TO", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(110)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(55, 65, 81)
        for line in to_lines:
            if line:
                pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
                pdf.set_x(110)
        pdf.ln(3)
    pdf.set_y(max(y_after_from, pdf.get_y()))
    pdf.ln(4)

    # ── Line items table ──────────────────────────
    pdf.set_fill_color(243, 244, 246)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(75, 85, 99)
    pdf.cell(90, 8, "  Description", border=0, fill=True)
    pdf.cell(25, 8, "Qty", border=0, fill=True, align="C")
    pdf.cell(35, 8, "Unit Price", border=0, fill=True, align="R")
    pdf.cell(40, 8, "Amount", border=0, fill=True, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(55, 65, 81)
    for item in line_items:
        pdf.cell(90, 7, f"  {item.get('description', '')}")
        pdf.cell(25, 7, str(item.get("quantity", 1)), align="C")
        pdf.cell(35, 7, f"{sym}{item.get('unit_price', 0):.2f}", align="R")
        pdf.cell(40, 7, f"{sym}{item.get('total', 0):.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)

    # ── Totals ────────────────────────────────────
    pdf.set_x(120)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(40, 7, "Subtotal:")
    pdf.cell(30, 7, f"{sym}{quote.get('subtotal', 0):.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    tax_rate = quote.get("tax_rate", 0)
    tax_amount = quote.get("tax_amount", 0)
    if tax_rate > 0:
        pdf.set_x(120)
        pdf.cell(40, 7, f"{tax_label} ({tax_rate}%):")
        pdf.cell(30, 7, f"{sym}{tax_amount:.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(120)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(40, 9, "Total:")
    pdf.cell(30, 9, f"{sym}{quote.get('total', 0):.2f}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ── Valid until ───────────────────────────────
    if quote.get("valid_until"):
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(0, 5, "VALID UNTIL", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(55, 65, 81)
        pdf.cell(0, 5, quote["valid_until"][:10], new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    # ── Notes ─────────────────────────────────────
    notes = quote.get("notes", "")
    if notes:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(0, 5, "NOTES", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(55, 65, 81)
        pdf.multi_cell(0, 5, notes)

    return bytes(pdf.output())
