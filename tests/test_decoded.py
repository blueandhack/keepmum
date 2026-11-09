"""A secret a command printed encoded (a hex dump, `od -c`, plain hex, base64) is masked: the
decoded text is scanned (keepmum.decoded). A live pi run on DeepSeek ran `xxd .env`, and the token
went out as hex and in 16-character pieces of the dump's text column."""

import base64
import json
import shutil
import subprocess

import pytest

from keepmum.decoded import blocks
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.redact import Redactor
from keepmum.vault import Vault

TOKEN = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"  # synthetic, in GitHub's format
ENV = f"GITHUB_TOKEN={TOKEN}\nLOG_LEVEL=info\n".encode()


def xxd(data: bytes) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        groups = " ".join(chunk[i:i + 2].hex() for i in range(0, len(chunk), 2))
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08x}: {groups:<39}  {text}")
    return "\n".join(lines) + "\n"


def hexdump_c(data: bytes) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        left = " ".join(f"{b:02x}" for b in chunk[:8])
        right = " ".join(f"{b:02x}" for b in chunk[8:])
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08x}  {left:<23}  {right:<23}  |{text}|")
    return "\n".join(lines) + "\n"


def od_c(data: bytes) -> str:
    esc = {10: "\\n", 9: "\\t", 13: "\\r", 0: "\\0"}
    lines = []
    for off in range(0, len(data), 16):
        fields = "".join(f"{esc.get(b, chr(b)):>4}" for b in data[off:off + 16])
        lines.append(f"{off:07o} {fields}")
    return "\n".join(lines) + "\n"


def wrapped(s: str, n: int) -> str:
    return "\n".join(s[i:i + n] for i in range(0, len(s), n)) + "\n"


FORMATS = {
    "xxd": xxd(ENV),
    "hexdump -C": hexdump_c(ENV),
    "od -c": od_c(ENV),
    "xxd -p": wrapped(ENV.hex(), 60),
    "base64": wrapped(base64.b64encode(ENV).decode(), 76),
    "base64 inline": "the file is " + base64.b64encode(ENV).decode() + " (base64)",
}


@pytest.mark.parametrize("name", FORMATS)
def test_each_format_decodes_to_the_file(name):
    assert any(TOKEN in d for _, _, d in blocks(FORMATS[name]))


@pytest.mark.parametrize("name", FORMATS)
def test_a_secret_printed_encoded_is_masked_with_its_block(name):
    out = Redactor(Pipeline(RulesDetector()), Vault(bytes(range(32)))).redact_text(f"$ cmd .env\n{FORMATS[name]}done\n")
    assert "KEEPMUM_SECRET_" in out and out.endswith("done\n")
    assert not any(x in out for x in ("ghp", "Zq8v", "_Zq8", "4749", "R0lU", "6768"))


@pytest.mark.parametrize("name", FORMATS)
def test_codex_output_a_json_document_in_a_text_field_is_read_too(name):
    """Codex's command output is JSON in a text field: its new lines are `\\n`, and od's `\\n` is `\\\\n`."""
    doc = json.dumps({"chunk_id": "3bf602", "exit_code": 0, "output": FORMATS[name]})
    out = Redactor(Pipeline(RulesDetector()), Vault(bytes(range(32)))).redact_text(doc)
    assert "KEEPMUM_SECRET_" in out and not any(x in out for x in ("ghp", "Zq8v", "4749", "R0lU"))
    assert json.loads(out)["chunk_id"] == "3bf602"  # still a JSON document


def test_a_value_masked_before_is_found_encoded():
    v = Vault(bytes(range(32)))
    r = Redactor(Pipeline(RulesDetector()), v)
    v.placeholder("PERSON", "Margaret Thornbury", ["pf-serve"])
    out = r.redact_text("blob: " + base64.b64encode(b"client=Margaret Thornbury; since 2021").decode())
    assert out.startswith("blob: KEEPMUM_")


@pytest.mark.parametrize("text", [
    "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "commit a3f9c2e17b4d9e8f0a1b2c3d4e5f60718293a4b5",
    "00000000 fix: the build",
    "def test_a_value_in_a_script_a_shell_runs_is_quoted_for_that_shell_too():",
    xxd(b"just a readme, nothing secret in it at all\n"),
    "payload " + base64.b64encode(b"hello world, this is plain text").decode(),
])
def test_encoded_text_without_a_secret_is_left_alone(text):
    assert Redactor(Pipeline(RulesDetector()), Vault(bytes(range(32)))).redact_text(text) == text


@pytest.mark.skipif(not shutil.which("xxd"), reason="no xxd")
def test_the_real_xxd(tmp_path):
    (tmp_path / ".env").write_bytes(ENV)
    out = subprocess.run(["xxd", ".env"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "Zq8v" not in Redactor(Pipeline(RulesDetector()), Vault(bytes(range(32)))).redact_text(out)


@pytest.mark.skipif(not shutil.which("od"), reason="no od")
@pytest.mark.parametrize("args", [["-c"], ["-An", "-c"], ["-t", "x1"], ["-An", "-t", "x1"], ["-t", "x1c"],
                                  ["-An", "-t", "x1c"], ["-A", "x", "-t", "x1"]])
@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_the_real_od_in_each_layout(args, eol, tmp_path):
    """Without offsets (`-An`), and hex lines with character lines between them (`-t x1c`): a live
    pi run printed .env with `od -An -tx1c`, and the token went out as hex and spaced characters."""
    (tmp_path / ".env").write_bytes(ENV)
    out = subprocess.run(["od", *args, ".env"], cwd=tmp_path, capture_output=True, text=True).stdout.replace("\n", eol)
    red = Redactor(Pipeline(RulesDetector()), Vault(bytes(range(32)))).redact_text(out)
    assert "KEEPMUM_SECRET_" in red
    squeezed = "".join(red.split())
    assert not any(x in squeezed for x in ("Zq8v", "5a71", "TOKEN", "544f4b45"))
