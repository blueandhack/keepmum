"""PDFs attached whole (pdftext.py, Redactor.pdf_in_place): a value keepmum masks, printed in one,
no longer reaches the provider in its bytes (doc-02, Nov 18)."""

import base64
import json
import secrets
import zlib

from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.pdftext import text_of
from keepmum.redact import Redactor
from keepmum.vault import Vault

KEY = "sk_live_" + "Zq3bX8mL2vT9wR4kY7nP1sD6"  # split: GitHub's push protection takes a whole fake key for a real one


def pdf(content: bytes, extra: list[bytes] = (), flate: bool = False) -> bytes:
    """A one-page PDF drawing `content` (a content stream), with `extra` objects after its font."""
    stream = zlib.compress(content) if flate else content
    filt = b" /Filter /FlateDecode" if flate else b""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d%s >>\nstream\n" % (len(stream), filt) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", *extra]
    out = bytearray(b"%PDF-1.4\n")
    for i, body in enumerate(objects, 1):
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    return bytes(out + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n")


PLAIN = pdf(b"BT /F1 11 Tf 56 780 Td (Acme Payments \\(onboarding\\)) Tj T* (Your live API key: " + KEY.encode()
            + b") Tj T* [(Invoice INV-2026-) -20 (0412)] TJ ET")


def test_text_of_a_plain_and_a_compressed_pdf():
    want = f"Acme Payments (onboarding)\nYour live API key: {KEY}\nInvoice INV-2026-0412"
    assert text_of(PLAIN) == want
    assert text_of(pdf(b"BT (Your live API key: " + KEY.encode() + b") Tj ET", flate=True)) == f"Your live API key: {KEY}"


def test_text_of_a_pdf_whose_font_maps_its_codes():
    # Chrome and Word draw text as font codes (hex strings) mapped to Unicode by a ToUnicode table.
    text = f"Key {KEY} 东京"
    codes = {ch: i + 1 for i, ch in enumerate(dict.fromkeys(text))}
    cmap = b"begincmap\n%d beginbfchar\n" % len(codes) + b"".join(
        b"<%04X> <%s>\n" % (code, ch.encode("utf-16-be").hex().upper().encode()) for ch, code in codes.items()) + b"endbfchar\nendcmap"
    drawn = b"BT /F1 11 Tf 56 780 Td <" + "".join(f"{codes[c]:04X}" for c in text).encode() + b"> Tj ET"
    cmap_obj = b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(zlib.compress(cmap)) + zlib.compress(cmap) + b"\nendstream"
    assert text_of(pdf(drawn, [cmap_obj], flate=True)) == text


def test_text_of_what_is_no_pdf_is_empty():
    assert text_of(b"\x89PNG\r\n\x1a\n" + secrets.token_bytes(64)) == "" and text_of(pdf(b"q 1 0 0 1 0 0 cm Q")) == ""


def redactor():
    return Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32)))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_a_pdf_with_a_key_goes_as_its_masked_text_in_a_messages_request():
    r = redactor()
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64(PLAIN)}}
    body = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [doc]}]}]}
    block = r.redact_request(body)["messages"][1]["content"][0]["content"][0]
    assert block["type"] == "text" and "KEEPMUM_SECRET_" in block["text"] and "INV-2026-0412" in block["text"]
    assert KEY not in json.dumps(block) and "keepmum" in block["text"].split("\n")[0]
    # The same PDF in the next request: the same text, so the conversation's history stays the same.
    assert r.redact_request(body)["messages"][1]["content"][0]["content"][0] == block
    assert [t for _, t, _ in r.tool_results if "INV-2026-0412" in t]  # its text is checked as a tool result


def test_a_pdf_with_nothing_to_mask_goes_as_it_is():
    r = redactor()
    clean = pdf(b"BT (Invoice INV-2026-0412, EUR 12,480.00) Tj ET")
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64(clean)}}
    assert r.redact_request({"messages": [{"role": "user", "content": [doc]}]})["messages"][0]["content"][0] == doc


def test_a_pdf_with_a_key_in_a_responses_tool_output_and_in_gemini():
    r = redactor()
    part = {"type": "input_file", "filename": "acme.pdf", "file_data": "data:application/pdf;base64," + b64(PLAIN)}
    body = {"input": [{"type": "function_call", "call_id": "c1", "name": "view", "arguments": "{}"},
                      {"type": "function_call_output", "call_id": "c1", "output": [part]}]}
    out = r.redact_responses_request(body)["input"][-1]["output"][0]
    assert out["type"] == "input_text" and "`acme.pdf`" in out["text"] and KEY not in out["text"]
    msg = r.redact_responses_request({"input": [{"type": "message", "role": "user", "content": [part]}]})["input"][-1]
    assert msg["content"][0]["type"] == "input_text"
    for key, mime in (("inline_data", "mime_type"), ("inlineData", "mimeType")):
        g = r.redact_gemini_request({"contents": [{"role": "user", "parts": [{key: {mime: "application/pdf", "data": b64(PLAIN)}}]}]})
        assert set(g["contents"][0]["parts"][0]) == {"text"} and KEY not in json.dumps(g)


def test_a_pdf_in_a_gemini_function_result_goes_as_text_in_its_response():
    # Code Assist takes files only in a function response's parts (agy, doc-02, Nov 18).
    r = redactor()
    body = {"contents": [{"role": "user", "parts": [{"functionResponse": {"name": "read_file", "response": {"output": "read 1 file"},
                                                                       "parts": [{"inlineData": {"mimeType": "application/pdf", "data": b64(PLAIN)}}]}}]}]}
    fr = r.redact_gemini_request(body)["contents"][0]["parts"][0]["functionResponse"]
    assert fr["parts"] == [] and fr["response"]["output"] == "read 1 file"
    assert "INV-2026-0412" in fr["response"]["file_text"] and KEY not in json.dumps(fr)


def test_keepmums_own_prefix_alone_is_no_secret():
    assert Pipeline(RulesDetector(), None).detect("api_key = KEEPMUM_") == []
