"""Certificate rendering. One predefined template, drawn with ReportLab."""
import os
import tempfile
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

GOLD = colors.HexColor("#B8860B")
NAVY = colors.HexColor("#1F2A44")
GREY = colors.HexColor("#666666")


def _fit_font_size(text, font, max_size, max_width, min_size=12):
    """Shrink the font until `text` fits in `max_width` (long names)."""
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _draw_template(c, *, recipient_name, title, issuer, issue_date, certificate_id):
    width, height = landscape(A4)
    cx = width / 2
    text_width = width - 160

    # Double border
    c.setStrokeColor(GOLD)
    c.setLineWidth(6)
    c.rect(25, 25, width - 50, height - 50)
    c.setLineWidth(1.5)
    c.rect(38, 38, width - 76, height - 76)

    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 44)
    c.drawCentredString(cx, height - 130, "CERTIFICATE")
    c.setFont("Helvetica", 16)
    c.setFillColor(GOLD)
    c.drawCentredString(cx, height - 160, "OF PARTICIPATION")

    c.setFillColor(GREY)
    c.setFont("Helvetica", 15)
    c.drawCentredString(cx, height - 215, "This is to certify that")

    # Recipient name + underline
    c.setFillColor(NAVY)
    size = _fit_font_size(recipient_name, "Helvetica-Bold", 40, text_width)
    c.setFont("Helvetica-Bold", size)
    c.drawCentredString(cx, height - 270, recipient_name)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(cx - text_width / 2, height - 282, cx + text_width / 2, height - 282)

    c.setFillColor(GREY)
    c.setFont("Helvetica", 15)
    c.drawCentredString(cx, height - 318, "has successfully participated in")

    c.setFillColor(NAVY)
    size = _fit_font_size(title, "Helvetica-Bold", 26, text_width)
    c.setFont("Helvetica-Bold", size)
    c.drawCentredString(cx, height - 360, title)

    # Footer: date (left), issuer (right)
    pretty_date = date.fromisoformat(issue_date).strftime("%d %B %Y")
    c.setStrokeColor(GREY)
    c.setLineWidth(0.8)
    c.line(110, 120, 290, 120)
    c.line(width - 290, 120, width - 110, 120)
    c.setFillColor(NAVY)
    c.setFont("Helvetica", 13)
    c.drawCentredString(200, 126, pretty_date)
    c.drawCentredString(width - 200, 126, issuer)
    c.setFillColor(GREY)
    c.setFont("Helvetica", 10)
    c.drawCentredString(200, 104, "Date")
    c.drawCentredString(width - 200, 104, "Issued by")

    c.setFont("Helvetica", 8)
    c.drawCentredString(cx, 55, f"Certificate ID: {certificate_id}")


def generate_certificate(output_path, *, recipient_name, title, issuer,
                         issue_date, certificate_id):
    """Render one certificate PDF to `output_path`.

    Written to a temp file and renamed, so a crash mid-render never leaves a
    truncated PDF at the final path.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".tmp", dir=output_path.parent)
    os.close(fd)
    try:
        c = canvas.Canvas(tmp_name, pagesize=landscape(A4))
        c.setTitle(f"Certificate - {recipient_name}")
        c.setAuthor(issuer)
        _draw_template(c, recipient_name=recipient_name, title=title,
                       issuer=issuer, issue_date=issue_date,
                       certificate_id=certificate_id)
        c.showPage()
        c.save()
        os.replace(tmp_name, output_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
