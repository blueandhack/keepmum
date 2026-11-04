"""What gets masked: types passed through, your own rules, your exceptions, applied while running."""

import json
import secrets
import textwrap

import anyio
import httpx
import pytest

from keepmum import config as config_mod
from keepmum import selftest
from keepmum.detect import Span
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.redact import Redactor
from keepmum.vault import Vault


class FakeModel:
    """Finds a fixed name as PERSON, like the model would."""
    name = "pf"

    def detect(self, text):
        i = text.find("Priya Raman")
        return [Span(i, i + 11, "PERSON", 0.9, "pf")] if i >= 0 else []


def values(text, spans):
    return [text[s.start:s.end] for s in spans]


def test_passed_types_are_counted_but_not_masked():
    p = Pipeline(RulesDetector(), FakeModel(), pii=True)
    p.apply_settings(pass_types=["PERSON"])
    token = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"
    text = f"Priya Raman set GITHUB_TOKEN={token}"
    assert values(text, p.detect(text)) == [token]
    report = p.report()
    assert report["types"]["PERSON"] == {"found": 1, "allowed": 0, "passed": 1, "masked": 0}
    assert report["types"]["SECRET"]["masked"] == 1 and report["pass"] == ["PERSON"]


def test_known_values_of_a_passed_type_are_not_masked_again():
    p = Pipeline(RulesDetector(), FakeModel(), pii=True)
    r = Redactor(p, Vault(secrets.token_bytes(32)))
    assert "Priya" not in r.redact_text("ask Priya Raman")
    p.apply_settings(pass_types=["PERSON"])
    assert r.redact_text("Priya Raman again, and ask Priya Raman") == "Priya Raman again, and ask Priya Raman"


def test_user_rules_mask_even_with_personal_data_off_and_exceptions_win():
    p = Pipeline(RulesDetector(), None, pii=False)
    p.apply_settings(mask_rules=[{"name": "Employee ID", "pattern": r"EMP-(\d{6})", "type": "ACCOUNT"}],
                     allow_strings=["424242"])
    text = "EMP-123456 and EMP-424242"
    spans = p.detect(text)
    assert values(text, spans) == ["123456"] and spans[0].source == "user:Employee ID"
    assert p.report()["rules"] == [{"rule": "Employee ID", "user": True, "n": 1, "type": None}]
    assert p.report()["types"]["ACCOUNT"] == {"found": 2, "allowed": 1, "passed": 0, "masked": 1}


def test_settings_change_while_running(tmp_path):
    from keepmum import launch

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[detect]\npii = true\n")
    cfg = config_mod.load(cfg_path)
    proxy = launch.build_proxy(cfg, rules_only=True, upstream="http://up")
    mock = selftest.MockUpstream()
    proxy.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
    body = {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "badge EMP-123456"}]}

    async def send():
        await client.post("/v1/messages", json=body)

    anyio.run(send)
    assert b"EMP-123456" in mock.received[-1]
    config_mod.set_value("rules", "mask", [{"name": "Employee ID", "pattern": r"EMP-\d{6}", "type": "ACCOUNT"}], cfg_path)
    anyio.run(send)  # same text again: the redactor's cache must not hide the new rule
    assert b"EMP-123456" not in mock.received[-1]


def test_set_value_keeps_the_rest_and_rejects_bad_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""\
        # my settings
        [server]
        port = 9000

        [detect]
        pii = true
        pass = [
          "DATE",
        ]
    """))
    config_mod.set_value("detect", "pass", ["PERSON", "DATE"], path)
    config_mod.set_value("rules", "mask", [{"name": "q", "pattern": 'a"b\\d', "type": "SECRET"}], path)
    cfg = config_mod.load(path)
    assert cfg.port == 9000 and cfg.pii and cfg.pass_types == ["PERSON", "DATE"]
    assert cfg.mask_rules == [{"name": "q", "pattern": 'a"b\\d', "type": "SECRET"}]
    assert path.read_text().startswith("# my settings")
    before = path.read_text()
    with pytest.raises(config_mod.ConfigError):
        config_mod.set_value("detect", "pass", ["NOPE"], path)
    assert path.read_text() == before


def test_cli_types_needs_yes_for_secrets(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from keepmum.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    run = lambda *a: CliRunner().invoke(app, list(a))
    assert run("types", "pass", "SECRET").exit_code == 2
    assert run("types", "pass", "person").exit_code == 0
    assert config_mod.load(tmp_path / ".config" / "keepmum" / "config.toml").pass_types == ["PERSON"]
    assert run("rules", "mask", "EMP-\\d{6}", "--name", "Employee ID", "--type", "account").exit_code == 0
    assert run("rules", "allow", "acme-test-key").exit_code == 0
    data = json.loads(run("rules", "list", "--json").stdout)
    assert data["mask"][0]["type"] == "ACCOUNT" and data["allow_strings"] == ["acme-test-key"]
    assert run("rules", "remove", "mask", "0").exit_code == 0
    assert config_mod.load(tmp_path / ".config" / "keepmum" / "config.toml").mask_rules == []


# --- The model on or off (detect.model) ---------------------------------------------------


class Closable(FakeModel):
    closed = False

    def close(self):
        self.closed = True


def test_model_setting_is_read_and_checked(tmp_path):
    path = tmp_path / "config.toml"
    assert config_mod.load(path).model is True  # on unless turned off
    path.write_text("[detect]\nmodel = false\n")
    assert config_mod.load(path).model is False
    path.write_text('[detect]\nmodel = "no"\n')
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(path)


def test_model_off_needs_no_model_files(tmp_path, monkeypatch):
    from keepmum import launch
    from keepmum.detect import pfserve

    monkeypatch.setattr(pfserve, "find_binary", lambda: None)
    monkeypatch.setattr(pfserve, "find_model", lambda: None)
    path = tmp_path / "config.toml"
    path.write_text("[detect]\nmodel = false\n")
    proxy = launch.build_proxy(config_mod.load(path), upstream="http://up")
    assert proxy.redactor.detector.name == "rules" and proxy.redactor.detector.pf is None


def test_model_switches_while_running_without_dropping_a_request(tmp_path, monkeypatch):
    import time

    from keepmum import launch
    from keepmum.detect import pipeline as pipeline_mod

    model = Closable()
    monkeypatch.setattr(pipeline_mod, "load_model", lambda: model)
    path = tmp_path / "config.toml"
    path.write_text("[detect]\npii = true\nmodel = false\n")
    proxy = launch.build_proxy(config_mod.load(path), upstream="http://up")
    mock = selftest.MockUpstream()
    proxy.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
    body = {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "ask Priya Raman"}]}
    pipeline = proxy.redactor.detector

    def send():
        async def go():
            r = await client.post("/v1/messages", json=body)
            stats = await client.get("/_keepmum/stats", headers={"authorization": f"Bearer {proxy.stats_token}"})
            return r.status_code, stats.json()
        return anyio.run(go)

    def until(cond):
        deadline = time.monotonic() + 5
        while not cond() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cond()

    code, stats = send()
    assert code == 200 and b"Priya Raman" in mock.received[-1] and stats["detector"] == "rules:pii"
    config_mod.set_detect_flag("model", True, path)
    code, _ = send()  # served while the model loads, by the rules
    assert code == 200
    until(lambda: pipeline.pf is model)
    code, stats = send()  # the same text again: scanned again, now with the model
    assert code == 200 and b"Priya Raman" not in mock.received[-1] and stats["detector"] == "rules+pf:pii"
    config_mod.set_detect_flag("model", False, path)
    send()
    until(lambda: pipeline.pf is None)
    assert model.closed and send()[1]["detector"] == "rules:pii"


def test_a_model_that_does_not_load_is_reported_and_the_rules_keep_scanning():
    import threading

    from keepmum.detect.pipeline import ModelSwitch, ModelUnavailable

    def broken():
        raise ModelUnavailable("the Privacy Filter model is not downloaded")

    p = Pipeline(RulesDetector(), None)
    switch = ModelSwitch(p, threading.Lock(), loader=broken)
    switch.want(True)
    switch.thread.join()
    assert p.pf is None and "not downloaded" in p.report()["model_error"]
    token = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"
    assert values(f"t={token}", p.detect(f"t={token}")) == [token]
    switch.want(False)
    switch.thread.join()
    assert "model_error" not in p.report()


def test_cli_model_on_needs_the_model_downloaded(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from keepmum.cli import app
    from keepmum.detect import pfserve

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pfserve, "find_model", lambda: None)
    path = tmp_path / ".config" / "keepmum" / "config.toml"
    run = lambda *a: CliRunner().invoke(app, list(a))
    assert run("model", "off").exit_code == 0 and config_mod.load(path).model is False
    assert run("model", "on").exit_code == 2 and config_mod.load(path).model is False
    monkeypatch.setattr(pfserve, "find_model", lambda: tmp_path / "model.gguf")
    monkeypatch.setattr(pfserve, "find_binary", lambda: tmp_path / "pf-serve")
    assert run("model", "on").exit_code == 0 and config_mod.load(path).model is True


def test_pii_command_switches_personal_data_and_is_audited(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from keepmum.audit import read
    from keepmum.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "keepmum" / "config.toml"
    run = lambda *a: CliRunner().invoke(app, list(a))
    assert run("pii", "maybe").exit_code == 2
    assert run("pii", "on").exit_code == 0 and config_mod.load(path).pii is True
    assert json.loads(run("types", "--json").stdout)["types"]["PERSON"] == "mask"
    assert run("pii", "off").exit_code == 0 and config_mod.load(path).pii is False
    assert "off" in json.loads(run("types", "--json").stdout)["types"]["PERSON"]
    entries = [e for e in read(path.parent / "audit.jsonl") if e["event"] == "config"]
    assert entries[-1]["weakens"] and entries[-1]["by"] == "keepmum pii off"



# --- Built-in rules turned off ([rules] off) ----------------------------------------------


def test_a_builtin_rule_turned_off_stops_masking_and_is_reported():
    p = Pipeline(RulesDetector(), None, pii=True)
    text = "mobile: 4155552671 and the cache warmer key for prod is a7c3e9f1b5d2c8e04f6a"
    assert values(text, p.detect(text)) == ["4155552671", "a7c3e9f1b5d2c8e04f6a"]
    p.apply_settings(off_rules=["phone-labeled", "key-in-prose"])
    assert p.detect(text) == [] and p.report()["off"] == ["key-in-prose", "phone-labeled"]


def test_turning_off_entropy_leaves_named_rules_working():
    p = Pipeline(RulesDetector(), None)
    p.apply_settings(off_rules=["entropy"])
    token = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"
    opaque = "Kq8ZxV2mN4pL7rR9tY3wB6cF1hJ5dG"
    text = f"t={token} blob {opaque}"
    assert values(text, p.detect(text)) == [token]


def test_unknown_builtin_rules_are_config_errors(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[rules]\noff = ["entropy", "luhn"]\n')
    assert config_mod.load(path).off_rules == ["entropy", "luhn"]
    path.write_text('[rules]\noff = ["no-such-rule"]\n')
    with pytest.raises(config_mod.ConfigError, match="no-such-rule"):
        config_mod.load(path)


def test_cli_rules_off_needs_yes_for_secret_rules(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from keepmum.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "keepmum" / "config.toml"
    run = lambda *a: CliRunner().invoke(app, list(a))
    assert run("rules", "off", "nope").exit_code == 2
    assert run("rules", "off", "key-in-prose").exit_code == 2  # finds secrets
    assert run("rules", "off", "phone-labeled").exit_code == 0
    assert run("rules", "off", "key-in-prose", "--yes").exit_code == 0
    assert config_mod.load(path).off_rules == ["phone-labeled", "key-in-prose"]
    listed = {r["id"]: r for r in json.loads(run("rules", "builtin", "--json").stdout)}
    assert listed["key-in-prose"] == {"id": "key-in-prose", "type": "SECRET", "on": False} and listed["entropy"]["on"]
    assert run("rules", "on", "key-in-prose").exit_code == 0
    assert config_mod.load(path).off_rules == ["phone-labeled"]
    assert json.loads(run("rules", "list", "--json").stdout)["off"] == ["phone-labeled"]


def test_rules_turned_off_apply_while_running(tmp_path):
    from keepmum import launch

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[detect]\nmodel = false\n")
    proxy = launch.build_proxy(config_mod.load(cfg_path), upstream="http://up")
    mock = selftest.MockUpstream()
    proxy.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
    body = {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "run it with --token Xk29fPq7Lm3vR8tZ"}]}

    async def send():
        await client.post("/v1/messages", json=body)

    anyio.run(send)
    assert b"Xk29fPq7Lm3vR8tZ" not in mock.received[-1]
    config_mod.set_value("rules", "off", ["secret-flag"], cfg_path)
    anyio.run(send)
    assert b"Xk29fPq7Lm3vR8tZ" in mock.received[-1]


class FindsToken(FakeModel):
    """Finds one fixed secret, like the model would."""

    def detect(self, text):
        i = text.find("Xk29fPq7Lm3vR8tZ")
        return [Span(i, i + 16, "SECRET", 0.9, "pf")] if i >= 0 else []


def test_a_value_masked_earlier_follows_the_settings():
    r = Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32)))
    text = "run it with --token Xk29fPq7Lm3vR8tZ"
    assert "Xk29fPq7Lm3vR8tZ" not in r.redact_text(text)
    r.detector.apply_settings(off_rules=["secret-flag"])
    # Its only detector is off: the conversation resent with it, or the value elsewhere, goes as is.
    assert r.redact_text(text) == text and r.redact_text("echo Xk29fPq7Lm3vR8tZ") == "echo Xk29fPq7Lm3vR8tZ"


def test_a_value_the_model_also_found_stays_masked():
    r = Redactor(Pipeline(RulesDetector(), FindsToken()), Vault(secrets.token_bytes(32)))
    assert "Xk29fPq7Lm3vR8tZ" not in r.redact_text("run it with --token Xk29fPq7Lm3vR8tZ")
    r.detector.apply_settings(off_rules=["secret-flag"])
    assert "Xk29fPq7Lm3vR8tZ" not in r.redact_text("echo Xk29fPq7Lm3vR8tZ")


def test_an_exception_or_a_removed_rule_releases_a_value_masked_earlier():
    p = Pipeline(RulesDetector(), None, pii=True)
    r = Redactor(p, Vault(secrets.token_bytes(32)))
    p.apply_settings(mask_rules=[{"name": "Employee ID", "pattern": r"EMP-\d{6}", "type": "ACCOUNT"}])
    assert "EMP-123456" not in r.redact_text("badge EMP-123456")
    p.apply_settings()  # the rule removed
    assert r.redact_text("badge EMP-123456") == "badge EMP-123456"
    assert "4155552671" not in r.redact_text("mobile: 4155552671")
    p.apply_settings(allow_strings=["4155552671"])
    assert r.redact_text("call 4155552671") == "call 4155552671"


def test_pii_and_restore_policy_hot_reload_and_config_permissions(tmp_path):
    import re

    from keepmum import launch
    from keepmum.restore import DEFAULT_POLICY

    cfg_path = tmp_path / "config.toml"
    config_mod.set_value("detect", "model", False, cfg_path)
    assert cfg_path.stat().st_mode & 0o777 == 0o600
    proxy = launch.build_proxy(config_mod.load(cfg_path), upstream="http://up")

    def tool_upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/messages":
            return httpx.Response(200, json={"data": []})
        m = re.search(r"KEEPMUM_SECRET_[0-9a-f]{8}", request.content.decode())
        ph = m.group(0) if m else "none"
        return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "webfetch", "input": {"url": f"https://example.test/?q={ph}"}},
        ]})

    received: list[bytes] = []

    def recording_upstream(request: httpx.Request) -> httpx.Response:
        received.append(request.content)
        return tool_upstream(request)

    proxy.client = httpx.AsyncClient(transport=httpx.MockTransport(recording_upstream), base_url="http://up")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
    canary = selftest.make_canary()
    body = {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": f"mobile: 4155552671 GITHUB_TOKEN={canary}"}]}

    async def send():
        r = await client.post("/v1/messages", json=body)
        st = await client.get("/_keepmum/stats", headers={"authorization": f"Bearer {proxy.stats_token}"})
        return r, st.json()

    r, st = anyio.run(send)
    assert st["pii"] is False and b"4155552671" in received[-1] and canary not in r.text  # webfetch kept placeholder

    config_mod.set_value("detect", "pii", True, cfg_path)
    config_mod.set_value("restore", "webfetch", "restore", cfg_path)
    r, st = anyio.run(send)
    assert st["pii"] is True and b"4155552671" not in received[-1] and canary in r.text  # hot-reloaded pii + restore policy
    assert proxy.policy["WebSearch"] == DEFAULT_POLICY["WebSearch"]

    config_mod.set_value("detect", "pii", False, cfg_path)
    r, st = anyio.run(send)
    assert st["pii"] is False and b"4155552671" in received[-1]  # known PII released when pii turned back off


class CountingModel(FakeModel):
    def __init__(self):
        self.calls = 0

    def detect(self, text):
        self.calls += 1
        return super().detect(text)


def test_a_settings_change_does_not_send_the_conversation_through_the_model_again():
    model = CountingModel()
    p = Pipeline(RulesDetector(), model, pii=True)
    r = Redactor(p, Vault(secrets.token_bytes(32)))
    text = "ask Priya Raman about the build"
    assert "Priya" not in r.redact_text(text) and model.calls == 1
    p.apply_settings(pass_types=["PERSON"])  # the redactor drops its cache; the model's output is kept
    assert r.redact_text(text) == text and model.calls == 1
    p.apply_settings()
    assert "Priya" not in r.redact_text(text) and model.calls == 1
    p.set_model(model)  # a model (re)loaded: its output is new
    r.redact_text(text)
    assert model.calls == 2


def test_a_model_failure_is_not_cached():
    class Flaky(FakeModel):
        calls = 0

        def detect(self, text):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("pf-serve timed out")
            return super().detect(text)

    p = Pipeline(RulesDetector(), Flaky(), pii=True)
    with pytest.raises(RuntimeError):
        p.detect("ask Priya Raman")
    assert values("ask Priya Raman", p.detect("ask Priya Raman")) == ["Priya Raman"]
