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


def test_a_dump_of_a_secrets_cut_off_tail_is_masked():
    # `tail -c 200 .env | od -c | tail -3` printed the last 23 characters of a key keepmum had masked
    # (crypto-01 on Claude Code, Nov 18).
    import subprocess

    r = redactor()
    key = "0x" + secrets.token_hex(32)
    assert "KEEPMUM_SECRET_" in r.redact_text(f"STAGING_SIGNING_KEY={key}")
    dump = subprocess.run(["od", "-c"], input=(key[-23:] + "\n").encode(), capture_output=True).stdout.decode()
    out = r.redact_text(dump)
    assert "KEEPMUM_SECRET_" in out and " ".join(key[-10:-4]) not in out
    # A dump of anything else is left alone.
    other = subprocess.run(["od", "-c"], input=b"LOG_LEVEL=info\nAPP_ENV=dev\n", capture_output=True).stdout.decode()
    assert r.redact_text(other) == other


def test_a_secrets_cut_off_tail_in_plain_text_is_masked():
    # `tail -c 200 .env` began in the middle of a key keepmum had masked whole (crypto-01, Nov 18).
    r = redactor()
    key = "0x" + secrets.token_hex(32)
    assert "KEEPMUM_SECRET_" in r.redact_text(f"DEPLOYER_PRIVATE_KEY={key}\nRPC_URL=https://rpc.example.org")
    out = r.redact_text(key[-54:] + "\nRPC_URL=https://rpc.example.org\nCHAIN_ID=11155111")
    assert key[-54:-30] not in out and out.startswith("KEEPMUM_SECRET_") and "CHAIN_ID=11155111" in out
    # Text that only shares a few characters with it is left alone.
    assert r.redact_text(f"commit {key[2:10]}abcdef0123 done") == f"commit {key[2:10]}abcdef0123 done"


def test_text_of_escapes_in_literal_strings():
    # \101 is octal for "A", a backslash at a line end continues the string, \q is no escape: dropped
    drawn = b"BT (\\101cme\\\nPay \\q(x) \\t\\\\ ok) Tj ET"
    assert text_of(pdf(drawn)) == "AcmePay q(x) \t\\ ok"


def test_text_of_the_quote_operators_and_spacing_in_tj():
    # ' and " move to the next line and show a string; in TJ a gap wider than 200 is a space; a Td
    # along the line (second operand 0) keeps the line, one that moves down starts a new one.
    drawn = (b"BT (first) ' 1 2 (second) \" [(wide) -250 (gap) -50 (narrow)] TJ 40 0 Td (same) Tj "
             b"0 -14 Td (next) Tj /F1 9 Tf (font) Tj ET")
    assert text_of(pdf(drawn)) == "first\nsecond\nwide gapnarrowsame\nnextfont"


def test_text_of_a_font_with_a_range_of_codes():
    # bfrange: <0001> <0003> <0041> maps codes 1..3 to A..C; an array form and a short line are skipped.
    cmap = (b"begincmap\n3 beginbfrange\n<0001> <0003> <0041>\n<0010> <0011> [<0058> <0059>]\n<0020>\nendbfrange\nendcmap")
    cmap_obj = b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream"
    assert text_of(pdf(b"BT <000100020003> Tj ET", [cmap_obj])) == "ABC"
    # Codes the table does not know, in a one-byte font: read as they are.
    one = b"begincmap\n1 beginbfchar\n<01> <0041>\nendbfchar\nendcmap"
    one_obj = b"<< /Length %d >>\nstream\n" % len(one) + one + b"\nendstream"
    assert text_of(pdf(b"BT (\\001bc) Tj ET", [one_obj])) == "Abc"


def test_text_of_skips_streams_it_cannot_read():
    broken = b"<< /Length 9 /Filter /FlateDecode >>\nstream\nnot zlib!\nendstream"
    image = b"<< /Length 4 /Filter /DCTDecode >>\nstream\n\xff\xd8\xff\xe0\nendstream"
    unclosed = b"<< /Length 99 >>\nstream\nBT (lost) Tj ET"
    assert text_of(pdf(b"BT (kept) Tj ET", [broken, image])) == "kept"
    assert text_of(pdf(b"BT (kept) Tj ET", [unclosed])) == "kept"


def test_text_of_stops_at_its_caps(monkeypatch):
    from keepmum import pdftext

    monkeypatch.setattr(pdftext, "MAX_TEXT", 30)
    many = b"BT " + b" T* ".join(b"(line %02d of text) Tj" % i for i in range(20)) + b" ET"
    out = text_of(pdf(many, [b"<< /Length 20 >>\nstream\nBT (after the cap) Tj ET\nendstream"]))
    assert out.startswith("line 00 of text\nline 01") and len(out) <= 30 and "after" not in out
    monkeypatch.setattr(pdftext, "MAX_PDF", 10)
    assert text_of(PLAIN) == ""


def test_from_base64_reads_data_urls_and_refuses_what_is_not_base64():
    from keepmum.pdftext import from_base64

    assert from_base64("data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4").decode()) == b"%PDF-1.4"
    assert from_base64(base64.b64encode(b"x").decode()) == b"x"
    assert from_base64("abc") is None  # incomplete padding
