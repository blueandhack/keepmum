"""Real documents from plain text, with the standard library only, for the doc-* cases.

  python3 docs.py pdf  SOURCE.txt OUT.pdf    a one-page PDF, its text extractable (Helvetica, no compression)
  python3 docs.py docx SOURCE.txt OUT.docx   a Word document: `# ` and `## ` lines are headings, `- ` lines
                                             list items, the rest paragraphs

The source is written by the case (so it can hold the run's values) and removed after.
"""

import sys
import zipfile
from xml.sax.saxutils import escape


def pdf(lines: list[str]) -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = ["BT", "/F1 11 Tf", "14 TL", "56 780 Td"]
    for line in lines:
        size = 16 if line.startswith("# ") else 11
        ops += [f"/F1 {size} Tf", f"({esc(line.lstrip('# '))}) Tj", "T*"]
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def docx(lines: list[str], path: str) -> None:
    paras = []
    for line in lines:
        if line.startswith("## "):
            style, text = "Heading2", line[3:]
        elif line.startswith("# "):
            style, text = "Heading1", line[2:]
        elif line.startswith("- "):
            style, text = "ListBullet", line[2:]
        else:
            style, text = None, line
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        paras.append(f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {ns}><w:body>{"".join(paras)}</w:body></w:document>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", document)


if __name__ == "__main__":
    kind, source, target = sys.argv[1:4]
    lines = open(source, encoding="utf-8").read().strip("\n").splitlines()
    if kind == "pdf":
        with open(target, "wb") as f:
            f.write(pdf(lines))
    else:
        docx(lines, target)
