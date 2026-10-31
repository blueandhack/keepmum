import json
import os
import secrets
import stat
import sys
import textwrap

import httpx
import pytest

from keepmum import config as config_mod
from keepmum import launch, selftest
from keepmum.detect.pipeline import Pipeline, default_pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.proxy import Proxy
from keepmum.redact import Redactor
from keepmum.vault import Vault


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in launch.BYPASS_KEYS:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def write_config(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(text))
    return p


def test_config_defaults_when_missing(tmp_path):
    cfg = config_mod.load(tmp_path / "nope.toml")
    assert cfg.port == 7788 and cfg.pii is False and cfg.restore["WebFetch"] == "keep"


def test_config_reads_values_and_merges_policy(tmp_path):
    cfg = config_mod.load(write_config(tmp_path, """
        [server]
        port = 9000
        [detect]
        pii = true
        [allowlist]
        strings = ["ACME_TEST_KEY_1234"]
        [restore]
        Bash = "keep"
        "mcp__files__*" = "restore"
    """))
    assert cfg.port == 9000 and cfg.pii and cfg.allow_strings == ["ACME_TEST_KEY_1234"]
    assert cfg.restore["Bash"] == "keep" and cfg.restore["WebFetch"] == "keep" and cfg.restore["mcp__files__*"] == "restore"


@pytest.mark.parametrize("text, message", [
    ("[detect]\njudge = true\n", "unknown key detect.judge"),
    ("[server]\nport = \"7788\"\n", "server.port must be int"),
    ("[detect]\npii = 1\n", "detect.pii must be bool"),
    ("[restore]\nBash = \"maybe\"\n", "restore.Bash must be"),
    ("[allowlist]\npatterns = [\"(\"]\n", "allowlist pattern"),
    ("[nonsense]\nx = 1\n", "unknown section"),
    ("[server]\nupstream = \"ftp://x\"\n", "server.upstream"),
])
def test_config_rejects_bad_values(tmp_path, text, message):
    with pytest.raises(config_mod.ConfigError, match=message):
        config_mod.load(write_config(tmp_path, text))


def test_user_allowlist_adds_to_the_defaults():
    token = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"
    p = default_pipeline(rules_only=True, allow_strings=[token])
    assert p.detect(f"t = {token}") == []
    assert p.detect("aws_access_key_id = AKIAIOSFODNN7EXAMPLE") == []  # built-in entry still there


def test_bypass_risks_in_settings_and_env(home, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    assert launch.bypass_risks(project) == []
    (project / ".claude" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://elsewhere"}}))
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    risks = launch.bypass_risks(project)
    assert any("CLAUDE_CODE_USE_BEDROCK" in r for r in risks) and any("settings.json sets env ANTHROPIC_BASE_URL" in r for r in risks)


FAKE_CLAUDE = """#!{python}
import json, os, sys, urllib.request
base = os.environ["ANTHROPIC_BASE_URL"]
body = {{"model": "m", "max_tokens": 8, "messages": [{{"role": "user", "content": "export GITHUB_TOKEN=" + os.environ["CANARY"]}}]}}
req = urllib.request.Request(base + "/v1/messages", data=json.dumps(body).encode(), headers={{"content-type": "application/json"}})
reply = json.load(urllib.request.urlopen(req))
open(os.environ["OUT"], "w").write(json.dumps({{"base": base, "args": sys.argv[1:], "reply": reply}}))
sys.exit(7)
"""


def test_keepmum_claude_runs_claude_through_the_proxy(home, tmp_path, monkeypatch):
    canary = selftest.make_canary()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CANARY", canary)
    monkeypatch.setenv("OUT", str(tmp_path / "out.json"))
    mock = selftest.MockUpstream()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    proxy = Proxy(Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32))), upstream="http://up", client=upstream)

    code = launch.run_claude(proxy, ["-p", "hello", "--model", "x"])

    out = json.loads((tmp_path / "out.json").read_text())
    assert code == 7  # Claude Code's exit code is passed through
    assert out["base"].startswith("http://127.0.0.1:") and out["args"] == ["-p", "hello", "--model", "x"]
    assert canary in out["reply"]["content"][0]["text"]  # restored on the way back
    assert not any(canary.encode() in b for b in mock.received)  # never sent upstream
    assert not launch.runtime_file().exists()  # cleaned up when Claude exits


def test_runtime_file_is_private_while_running(home):
    mock = selftest.MockUpstream()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    proxy = Proxy(Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32))), upstream="http://up", client=upstream)
    server = launch.BackgroundServer(proxy)
    server.start()
    try:
        info = json.loads(launch.runtime_file().read_text())
        assert info["port"] == server.port and info["pid"] == os.getpid()
        assert launch.runtime_file().stat().st_mode & 0o777 == 0o600
        assert httpx.get(f"http://127.0.0.1:{server.port}/_keepmum/health").json() == {"ok": True}
    finally:
        server.stop()
    assert not launch.runtime_file().exists()


WRAPPED_CLAUDE = """#!{python}
import json, os, sys, urllib.request
base = os.environ["ANTHROPIC_BASE_URL"]
body = {{"model": "m", "max_tokens": 8, "messages": [{{"role": "user", "content": "export GITHUB_TOKEN=" + os.environ["CANARY"]}}]}}
req = urllib.request.Request(base + "/v1/messages", data=json.dumps(body).encode(), headers={{"content-type": "application/json"}})
reply = json.load(urllib.request.urlopen(req))
open(os.environ["OUT"], "w").write(json.dumps({{"base": base, "args": sys.argv[1:], "reply": reply}}))
print("CLAUDE-STDOUT")
sys.exit(5)
"""


class _App:
    def __init__(self, app):
        self.app = app


@pytest.fixture
def wrapper_env(tmp_path, monkeypatch):
    """A fake claude, a mock Anthropic on a real port, and a private HOME for the wrapper."""
    import shutil

    from keepmum.detect.pfserve import find_binary, find_model

    model_env = {k: str(v) for k, v in (("KEEPMUM_PF_SERVE", find_binary()), ("KEEPMUM_PF_MODEL", find_model())) if v}
    monkeypatch.setenv("HOME", str(tmp_path))  # the mock upstream's runtime.json goes here
    mock = selftest.MockUpstream()
    upstream = launch.BackgroundServer(_App(mock.app))
    upstream.start()
    fake = tmp_path / "claude"
    fake.write_text(WRAPPED_CLAUDE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in launch.BYPASS_KEYS}
    env.update(HOME=str(home), **model_env, CANARY=selftest.make_canary(),
               OUT=str(tmp_path / "out.json"), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{upstream.port}")
    wrapper = shutil.which("keepmum-claude-wrapper", path=os.path.dirname(sys.executable))
    yield {"env": env, "fake": str(fake), "mock": mock, "home": home, "out": tmp_path / "out.json", "wrapper": wrapper}
    upstream.stop()


def test_wrapper_runs_claude_through_its_own_proxy(wrapper_env):
    import subprocess

    from keepmum.detect.pipeline import ModelUnavailable, default_pipeline

    try:
        default_pipeline()
    except ModelUnavailable:
        pytest.skip("Privacy Filter model not downloaded")
    w = wrapper_env
    r = subprocess.run([w["wrapper"], w["fake"], "--output-format", "stream-json"], env=w["env"], cwd=w["home"],
                       capture_output=True, text=True, timeout=120)
    out = json.loads(w["out"].read_text())
    canary = w["env"]["CANARY"]
    assert r.returncode == 5 and r.stdout == "CLAUDE-STDOUT\n"  # exit code passed on; stdout untouched
    assert out["args"] == ["--output-format", "stream-json"]
    assert out["base"] != w["env"]["ANTHROPIC_BASE_URL"]  # went through keepmum, which chained to the mock
    assert canary in out["reply"]["content"][0]["text"]
    assert not any(canary.encode() in b for b in w["mock"].received)


def test_wrapper_reuses_a_running_keepmum_serve(wrapper_env, monkeypatch):
    import subprocess

    w = wrapper_env
    serve_proxy = Proxy(Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32))),
                        upstream=w["env"]["ANTHROPIC_BASE_URL"], stats_token="tok", info={"mode": "serve"})
    serve = launch.BackgroundServer(serve_proxy)
    serve.start()
    try:
        cfg_dir = w["home"] / ".config" / "keepmum"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.toml").write_text(f"[server]\nport = {serve.port}\n")
        (cfg_dir / "stats-token").write_text("tok")
        r = subprocess.run([w["wrapper"], w["fake"]], env=w["env"], cwd=w["home"], capture_output=True, text=True, timeout=60)
    finally:
        serve.stop()
    out = json.loads(w["out"].read_text())
    assert r.returncode == 5 and out["base"] == f"http://127.0.0.1:{serve.port}"
    assert not any(w["env"]["CANARY"].encode() in b for b in w["mock"].received)


def test_wrapper_refuses_on_a_bypass_risk(wrapper_env):
    import subprocess

    w = wrapper_env
    r = subprocess.run([w["wrapper"], w["fake"]], env={**w["env"], "CLAUDE_CODE_USE_BEDROCK": "1"}, cwd=w["home"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "could bypass" in r.stderr and r.stdout == ""
    assert not w["out"].exists()  # Claude was never started


def test_wrapper_reports_a_missing_binary_cleanly(wrapper_env):
    import subprocess

    w = wrapper_env
    r = subprocess.run([w["wrapper"], "/nonexistent/claude"], env=w["env"], cwd=w["home"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and r.stdout == "" and "cannot run /nonexistent/claude" in r.stderr and "Traceback" not in r.stderr
