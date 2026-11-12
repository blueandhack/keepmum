import http.server
import json
import threading

import pytest
from typer.testing import CliRunner

from keepmum import local, localmodels
from keepmum.cli import app
from keepmum.config import load
from keepmum.local import Server
from keepmum.localmodels import DEFAULT_PICK, PICKS, PullError, quant_of, resolve

PICK = PICKS[0].model


@pytest.mark.parametrize("ref, quant, want", [
    ("recommended", None, PICK),
    (PICK, None, PICK),
    ("qwen3:8b", None, "qwen3:8b"),
    ("https://huggingface.co/owner/Some-Model-GGUF", "Q4_K_M", "hf.co/owner/Some-Model-GGUF:Q4_K_M"),
    ("https://huggingface.co/owner/Some-Model-GGUF/tree/main?x=1", None, "hf.co/owner/Some-Model-GGUF"),
    ("huggingface.co/owner/repo", "IQ3_M", "hf.co/owner/repo:IQ3_M"),
    ("hf.co/owner/repo:Q8_0", None, "hf.co/owner/repo:Q8_0"),
    ("hf.co/owner/repo:Q8_0", "Q4_K_M", "hf.co/owner/repo:Q4_K_M"),  # --quant wins
    ("https://huggingface.co/owner/repo/blob/main/Model.i1-Q4_K_M.gguf", None, "hf.co/owner/repo:Q4_K_M"),
    ("https://huggingface.co/owner/repo/resolve/main/Qwen3.6-35B-A3B-Aggressive-IQ3_M.gguf", None, "hf.co/owner/repo:IQ3_M"),
    ("gemma3", "12b", "gemma3:12b"),
])
def test_resolve_names_what_ollama_pulls(ref, quant, want):
    assert resolve(ref, quant) == want


@pytest.mark.parametrize("ref, quant", [("", None), ("https://huggingface.co/owner", None), ("ftp://huggingface.co/a/b", None),
                                        ("hf.co/owner/repo", "Q4 K M"), ("https://huggingface.co/a/b$c", None)])
def test_resolve_refuses_what_is_not_a_model(ref, quant):
    with pytest.raises(PullError):
        resolve(ref, quant)


def test_quant_of_reads_a_gguf_files_name():
    assert quant_of("Bimo-7B-CyberAgent.Q4_K_M.gguf") == "Q4_K_M"
    assert quant_of("Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-NEO-MAX-MTP-Q8_0.gguf") == "Q8_0"
    assert quant_of("model-bf16.gguf") == "BF16"
    assert quant_of("Qwen3.5-9B.gguf") is None


def test_picks_start_with_the_default_and_fit_their_mac():
    assert PICKS[0].id == DEFAULT_PICK == "recommended"
    assert len({p.id for p in PICKS}) == len(PICKS)
    for p in PICKS:
        assert p.model.startswith("hf.co/") and p.memory_gb < p.mac_gb * 0.75


def _ollama(lines: list[dict], status: int = 200):
    """A fake Ollama /api/pull on a free port; its URL, and the bodies it was sent."""
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for line in lines:
                self.wfile.write((json.dumps(line) + "\n").encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", got, srv


def test_pull_reports_progress_across_layers():
    url, got, srv = _ollama([
        {"status": "pulling manifest"},
        {"status": "pulling a", "digest": "a", "total": 100, "completed": 50},
        {"status": "pulling a", "digest": "a", "total": 100, "completed": 100},
        {"status": "pulling b", "digest": "b", "total": 20, "completed": 20},
        {"status": "success"}])
    seen = []
    localmodels.pull(url, "m:q", lambda d, t: seen.append((d, t)))
    srv.shutdown()
    assert got == [{"model": "m:q", "stream": True}]
    assert seen == [(50, 100), (100, 100), (120, 120)]


@pytest.mark.parametrize("lines, status, says", [
    ([{"error": "pull model manifest: file does not exist"}], 200, "does not exist"),
    ([{"status": "pulling manifest"}], 200, "stopped before"),
    ([{"error": "invalid model name"}], 400, "invalid model name"),
])
def test_pull_says_why_it_failed(lines, status, says):
    url, _, srv = _ollama(lines, status)
    with pytest.raises(PullError, match=says):
        localmodels.pull(url, "m")
    srv.shutdown()


def test_pull_when_nothing_answers():
    with pytest.raises(PullError, match="open the Ollama app"):
        localmodels.pull("http://127.0.0.1:9", "m", timeout=1)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "keepmum" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("")
    return path


def test_the_cli_pulls_the_default_pick_and_uses_it(home, monkeypatch):
    pulled = []
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: [])
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama")
    monkeypatch.setattr(localmodels, "pull", lambda url, model, progress=None, timeout=30.0: (pulled.append((url, model)), progress(5, 10)))
    r = CliRunner().invoke(app, ["guard", "local", "pull"])
    assert r.exit_code == 0, r.output
    assert pulled == [("http://localhost:11434", PICK)] and "50% of" in r.stderr
    assert load(home).guard_local_servers == [Server("http://localhost:11434", PICK)]
    # A Hugging Face link onto the same server: its model changes, no second server.
    r = CliRunner().invoke(app, ["guard", "local", "pull", "https://huggingface.co/o/r", "--quant", "Q4_K_M"])
    assert r.exit_code == 0, r.output
    assert load(home).guard_local_servers == [Server("http://localhost:11434", "hf.co/o/r:Q4_K_M")]


def test_the_cli_skips_a_model_ollama_has_and_leaves_others_alone(home, monkeypatch):
    home.write_text('[guard]\nlocal_servers = [{ url = "http://localhost:1234", model = "qwen/qwen3-8b" }, '
                    '{ url = "http://localhost:11434", model = "qwen3:8b", checks = ["harm"] }]\n')
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: [PICK])
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama" if url.endswith("11434") else "LM Studio")
    monkeypatch.setattr(localmodels, "pull", lambda *a, **k: pytest.fail("already there"))
    r = CliRunner().invoke(app, ["guard", "local", "pull", "recommended"])
    assert r.exit_code == 0 and "already on Ollama" in r.stderr
    assert load(home).guard_local_servers == [Server("http://localhost:1234", "qwen/qwen3-8b"),
                                              Server("http://localhost:11434", PICK, ("harm",))]
    r = CliRunner().invoke(app, ["guard", "local", "pull", "--server", "1"])
    assert r.exit_code == 1 and "only Ollama downloads" in r.stderr


def test_the_cli_lists_picks(home, monkeypatch):
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: None)
    out = CliRunner().invoke(app, ["guard", "local", "picks"]).stdout
    assert out.startswith("recommended (default)") and PICK in out
    data = json.loads(CliRunner().invoke(app, ["guard", "local", "--json"]).stdout)
    assert data["default_pick"] == "recommended" and [p["id"] for p in data["picks"]] == [p.id for p in PICKS]
    assert "keepmum guard local pull" in CliRunner().invoke(app, ["guard", "local"]).stdout
    assert CliRunner().invoke(app, ["guard", "local", "pull", "ftp://huggingface.co/a/b"]).exit_code == 2
