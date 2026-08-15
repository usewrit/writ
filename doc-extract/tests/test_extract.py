"""doc-extract engine + router tests.

Each test importorskips its heavy dependency so the suite still runs (skipping
what's absent) in a minimal env. Run in the service venv for full coverage:
    cd doc-extract && pip install -r requirements.txt && pytest -q

That convenience has a sharp edge: with the deps missing, every PDF/OCR/office
test skips and the suite still reports green — which is exactly what happened,
so the real extraction paths went unexercised. CI installs requirements.txt and
sets DOC_EXTRACT_REQUIRE_DEPS=1, which turns a missing dependency from a skip
into a failure (see test_heavy_dependencies_are_installed below).
"""
import io
import json
import os

import pytest

import extractor
from extractor import sniff_kind


# --- dependency guard -------------------------------------------------------

# (name, import target) for every dependency that gates a real extraction path.
# Keep this in step with requirements.txt: the guard's whole job is to convert a
# missing dependency into a failure rather than a skipped test, so a name that
# has drifted out of the requirements turns it into a false alarm and one that is
# missing from here re-opens the silent-skip hole it exists to close.
_HEAVY_DEPS = [
    ("pypdfium2", "pypdfium2"),
    ("pdfplumber", "pdfplumber"),
    ("pypdf", "pypdf"),
    ("rapidocr-onnxruntime", "rapidocr_onnxruntime"),
    ("Pillow", "PIL.Image"),
    ("python-docx", "docx"),
    ("openpyxl", "openpyxl"),
    ("python-pptx", "pptx"),
]


@pytest.mark.skipif(
    os.getenv("DOC_EXTRACT_REQUIRE_DEPS", "") not in ("1", "true", "yes"),
    reason="set DOC_EXTRACT_REQUIRE_DEPS=1 to require the full extraction stack",
)
def test_heavy_dependencies_are_installed():
    """Fail loudly when a dependency is missing instead of skipping half the suite."""
    import importlib

    missing = []
    for dist, module in _HEAVY_DEPS:
        try:
            importlib.import_module(module)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{dist} ({module}: {e})")
    assert not missing, (
        "these dependencies gate real extraction paths and are absent, so their "
        "tests would silently skip: " + ", ".join(missing)
    )


# --- sniffing ---------------------------------------------------------------

def test_sniff_pdf_by_magic():
    assert sniff_kind("application/octet-stream", b"%PDF-1.7\n...") == "pdf"


def test_sniff_png_by_magic():
    assert sniff_kind("", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "image"


def test_sniff_json_by_content_type():
    assert sniff_kind("application/json; charset=utf-8", b'{"a":1}') == "json"


def test_sniff_json_by_text_body():
    assert sniff_kind("text/plain", b'   [\n  {"x":1}\n]') == "json"


def test_sniff_csv_by_content_type():
    assert sniff_kind("text/csv", b"a,b\n1,2\n") == "csv"


def test_sniff_url_suffix_fallback():
    assert sniff_kind("application/octet-stream", b"\x00\x01", "https://x/report.pdf") == "pdf"


def test_sniff_docx_content_type():
    ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    # PK header but rely on content-type to disambiguate.
    assert sniff_kind(ct, b"PK\x03\x04rest") == "docx"


# --- structured (no heavy deps) --------------------------------------------

def test_json_list_becomes_records():
    body = json.dumps([{"name": "a", "n": 1}, {"name": "b", "n": 2}]).encode()
    res = extractor.extract(body, "application/json", "https://x/data.json")
    assert res["kind"] == "json"
    assert res["content_kind"] == "json"
    assert res["records"] == [{"name": "a", "n": 1}, {"name": "b", "n": 2}]
    assert "name" in res["markdown"]
    assert res["ocr"] is None  # never OCR structured data


def test_json_dict_with_row_list():
    body = json.dumps({"items": [{"id": 1}, {"id": 2}], "meta": "x"}).encode()
    res = extractor.extract(body, "application/json", "")
    assert res["records"] == [{"id": 1}, {"id": 2}]


def test_csv_becomes_records():
    body = b"name,age\nAlice,30\nBob,25\n"
    res = extractor.extract(body, "text/csv", "https://x/people.csv")
    assert res["content_kind"] == "csv"
    assert res["records"] == [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]
    assert res["ocr"] is None


def test_unknown_text_passthrough():
    res = extractor.extract(b"just some text", "text/plain", "")
    assert res["text"] == "just some text"
    assert res["ocr"] is None


def test_html_content_type_gets_clean_markdown():
    # An HTML body that legitimately reaches the sidecar (declared text/html)
    # must be cleaned to frontmatter markdown, not passed through as raw markup.
    body = (b"<html><head><title>Docs</title></head><body>"
            b"<header>site chrome</header>"
            b"<main><h1>Docs</h1><p>The body content here.</p></main>"
            b"</body></html>")
    res = extractor.extract(body, "text/html; charset=utf-8", "https://x/docs")
    assert res["kind"] == "html"
    assert res["content_kind"] == "html"
    assert res["markdown"].startswith("---")
    assert "The body content here." in res["markdown"]
    assert "site chrome" not in res["markdown"]        # header chrome stripped
    assert res["ocr"] is None


# --- PDF (text layer, no OCR) ----------------------------------------------

def _build_pdf(objects: list[bytes]) -> bytes:
    """Assemble numbered PDF objects into a valid file with a real xref table.

    Written by hand rather than with a PDF library on purpose. Every library
    that can WRITE a PDF is either a heavy dependency or AGPL (PyMuPDF, which
    this service deliberately no longer uses) — and a test fixture that needs a
    dependency the service does not have is a test that silently skips. These
    helpers keep the PDF paths, which are the core of the service, actually
    exercised on a bare checkout.
    """
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref_at,
    )
    return out


def _make_text_pdf(text: str) -> bytes:
    """A one-page PDF carrying `text` as a real, extractable text layer."""
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    stream = b"BT /F1 14 Tf 72 720 Td (" + escaped.encode("latin-1") + b") Tj ET"
    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ])


def _make_image_only_pdf(png: bytes, width: int, height: int) -> bytes:
    """A one-page PDF whose entire content is an image — NO text layer at all.

    The image is embedded as a DCTDecode (JPEG) or FlateDecode XObject depending
    on what `png` actually is; callers pass PNG bytes, which are re-encoded to
    raw RGB + Flate here because PDF has no native PNG filter.
    """
    from PIL import Image  # the OCR path already requires Pillow
    import io
    import zlib

    img = Image.open(io.BytesIO(png)).convert("RGB")
    raw = zlib.compress(img.tobytes())
    stream = b"q %d 0 0 %d 0 0 cm /Im0 Do Q" % (width, height)
    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents 4 0 R "
        b"/Resources << /XObject << /Im0 5 0 R >> >> >>" % (width, height),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        b"/Length %d >>\nstream\n" % (img.width, img.height, len(raw)) + raw + b"\nendstream",
    ])


def test_pdf_text_layer_no_ocr():
    data = _make_text_pdf("Hello crawled document world")
    res = extractor.extract(data, "application/pdf", "https://x/doc.pdf", ocr_mode="auto")
    assert res["kind"] == "pdf"
    assert res["content_kind"] == "pdf"          # has a text layer → not OCR
    assert res["meta"]["text_layer"] is True
    assert res["ocr"] is None                    # OCR must not fire on a text PDF
    assert "Hello crawled" in (res["markdown"] + res["text"])


def test_pdf_ocr_off_still_returns_text_layer():
    data = _make_text_pdf("Text layer present")
    res = extractor.extract(data, "application/pdf", "", ocr_mode="off")
    assert "Text layer present" in res["text"]


# --- PDF content-type mismatch (.pdf URL that returns HTML/text) ------------

def test_pdf_url_returning_html_is_detected():
    # A soft-404 / login wall served under a .pdf URL. The engine must
    # short-circuit before ever trying to parse it as a PDF.
    body = b"<!DOCTYPE html>\n<html><head><title>Sign in</title></head>" \
           b"<body>Please log in to view this document.</body></html>"
    res = extractor.extract(body, "application/pdf", "https://x/report.pdf")
    assert res["kind"] == "html"
    assert res["content_kind"] == "html"
    assert "Please log in" in res["text"]
    assert res["ocr"] is None
    assert res["meta"].get("declared_pdf") is True


def test_pdf_url_html_gets_clean_markdown():
    # The mislabeled HTML must come back as clean, frontmatter-headed markdown
    # (chrome stripped) — not raw markup.
    body = (b"<!doctype html><html><head><title>Report</title>"
            b"<meta name='description' content='Quarterly numbers'></head><body>"
            b"<nav><a href='/'>Home</a></nav>"
            b"<main><h1>Report</h1><p>Revenue climbed this quarter.</p></main>"
            b"<footer>copyright junk</footer></body></html>")
    res = extractor.extract(body, "application/pdf", "https://x/q3.pdf")
    md = res["markdown"]
    assert md.startswith("---")                       # YAML frontmatter header
    assert 'title: "Report"' in md
    assert 'url: "https://x/q3.pdf"' in md
    assert "Revenue climbed this quarter." in md
    assert "Home" not in md                            # nav chrome stripped from body
    assert "copyright junk" not in md                  # footer chrome stripped
    assert res["meta"].get("declared") == "pdf"


def test_pdf_url_octet_stream_html_body_is_detected():
    # Server sends octet-stream for a .pdf URL, but the body is an HTML error page.
    body = b"<html><body>Not Found</body></html>"
    res = extractor.extract(body, "application/octet-stream", "https://x/missing.pdf")
    assert res["kind"] == "html"
    assert "Not Found" in res["text"]


def test_pdf_url_returning_plain_text_is_detected():
    body = b"Error: the requested document is temporarily unavailable."
    res = extractor.extract(body, "application/pdf", "https://x/doc.pdf")
    assert res["kind"] == "text"
    assert res["content_kind"] == "text"
    assert "temporarily unavailable" in res["text"]


def test_real_pdf_not_misdetected_as_text():
    data = _make_text_pdf("Genuine PDF body")
    res = extractor.extract(data, "application/pdf", "https://x/real.pdf")
    assert res["kind"] == "pdf"
    assert res["meta"].get("declared_pdf") is None  # took the real PDF path


# --- docx -------------------------------------------------------------------

def test_docx_extraction():
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("Title", level=1)
    document.add_paragraph("A body paragraph.")
    buf = io.BytesIO()
    document.save(buf)
    ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    res = extractor.extract(buf.getvalue(), ct, "https://x/report.docx")
    assert res["content_kind"] == "docx"
    assert "Title" in res["markdown"]
    assert "A body paragraph." in res["text"]
    assert res["ocr"] is None


# --- image OCR (skips cleanly when RapidOCR absent) ------------------------

def _text_png(text: str) -> bytes:
    """A PNG of `text`, legible enough to OCR without needing a system font.

    PIL's built-in bitmap font is ~11px — too small for reliable recognition —
    so draw at that size and upscale. Keeps the fixture font-free, which matters
    because CI runners carry no guaranteed TrueType face."""
    Image = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw
    im = Image.new("RGB", (200, 40), "white")
    ImageDraw.Draw(im).text((8, 14), text, fill="black")
    im = im.resize((im.width * 6, im.height * 6), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_image_ocr_reads_the_text():
    pytest.importorskip("rapidocr_onnxruntime")
    res = extractor.extract(_text_png("INVOICE 42"), "image/png",
                            "https://x/scan.png", ocr_mode="auto")
    assert res["kind"] == "image"
    # Assert the text actually came back. The old version accepted
    # content_kind in ("ocr", "image") and an empty string, so it passed just as
    # happily when OCR recognized nothing at all.
    assert res["content_kind"] == "ocr"
    assert "INVOICE" in res["text"].upper()
    assert res["ocr"]["engine"] == "rapidocr"
    assert res["ocr"]["confidence"] > 0.5


def test_scanned_pdf_is_rasterized_and_ocred():
    """The core OCR path: a PDF whose only content is an IMAGE of text.

    No text layer ⇒ the engine must rasterize each page and OCR the pixels,
    tagging content_kind=ocr so the page bills the OCR lane."""
    pytest.importorskip("rapidocr_onnxruntime")
    pytest.importorskip("pypdfium2")

    png = _text_png("PURCHASE ORDER 88231")
    data = _make_image_only_pdf(png, 1200, 240)

    res = extractor.extract(data, "application/pdf", "https://x/scan.pdf", ocr_mode="auto")
    assert res["meta"]["text_layer"] is False
    assert res["content_kind"] == "ocr"
    assert "88231" in res["text"]
    assert res["ocr"]["pages"] == 1

    # ocr_mode=off on the same bytes must yield nothing rather than OCR anyway.
    off = extractor.extract(data, "application/pdf", "https://x/scan.pdf", ocr_mode="off")
    assert off["ocr"] is None
    assert not off["text"].strip()


def test_image_ocr_off_skips():
    res = extractor.extract(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "image/png", "", ocr_mode="off")
    assert res["kind"] == "image"
    assert res["ocr"] is None


# --- Table plausibility: shredded prose must never render as a table --------


def test_justified_prose_grid_is_not_a_table():
    """Justified paragraphs align words into vertical columns; pdfplumber's
    text strategy reads them as a uniform-width grid of 1-3 word fragments with
    words CUT at column boundaries ("con" | "sectetuer"). The word-split gate
    must reject that shape under both the strict (borderless) and ruled bars."""
    from engines import pdf_infer

    shredded = [
        ["Sample", "PDF", None, None, None, None],
        ["This is a simple", "PDF fil", "e. Fun fun fun", ".", None, None],
        ["Lorem ipsum dolor sit", "amet, con", "sectetuer adipiscing",
         "elit. Phasellus f", "acilisis odio", "sed mi."],
        ["Curabitur suscipit. Nulla", "m vel nisi", ". Etiam semper ipsu",
         "m ut lectus. Proi", "n aliquam,", "erat eget"],
        ["pulvinar quis, nisl.", None, None, None, None, None],
    ]
    assert pdf_infer._word_split_fraction(shredded) >= 0.35
    assert pdf_infer._plausible_table(shredded, strict=True) is False
    assert pdf_infer._plausible_table(shredded, strict=False) is False


def test_real_and_unit_column_tables_stay_plausible():
    from engines import pdf_infer

    real = [
        ["Product", "Qty", "Price", "Total"],
        ["Widget A", "2", "$4.00", "$8.00"],
        ["Widget B", "1", "$3.50", "$3.50"],
        ["Gadget", "10", "$0.99", "$9.90"],
    ]
    assert pdf_infer._plausible_table(real, strict=True) is True
    assert pdf_infer._plausible_table(real, strict=False) is True

    # Lowercase unit cells ("mm", "kg") create a few letter→lowercase junctions
    # in a GENUINE table; the fraction gate must tolerate them.
    units = [
        ["Dimension", "Value", "Unit"],
        ["Length", "120", "mm"],
        ["Width", "80", "mm"],
        ["Mass", "2.4", "kg"],
    ]
    assert pdf_infer._plausible_table(units, strict=True) is True


def test_prose_filled_raggedness_rejected_even_without_word_cuts():
    """Prose lines fill wildly varying column counts (a closing line fills 1,
    a full line 6) even when the cuts happen to land on word boundaries — the
    filled-count consistency bar must reject that on its own."""
    from engines import pdf_infer

    ragged = [
        ["The quick", "Brown fox", "Jumps over", "The lazy", "Dog and", "Runs."],
        ["Then it", "Sat down.", None, None, None, None],
        ["A new", "Sentence starts", "Here with", "More words", "To fill", "Lines."],
        ["End.", None, None, None, None, None],
    ]
    # Every cell starts uppercase, so the word-split gate stays quiet — this
    # fixture isolates the filled-count consistency bar.
    assert pdf_infer._word_split_fraction(ragged) < 0.35
    assert pdf_infer._plausible_table(ragged, strict=True) is False
