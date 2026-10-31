"""End to end: keepmum as `keepmum serve` builds it, on real sockets, with a mock upstream on a
real port, while the CLI changes its settings. What each request carried is read back from what
the upstream received."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from keepmum import launch, selftest
from keepmum.audit import audit_file, verify
from keepmum.cli import app
from keepmum.config import load, set_value
from keepmum.stats import stats_token
from keepmum.vault import PLACEHOLDER


class _App:
    def __init__(self, app):
        self.app = app


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """A running proxy built from ~/.config/keepmum/config.toml in a HOME of its own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in launch.BYPASS_KEYS:
        monkeypatch.delenv(k, raising=False)
    servers = []

    def start(config: str, rules_only: bool = True):
        cfg_dir = tmp_path / ".config" / "keepmum"
        cfg_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (cfg_dir / "config.toml").write_text(config)
        mock = selftest.MockUpstream()
        upstream = launch.BackgroundServer(_App(mock.app))
        upstream.start()
        url = f"http://127.0.0.1:{upstream.port}"
        proxy = launch.build_proxy(load(), rules_only=rules_only, upstream=url, mode="serve")
        proxy.prefixed["/openai"] = url  # `keepmum serve` has no flag for the OpenAI upstream
        server = launch.BackgroundServer(proxy)
        server.start()
        servers.extend([server, upstream])
        client = httpx.Client(base_url=f"http://127.0.0.1:{server.port}", timeout=60,
                              headers={"x-api-key": "sk-ant-test-not-a-key", "anthropic-version": "2023-06-01"})

        def send(text: str) -> bytes:
            """Send one message; what the upstream received for it."""
            mock.received.clear()
            r = client.post("/v1/messages", json={"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": text}]})
            assert r.status_code == 200, r.text
            return b"".join(mock.received)

        def stats() -> dict:
            return client.get("/_keepmum/stats", headers={"authorization": f"Bearer {stats_token()}"}).json()

        return server, send, stats

    yield start
    for s in servers:
        if s.thread.is_alive():
            s.stop()


def cli(*args):
    r = CliRunner().invoke(app, list(args))
    assert r.exit_code == 0, r.output
    return r.output


def test_settings_changed_by_the_cli_apply_to_a_running_proxy(serve):
    server, send, _ = serve("[detect]\nmodel = false\n")

    first = selftest.make_canary()
    sent = send(f"export GITHUB_TOKEN={first}")
    assert first.encode() not in sent and PLACEHOLDER.search(sent.decode())

    cli("observe", "on")
    watched = selftest.make_canary()
    assert watched.encode() in send(f"token {watched}")  # observe only: sent unchanged
    cli("observe", "off")
    again = selftest.make_canary()
    assert again.encode() not in send(f"token {again}")

    allowed = selftest.make_canary()
    cli("rules", "allow", "--", allowed)  # a value after --, as the app passes it
    assert allowed.encode() in send(f"token {allowed}")

    # Each change is recorded as made by its command; the proxy, seeing the file change, adds none.
    entries = [json.loads(line) for line in audit_file().read_text().splitlines()]
    changes = [(e["by"], c["setting"]) for e in entries if e["event"] == "config" and not e.get("baseline")
               for c in e["changes"]]
    assert changes == [("keepmum observe on", "detect.observe"), ("keepmum observe off", "detect.observe"),
                       ("keepmum rules allow", "allowlist.strings")]

    server.stop()
    assert not launch.runtime_file().exists()
    assert verify()["ok"]


def test_personal_data_turned_on_and_off_while_running(serve):
    from keepmum.detect.pipeline import ModelUnavailable, default_pipeline

    try:
        default_pipeline().pf.close()
    except ModelUnavailable:
        pytest.skip("Privacy Filter model not downloaded")
    server, send, stats = serve("[detect]\nmodel = true\n", rules_only=False)
    name, email = "Margaret Thornbury", "margaret.thornbury@gmail.com"
    text = f"Hi, I'm {name}. Write to me at {email} about the invoice."

    sent = send(text)
    assert name.encode() in sent and email.encode() in sent and stats()["pii"] is False

    set_value("detect", "pii", True, load().path)
    sent = send(text)
    assert name.encode() not in sent and email.encode() not in sent  # the name only the model finds
    assert stats()["pii"] is True

    # Turned off again: values masked earlier as personal data go through as they are.
    set_value("detect", "pii", False, load().path)
    sent = send(text)
    assert name.encode() in sent and email.encode() in sent and stats()["pii"] is False
