"""clients.py: the edges of Codex, pi and opencode configs: files keepmum must refuse, leave alone,
or put back exactly, and opencode's version probe."""

import json
import tomllib

import pytest

from keepmum import clients
from keepmum.protect import ProtectError

PORT = 7788
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    (tmp_path / "codex").mkdir()
    (tmp_path / "pi").mkdir()
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    monkeypatch.setattr(clients, "_gateways", lambda: {"corp": "https://gw.corp.example"})
    clients._version_cache.clear()
    return tmp_path


# --- Codex -------------------------------------------------------------------------------------

def test_codex_run_provider_refuses_bad_toml_local_models_and_providers_without_a_url(homes):
    cfg = homes / "codex" / "config.toml"
    cfg.write_text("model_provider = \n")
    with pytest.raises(ProtectError, match="is not valid TOML"):
        clients.codex_run_provider(PORT, {})
    cfg.write_text('model_provider = "ollama"\n')
    with pytest.raises(ProtectError, match="model on this Mac"):
        clients.codex_run_provider(PORT, {})
    cfg.write_text('model_provider = "corp"\n[model_providers.corp]\nname = "corp"\nbase_url = 5\n')
    with pytest.raises(ProtectError, match="has no base_url"):
        clients.codex_run_provider(PORT, {})


def test_codex_run_provider_with_no_config_is_keepmums_own(homes):
    provider, extra = clients.codex_run_provider(PORT, {})
    assert provider == clients.codex_provider(PORT) and extra == {}


def test_codex_run_provider_routes_through_a_known_gateway(homes):
    (homes / "codex" / "config.toml").write_text(
        'model_provider = "corp"\n[model_providers.corp]\nname = "Corp"\nbase_url = "https://gw.corp.example/v1/"\nenv_key = "CORP_KEY"\n')
    provider, extra = clients.codex_run_provider(PORT, {"corp": "https://gw.corp.example"})
    assert extra == {} and provider["base_url"] == f"{BASE}/gateway/corp/v1"
    assert provider["env_key"] == "CORP_KEY" and provider["name"] == "keepmum"


def test_codex_enable_refuses_a_provider_without_a_url_or_without_adopt(homes):
    cfg = homes / "codex" / "config.toml"
    cfg.write_text('model_provider = "corp"\n[model_providers.corp]\nname = "corp"\n')
    with pytest.raises(ProtectError, match="has no base_url"):
        clients.codex_enable(PORT, adopt=lambda url, who: url)
    cfg.write_text('model_provider = "corp"\n[model_providers.corp]\nbase_url = "https://gw.corp.example/v1"\n')
    with pytest.raises(ProtectError, match="already selects model_provider 'corp'"):
        clients.codex_enable(PORT)
    assert "keepmum" not in cfg.read_text()


def test_codex_enable_and_status_reject_invalid_toml(homes):
    cfg = homes / "codex" / "config.toml"
    cfg.write_text("[broken\n")
    with pytest.raises(ProtectError, match="fix it first"):
        clients.codex_enable(PORT)
    st = clients.codex_status(PORT)
    assert st["installed"] and not st["on"] and "is not valid TOML" in st["error"]


def test_codex_enable_never_writes_a_file_codex_cannot_read(homes):
    cfg = homes / "codex" / "config.toml"
    original = 'model_providers = { keepmum = { name = "inline" } }\n'
    cfg.write_text(original)
    with pytest.raises(ProtectError, match="could not update"):
        clients.codex_enable(PORT)
    assert cfg.read_text() == original


def test_codex_disable_without_a_config_or_without_keepmum_changes_nothing(homes):
    cfg = homes / "codex" / "config.toml"
    assert clients.codex_disable(PORT) == {"installed": False, "on": False} and not cfg.exists()
    cfg.write_text('model = "gpt-5"\n')
    st = clients.codex_disable(PORT)
    assert cfg.read_text() == 'model = "gpt-5"\n' and not st["on"]
    assert not (homes / "codex" / "config.toml.keepmum-backup").exists()


def test_codex_through_a_gateway_reports_via_and_off_selects_the_old_provider(homes):
    cfg = homes / "codex" / "config.toml"
    original = 'model_provider = "corp"\n\n[model_providers.corp]\nname = "Corp"\nbase_url = "https://gw.corp.example/v1"\n'
    cfg.write_text(original)
    st = clients.codex_enable(PORT, adopt=lambda url, who: f"{BASE}/gateway/corp/v1")
    assert st["on"] and st["via"] == "https://gw.corp.example/v1"
    assert '(was "corp")' in cfg.read_text()
    assert tomllib.loads(cfg.read_text())["model_providers"]["keepmum"]["base_url"] == f"{BASE}/gateway/corp/v1"
    provider, extra = clients.codex_run_provider(PORT, {"corp": "https://gw.corp.example"})
    assert provider["base_url"] == f"{BASE}/gateway/corp/v1" and extra == {}
    clients.codex_disable(PORT)
    assert tomllib.loads(cfg.read_text()) == tomllib.loads(original)


def test_set_top_key_replaces_every_copy_with_one_line():
    text = 'model_provider = "a"\nmodel = "x"\nmodel_provider = "b"\n[t]\nmodel_provider = "kept in a table"\n'
    out = clients._set_top_key(text, "model_provider", 'model_provider = "k"')
    assert out == 'model_provider = "k"\nmodel = "x"\n[t]\nmodel_provider = "kept in a table"\n'
    assert clients._set_top_key("[t]\n", "model_provider", "new = 1") == "new = 1\n[t]\n"


def test_toml_values():
    assert clients._toml(True) == "true" and clients._toml(False) == "false"
    assert clients._toml({"a": {"b": 1}}) == '{"a" = {"b" = 1}}'
    assert clients._toml("x") == '"x"'


def test_codex_args_override_each_provider_key(homes):
    args = clients.codex_args(PORT, {"base_url": "http://x", "supports_websockets": False})
    assert args == ["-c", 'model_provider="keepmum"', "-c", 'model_providers.keepmum.base_url="http://x"',
                    "-c", "model_providers.keepmum.supports_websockets=false"]


# --- status helpers ----------------------------------------------------------------------------

def test_status_urls_through_a_gateway_for_every_provider_or_some():
    want = {"anthropic": BASE, "openai": f"{BASE}/openai/v1"}
    gw = {"corp": "https://gw.corp.example"}
    every = clients._status_urls(want, {"anthropic": f"{BASE}/gateway/corp", "openai": f"{BASE}/gateway/corp"}, PORT, gw)
    assert every["via"] == "https://gw.corp.example" and "via_providers" not in every
    assert clients.via_phrase(every) == ", then https://gw.corp.example"
    some = clients._status_urls(want, {"anthropic": f"{BASE}/gateway/gone", "openai": f"{BASE}/openai/v1"}, PORT, gw)
    assert some["via"] == "a gateway no longer in keepmum's config" and some["via_providers"] == ["anthropic"]
    assert clients.via_phrase(some) == "; anthropic then goes to a gateway no longer in keepmum's config"


def test_unpoint_removes_a_route_whose_gateway_is_gone():
    entry = {"baseUrl": f"{BASE}/gateway/gone/v1", "x": 1}
    assert clients._unpoint(entry, "baseUrl", BASE, PORT, {}) and entry == {"x": 1}
    entry = {"baseUrl": f"{BASE}/gateway/corp/v1"}
    assert clients._unpoint(entry, "baseUrl", BASE, PORT, {"corp": "https://gw"}) and entry == {"baseUrl": "https://gw/v1"}
    assert not clients._unpoint({"baseUrl": "https://mine"}, "baseUrl", BASE, PORT, {})


# --- pi ----------------------------------------------------------------------------------------

def test_pi_status_reports_an_unreadable_models_file(homes):
    (homes / "pi" / "models.json").write_text("{nope")
    st = clients.pi_status(PORT)
    assert st["installed"] and not st["on"] and "not valid JSON" in st["error"]


def test_pi_enable_refuses_providers_that_are_not_an_object(homes):
    models = homes / "pi" / "models.json"
    models.write_text(json.dumps({"providers": ["anthropic"]}))
    with pytest.raises(ProtectError, match="providers is not an object"):
        clients.pi_enable(PORT)
    assert json.loads(models.read_text()) == {"providers": ["anthropic"]}


def test_pi_disable_without_a_file_or_with_nothing_of_keepmums(homes):
    models = homes / "pi" / "models.json"
    assert clients.pi_disable(PORT) == {"installed": False, "on": False}
    mine = {"providers": {"openai": {"baseUrl": "https://mine.example/v1"}}}
    models.write_text(json.dumps(mine))
    clients.pi_disable(PORT)
    assert json.loads(models.read_text()) == mine and not (homes / "pi" / "models.json.keepmum-backup").exists()


def test_pi_off_keeps_the_users_own_headers_and_drops_an_emptied_providers(homes):
    models = homes / "pi" / "models.json"
    models.write_text(json.dumps({"providers": {"anthropic": {"headers": {"x-team": "a"}}}, "other": 1}))
    st = clients.pi_enable(PORT)
    assert st["on"] and set(st["providers"]) == {"anthropic", "openai", "openai-codex"}
    assert json.loads(models.read_text())["providers"]["anthropic"]["headers"] == {"x-team": "a", "x-keepmum-client": "pi"}
    assert not clients.pi_disable(PORT)["on"]
    assert json.loads(models.read_text()) == {"providers": {"anthropic": {"headers": {"x-team": "a"}}}, "other": 1}
    models.write_text(json.dumps({"other": 1}))
    clients.pi_enable(PORT)
    clients.pi_disable(PORT)
    assert json.loads(models.read_text()) == {"other": 1}


# --- opencode ----------------------------------------------------------------------------------

def oc(homes):
    d = homes / "xdg-config" / "opencode"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_opencode_status_reports_invalid_or_odd_config(homes):
    d = oc(homes)
    (d / "config.json").write_text("{nope")
    st = clients.opencode_status(PORT)
    assert not st["on"] and "is not valid JSON" in st["error"]
    (d / "config.json").write_text("[1, 2]")
    assert "does not hold a config object" in clients.opencode_status(PORT)["error"]


def test_opencode_enable_refuses_a_provider_that_is_not_an_object(homes):
    (oc(homes) / "opencode.json").write_text(json.dumps({"provider": []}))
    with pytest.raises(ProtectError, match="provider is not an object"):
        clients.opencode_enable(PORT)


@pytest.mark.parametrize("key", ["provider", "providers"])
def test_an_odd_opencode_provider_is_reported_not_fatal(homes, key):
    (oc(homes) / "opencode.json").write_text(json.dumps({key: "anthropic"}))
    out = clients.protect_clients("status", PORT, ["opencode", "pi"])
    assert f"`{key}` should be an object" in out["opencode"]["error"] and "pi" in out


def test_opencode_enable_leaves_the_users_own_base_url(homes):
    path = oc(homes) / "opencode.json"
    path.write_text(json.dumps({"provider": {"openai": {"options": {"baseURL": "https://mine.example/v1"}}}}))
    st = clients.opencode_enable(PORT)
    data = json.loads(path.read_text())
    assert data["provider"]["openai"] == {"options": {"baseURL": "https://mine.example/v1"}}
    assert data["provider"]["anthropic"]["options"]["baseURL"] == f"{BASE}/v1"
    assert st["providers"] == ["anthropic"] and st["other"] == ["openai"]


def test_opencode_disable_cases(homes):
    path = oc(homes) / "opencode.json"
    assert clients.opencode_disable(PORT) == {"installed": False, "on": False}
    path.write_text('{"provider": {} // comment\n}')
    with pytest.raises(ProtectError, match="not plain JSON"):
        clients.opencode_disable(PORT)
    mine = {"provider": {"openai": {"options": {"baseURL": "https://mine/v1"}}, "anthropic": "odd"}}
    path.write_text(json.dumps(mine))
    clients.opencode_disable(PORT)
    assert json.loads(path.read_text()) == mine


def test_opencode_off_keeps_the_users_headers_and_drops_an_emptied_provider(homes):
    path = oc(homes) / "opencode.json"
    path.write_text(json.dumps({"provider": {"anthropic": {"options": {"headers": {"x-team": "a"}}}}}))
    clients.opencode_enable(PORT)
    clients.opencode_disable(PORT)
    assert json.loads(path.read_text()) == {"provider": {"anthropic": {"options": {"headers": {"x-team": "a"}}}}}
    path.write_text(json.dumps({"$schema": "s"}))
    clients.opencode_enable(PORT)
    clients.opencode_disable(PORT)
    assert json.loads(path.read_text()) == {"$schema": "s"}


def test_opencode_base_from_api_and_2x_settings():
    assert clients._opencode_base({"provider": {"openai": {"api": "https://a"}}}, "openai") == "https://a"
    assert clients._opencode_base({"providers": {"openai": {"settings": {"baseURL": "https://b"}}},
                                   "provider": {"openai": {"api": "https://a"}}}, "openai") == "https://b"
    assert clients._opencode_base({"providers": {"openai": "odd"}}, "openai") is None


def test_opencode_major_reads_and_caches_the_version(homes, monkeypatch):
    exe = homes / "opencode"
    exe.write_text("#!/bin/sh\necho 'opencode 2.4.1'\n")
    exe.chmod(0o755)
    monkeypatch.setattr(clients.shutil, "which", lambda name: str(exe) if name == "opencode" else None)
    assert clients.opencode_major() == 2
    import os

    st = os.stat(exe)
    exe.write_text("#!/bin/sh\necho 'opencode 9.9.9'\n")
    os.utime(exe, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime: the cached answer
    assert clients.opencode_major() == 2
    os.utime(exe, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert clients.opencode_major() == 9


def test_opencode_major_without_a_version_or_a_binary(homes, monkeypatch):
    exe = homes / "opencode"
    exe.write_text("#!/bin/sh\necho 'dev build'\n")
    exe.chmod(0o755)
    monkeypatch.setattr(clients.shutil, "which", lambda name: str(exe))
    assert clients.opencode_major() is None
    monkeypatch.setattr(clients.shutil, "which", lambda name: str(homes / "gone"))
    assert clients.opencode_major() is None  # os.stat fails
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    assert clients.opencode_major() is None


def test_opencode_chatgpt_login_reads_auth_json(homes):
    auth = homes / "xdg-data" / "opencode" / "auth.json"
    assert not clients._opencode_chatgpt_login()
    auth.parent.mkdir(parents=True)
    auth.write_text("{bad")
    assert not clients._opencode_chatgpt_login()
    auth.write_text(json.dumps({"openai": {"type": "api"}}))
    assert not clients._opencode_chatgpt_login()
    auth.write_text(json.dumps({"openai": {"type": "oauth"}}))
    assert clients._opencode_chatgpt_login()


# --- all clients ---------------------------------------------------------------------------------

def test_protect_clients_reports_an_error_and_a_client_going_around_keepmum(homes, monkeypatch):
    (homes / "codex" / "config.toml").write_text("[broken\n")
    (homes / "pi" / "models.json").write_text(json.dumps({"providers": {"openai": {"baseUrl": "https://gw.corp.example/v1"}}}))
    out = clients.protect_clients("on", PORT, ["codex", "pi", "opencode"])
    assert not out["codex"]["on"] and "fix it first" in out["codex"]["error"]
    assert out["pi"]["around"] == "https://gw.corp.example/v1"  # straight to a gateway keepmum is in front of
    assert out["opencode"] == {"installed": False, "on": False}
