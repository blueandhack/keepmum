import http.server
import json
import threading

import pytest
from typer.testing import CliRunner

from keepmum import local, localmodels
from keepmum.cli import app
from keepmum.config import load
from keepmum.local import Server
from keepmum.localmodels import DEFAULT_PICK, PICKS, PullError, copy_name, parse_context, quant_of, resolve

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


@pytest.fixture
def no_copies(monkeypatch):
    """keepmum's context copies, without an Ollama: the name it would make, from the model's base."""
    monkeypatch.setattr(localmodels, "base_of", lambda url, model: model)

    def with_context(url, model, ctx, timeout=120.0):
        base = localmodels.base_of(url, model)
        return base if ctx is None else copy_name(base, ctx)

    monkeypatch.setattr(localmodels, "with_context", with_context)
    monkeypatch.setattr(localmodels, "delete_copy", lambda url, model: True)


def test_the_cli_pulls_the_default_pick_and_uses_it(home, monkeypatch, no_copies):
    pulled = []
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: [])
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama")
    monkeypatch.setattr(localmodels, "pull", lambda url, model, progress=None, timeout=30.0: (pulled.append((url, model)), progress(5, 10)))
    r = CliRunner().invoke(app, ["guard", "local", "pull"])
    assert r.exit_code == 0, r.output
    assert pulled == [("http://localhost:11434", PICK)] and "50% of" in r.stderr and "Context: 64k tokens" in r.stderr
    assert load(home).guard_local_servers == [Server("http://localhost:11434", copy_name(PICK, 65536))]
    # A Hugging Face link onto the same server, at Ollama's own context: its model changes, no second server.
    r = CliRunner().invoke(app, ["guard", "local", "pull", "https://huggingface.co/o/r", "--quant", "Q4_K_M", "--context", "default"])
    assert r.exit_code == 0, r.output
    assert load(home).guard_local_servers == [Server("http://localhost:11434", "hf.co/o/r:Q4_K_M")] and "Ollama's own" in r.stderr
    assert CliRunner().invoke(app, ["guard", "local", "pull", "--context", "huge"]).exit_code == 2


def test_the_cli_skips_a_model_ollama_has_and_leaves_others_alone(home, monkeypatch, no_copies):
    home.write_text('[guard]\nlocal_servers = [{ url = "http://localhost:1234", model = "qwen/qwen3-8b" }, '
                    '{ url = "http://localhost:11434", model = "qwen3:8b", checks = ["harm"] }]\n')
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: [PICK])
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama" if url.endswith("11434") else "LM Studio")
    monkeypatch.setattr(localmodels, "pull", lambda *a, **k: pytest.fail("already there"))
    r = CliRunner().invoke(app, ["guard", "local", "pull", "recommended"])
    assert r.exit_code == 0 and "already on Ollama" in r.stderr
    assert load(home).guard_local_servers == [Server("http://localhost:1234", "qwen/qwen3-8b"),
                                              Server("http://localhost:11434", copy_name(PICK, 65536), ("harm",))]
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


def test_parse_context_takes_sizes_and_default():
    assert parse_context("64k") == 65536 and parse_context("32768") == 32768 and parse_context(" 16K ") == 16384
    assert parse_context("default") is None and parse_context("max") is None
    for bad in ("huge", "1k", "4096k", "-1"):
        with pytest.raises(PullError):
            parse_context(bad)


def test_copy_names_are_ollama_names():
    assert copy_name(PICK, 65536) == "keepmum/qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive-iq3_m:ctx64k"
    assert copy_name("qwen3:8b", 32768) == "keepmum/qwen3-8b:ctx32k" and copy_name("m", 50000) == "keepmum/m:ctx50000"


def test_with_context_copies_from_the_base_and_reads_it_back(monkeypatch):
    import httpx

    sent = []

    def post(url, json=None, timeout=None):
        sent.append((url.rsplit("/", 1)[-1], json))
        if url.endswith("/api/show"):
            if json["model"].startswith("keepmum/"):
                return httpx.Response(200, json={"parameters": "num_ctx                        65536\ntop_k 20",
                                                 "model_info": {"qwen35moe.context_length": 262144},
                                                 "details": {"parent_model": "base:q"}})
            return httpx.Response(200, json={"parameters": "top_k 20", "model_info": {"qwen35moe.context_length": 262144}, "details": {}})
        return httpx.Response(200, json={"status": "success"})

    monkeypatch.setattr(httpx, "post", post)
    real = localmodels
    assert real.show("http://o", "keepmum/base-q:ctx64k") == {"context": 65536, "max": 262144, "parent": "base:q"}
    assert real.show("http://o", "base:q") == {"context": None, "max": 262144, "parent": None}
    # From a copy to another size: the new copy is made from the base, never from the copy.
    assert real.with_context("http://o", "keepmum/base-q:ctx64k", 32768) == "keepmum/base-q:ctx32k"
    assert sent[-1] == ("create", {"model": "keepmum/base-q:ctx32k", "from": "base:q", "parameters": {"num_ctx": 32768}, "stream": False})
    assert real.with_context("http://o", "keepmum/base-q:ctx64k", None) == "base:q"
    assert real.delete_copy("http://o", "base:q") is False  # never a model the user downloaded


def test_the_cli_changes_the_context_and_drops_the_old_copy(home, monkeypatch, no_copies):
    home.write_text(f'[guard]\nlocal_servers = [{{ url = "http://localhost:11434", model = "{copy_name(PICK, 65536)}" }}]\n')
    dropped = []
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: [copy_name(PICK, 65536), PICK])
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama")
    monkeypatch.setattr(localmodels, "base_of", lambda url, model: PICK)
    monkeypatch.setattr(localmodels, "show", lambda url, model, timeout=5.0: {"context": 65536, "max": 262144, "parent": PICK})
    monkeypatch.setattr(localmodels, "delete_copy", lambda url, model: dropped.append(model) or True)
    shown = CliRunner().invoke(app, ["guard", "local", "context"])
    assert shown.exit_code == 0 and "64k tokens (it takes up to 256k tokens)" in shown.stdout and "Options:" in shown.stdout
    r = CliRunner().invoke(app, ["guard", "local", "context", "32k"])
    assert r.exit_code == 0, r.output
    assert load(home).guard_local_servers == [Server("http://localhost:11434", copy_name(PICK, 32768))]
    assert dropped == [copy_name(PICK, 65536)]
    assert CliRunner().invoke(app, ["guard", "local", "context", "lots"]).exit_code == 2
    assert CliRunner().invoke(app, ["guard", "local", "context", "32k", "--server", "2"]).exit_code == 2


SPLASH_SPEC = "HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:IQ3_M"
SPLASH_ID = SPLASH_SPEC  # what Splash serves, started as `splash serve --model SPLASH_SPEC`


def test_splash_names_and_command():
    from keepmum.localmodels import splash_command, splash_id

    assert splash_id(SPLASH_SPEC) == SPLASH_ID and PICKS[0].splash == SPLASH_SPEC
    assert [p.id for p in PICKS if p.splash] == ["recommended", "larger"]  # Splash has no draft for the 9B
    want = f"splash serve --model {SPLASH_SPEC} --language-only --max-context 64K"
    assert splash_command(SPLASH_ID) == splash_command("recommended") == want
    assert splash_command(SPLASH_SPEC.split(":")[0]) == want  # a local file's served name: the repo alone
    assert splash_command("owner/other:Q4") == "splash serve --model owner/other:Q4 --language-only --max-context 64K"


def test_kind_knows_splash_and_missing_says_how_to_start_it(monkeypatch):
    import httpx

    def get(url, timeout=None):
        if url.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": SPLASH_ID, "owned_by": "splash"}]})
        return httpx.Response(404, json={})

    monkeypatch.setattr(httpx, "get", get)
    assert local.kind("http://localhost:8000") == "Splash"
    server = Server("http://localhost:8000", SPLASH_ID)
    assert local.missing(server, [SPLASH_ID], "Splash") is None
    assert f"`splash serve --model {SPLASH_SPEC}" in local.missing(server, None, "Splash")
    assert f"(Splash? `splash serve --model {SPLASH_SPEC}" in local.missing(server, None, None)  # down: unnamed
    other = local.missing(Server("http://localhost:8000", "owner/b"), ["owner/a"], "Splash")
    assert "it serves owner/a; restart it with `splash serve --model owner/b" in other
    assert "splash" in local.PRESETS and local.PRESETS["splash"][1] == "http://localhost:8000"


def test_the_cli_puts_splash_first_and_prints_how_to_start_it(home, monkeypatch):
    import shutil

    home.write_text('[guard]\nlocal_servers = [{ url = "http://localhost:11434", model = "qwen3:8b" }]\n')
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: None)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = CliRunner().invoke(app, ["guard", "local", "splash"])
    assert r.exit_code == 0, r.output
    assert load(home).guard_local_servers == [Server("http://localhost:8000", SPLASH_ID), Server("http://localhost:11434", "qwen3:8b")]
    assert f"splash serve --model {SPLASH_SPEC} --language-only --max-context 64K" in r.stdout
    assert "brew install incoai/tap/splash" in r.stderr
    # Again with the larger pick: the same server, its model changed, still first.
    assert CliRunner().invoke(app, ["guard", "local", "splash", "larger"]).exit_code == 0
    servers = load(home).guard_local_servers
    assert [x.url for x in servers] == ["http://localhost:8000", "http://localhost:11434"]
    assert servers[0].model == "HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:Q4_K_M"
    r = CliRunner().invoke(app, ["guard", "local", "splash", "light"])
    assert r.exit_code == 2 and "Splash does not run light" in r.stderr
    assert CliRunner().invoke(app, ["guard", "local", "splash", "nonsense"]).exit_code == 2
    assert CliRunner().invoke(app, ["guard", "local", "splash", "--url", "http://example.com:8000"]).exit_code == 2


def test_the_cli_says_when_splash_already_serves_it(home, monkeypatch):
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: [SPLASH_ID])
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Splash")
    r = CliRunner().invoke(app, ["guard", "local", "splash", "--url", "http://127.0.0.1:8093"])
    assert r.exit_code == 0 and "Splash is running with it" in r.stderr and r.stdout == ""
    assert load(home).guard_local_servers == [Server("http://127.0.0.1:8093", SPLASH_ID)]


def test_the_listing_gives_the_command_that_starts_splash(home, monkeypatch):
    home.write_text(f'[guard]\nlocal_servers = [{{ url = "http://localhost:8000", model = "{SPLASH_ID}" }}, '
                    '{ url = "http://localhost:11434", model = "qwen3:8b" }]\n')
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: None)
    rows = json.loads(CliRunner().invoke(app, ["guard", "local", "--json"]).stdout)["servers"]
    assert rows[0]["start"] == f"splash serve --model {SPLASH_SPEC} --language-only --max-context 64K"
    assert rows[1]["start"] is None
    picks = json.loads(CliRunner().invoke(app, ["guard", "local", "picks", "--json"]).stdout)["picks"]
    assert picks[0]["splash"] == SPLASH_SPEC and picks[2]["splash"] is None
