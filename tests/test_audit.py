import json
import multiprocessing
import re
import secrets
import time

import anyio
import httpx

from keepmum import selftest
from keepmum.audit import GENESIS, AuditLog, config_changes, config_state, entry_hash, read, verify
from keepmum.config import Config
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.proxy import Proxy
from keepmum.redact import Redactor
from keepmum.restore import DEFAULT_POLICY
from keepmum.vault import Vault


def lines(path):
    return path.read_text().splitlines()


def write(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def test_entries_chain_and_verify(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append("request", outcome="forwarded", n=i)
    entries = read(log.path)
    assert [e["seq"] for e in entries] == [1, 2, 3, 4, 5]
    assert entries[0]["prev"] == GENESIS
    assert all(b["prev"] == a["hash"] for a, b in zip(entries, entries[1:]))
    assert all(entry_hash(e) == e["hash"] for e in entries)
    result = verify(log.path)
    assert result["ok"] and result["entries"] == 5 and result["last"]["seq"] == 5


def test_an_edited_entry_is_caught(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for _ in range(3):
        log.append("request", outcome="blocked")
    entries = read(log.path)
    entries[1]["outcome"] = "forwarded"  # someone hides a block
    write(log.path, entries)
    result = verify(log.path)
    assert not result["ok"] and any("seq 2" in p and "edited" in p for p in result["problems"])


def test_an_edit_with_its_hash_recomputed_breaks_the_next_link(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for _ in range(3):
        log.append("config", weakens=True)
    entries = read(log.path)
    entries[1]["weakens"] = False
    entries[1]["hash"] = entry_hash(entries[1])
    write(log.path, entries)
    result = verify(log.path)
    assert not result["ok"] and any(p.startswith("seq 3: does not follow") for p in result["problems"])


def test_a_removed_entry_is_caught(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for _ in range(4):
        log.append("request")
    write(log.path, [e for e in read(log.path) if e["seq"] != 2])
    problems = verify(log.path)["problems"]
    assert any("seq 3" in p for p in problems)


def test_a_cut_off_end_or_start_is_caught(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for _ in range(4):
        log.append("request")
    entries = read(log.path)
    write(log.path, entries[:2])
    assert any("ends at seq 2" in p and "up to seq 4" in p for p in verify(log.path)["problems"])
    write(log.path, entries[2:])
    assert any("starts at seq 3" in p for p in verify(log.path)["problems"])


def test_a_full_rewrite_is_caught_only_by_an_anchor(tmp_path):
    """The limit the docs state: rewriting every hash from some point passes the chain check.
    A head kept elsewhere catches it."""
    log = AuditLog(tmp_path / "audit.jsonl")
    for _ in range(3):
        log.append("protect", action="off")
    head = read(log.path)[-1]
    anchor = f"{head['seq']}:{head['hash'][:16]}"
    assert verify(log.path, anchor)["ok"]
    rewritten, prev = [], GENESIS
    for e in read(log.path):
        e = {**e, "action": "on", "prev": prev}
        e["hash"] = entry_hash(e)
        rewritten.append(e)
        prev = e["hash"]
    write(log.path, rewritten)
    log._write_head({"seq": 3, "hash": prev})
    assert verify(log.path)["ok"]
    result = verify(log.path, anchor)
    assert not result["ok"] and any("different hash" in p for p in result["problems"])


def test_pruning_keeps_the_chain_checkable(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", keep_days=30)
    for _ in range(3):
        log.append("request")
    entries = read(log.path)
    old = time.time() - 40 * 86400
    for e in entries[:2]:  # two entries from 40 days ago
        e["ts"] = old
    prev = GENESIS
    for e in entries:
        e["prev"] = prev
        e["hash"] = entry_hash(e)
        prev = e["hash"]
    write(log.path, entries)
    assert log.prune() == 2
    kept = read(log.path)
    assert [e["seq"] for e in kept] == [3, 4] and kept[-1]["event"] == "pruned"
    assert kept[-1]["through"] == {"seq": 2, "hash": entries[1]["hash"]}
    assert verify(log.path)["ok"]


def test_pruning_all_entries_keeps_monotonic_seq_and_anchor(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", keep_days=30)
    for _ in range(3):
        log.append("request")
    entries = read(log.path)
    old = time.time() - 40 * 86400
    prev = GENESIS
    for e in entries:
        e["ts"] = old
        e["prev"] = prev
        e["hash"] = entry_hash(e)
        prev = e["hash"]
    write(log.path, entries)
    log._write_head({"seq": 3, "hash": prev})
    anchor = f"3:{prev[:16]}"
    assert log.prune() == 3
    kept = read(log.path)
    assert len(kept) == 1 and kept[0]["seq"] == 4 and kept[0]["event"] == "pruned"
    assert kept[0]["prev"] == prev and kept[0]["through"] == {"seq": 3, "hash": prev}
    assert verify(log.path, anchor)["ok"]
    log.append("request")
    assert [e["seq"] for e in read(log.path)] == [4, 5]
    assert verify(log.path, anchor)["ok"]


def test_a_torn_last_line_is_reported_and_appends_go_on(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("request")
    with open(log.path, "a") as f:
        f.write('{"seq": 2, "ts": 1')  # a crash mid-write
    log.append("stop")
    entries = read(log.path)
    assert [e["seq"] for e in entries] == [1, 2]
    assert entries[1]["prev"] == entries[0]["hash"]
    assert any("line 2" in p for p in verify(log.path)["problems"])


def _append_many(path, n):
    log = AuditLog(path)
    for _ in range(n):
        log.append("request")


def test_processes_appending_at_once_keep_one_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_many, args=(path, 40)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    result = verify(path)
    assert result["ok"] and result["entries"] == 160


def state(**kw):
    return config_state(Config(**kw))


def test_config_changes_say_what_weakens():
    base = state()
    weak = {c["setting"]: c["weakens"] for c in config_changes(base, state(observe=True, model=False, pass_types=["PERSON"],
                                                                               off_rules=["key-in-prose"]))}
    assert weak == {"detect.observe": True, "detect.model": True, "detect.pass": True, "rules.off": True}
    back = config_changes(state(observe=True), base)
    assert back == [{"setting": "detect.observe", "from": True, "to": False, "weakens": False}]
    restore = config_changes(base, state(restore={**DEFAULT_POLICY, "WebFetch": "restore"}))
    assert restore == [{"setting": "restore.WebFetch", "from": "keep", "to": "restore", "weakens": True}]
    shorter = config_changes(base, state(audit_keep_days=7))
    assert shorter == [{"setting": "audit.keep_days", "from": 90, "to": 7, "weakens": True}]


def test_values_in_settings_are_fingerprints():
    secret = "hunter2-staging-password"
    s = state(allow_strings=[secret], mask_rules=[{"name": "Employee ID", "pattern": r"EMP-\d{6}", "type": "ACCOUNT"}])
    text = json.dumps(s)
    assert secret not in text and r"EMP-\d{6}" not in text
    assert s["mask_rules"][0].startswith("Employee ID (ACCOUNT) #")
    changes = config_changes(state(), s)
    assert {c["setting"]: c["weakens"] for c in changes} == {"rules.mask": False, "allowlist.strings": True}


def test_note_config_records_a_baseline_then_only_changes(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    first = log.note_config(Config(), "keepmum serve")
    assert first["baseline"] and first["changes"] == [] and not first["weakens"]
    assert log.note_config(Config(), "config file") is None  # another process saw the same file
    entry = log.note_config(Config(observe=True), "keepmum observe on")
    assert entry["weakens"] and entry["by"] == "keepmum observe on"
    assert log.note_config(Config(observe=True), "config file") is None
    assert verify(log.path)["ok"]


def _tool_upstream(request: httpx.Request) -> httpx.Response:
    """A provider that asks to run the masked value through Bash, and to fetch it with WebFetch."""
    if request.url.path != "/v1/messages":
        return httpx.Response(200, json={"data": []})
    ph = re.search(r"KEEPMUM_SECRET_[0-9a-f]{8}", request.content.decode()).group(0)
    return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant", "content": [
        {"type": "text", "text": f"Using {ph}"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": f"deploy --token {ph}"}},
        {"type": "tool_use", "id": "t2", "name": "WebFetch", "input": {"url": f"https://example.test/?k={ph}"}},
    ]})


def test_requests_are_audited_without_values(tmp_path):
    canary = selftest.make_canary()
    log = AuditLog(tmp_path / "audit.jsonl")

    async def go():
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(_tool_upstream), base_url="http://up")
        proxy = Proxy(Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32))), upstream="http://up",
                      client=upstream, audit=log)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
        body = {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": f"export GITHUB_TOKEN={canary}"}]}
        r = await client.post("/v1/messages", json=body, headers={"user-agent": "claude-cli/2.1"})
        assert r.status_code == 200 and canary in r.text  # Bash got the real value back
        await client.post("/v1/files", content=b"x")  # an unscanned endpoint: blocked
        await client.get("/v1/models")  # no content: not audited

    anyio.run(go)
    text = log.path.read_text()
    assert canary not in text and "KEEPMUM_" not in text
    req, blocked = read(log.path)
    assert req["event"] == "request" and req["client"] == "claude-code" and req["outcome"] == "forwarded"
    assert req["masked"] == {"SECRET": 1} and req["new"] == 1 and req["found"] == {"github-token": 1}
    assert req["tools"] == [{"tool": "Bash", "action": "restore", "values": 1, "reason": "local", "types": {"SECRET": 1}},
                            {"tool": "WebFetch", "action": "keep", "values": 1, "reason": "placeholders", "types": {"SECRET": 1}}]
    assert blocked["outcome"] == "blocked" and blocked["path"] == "/v1/files"
    assert verify(log.path)["ok"]


def test_cli_records_changes_and_never_their_values(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from keepmum.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KEEPMUM_CALLER", "Keepmum.app")
    run = lambda *a: CliRunner().invoke(app, list(a))
    assert run("observe", "on").exit_code == 0
    assert run("rules", "allow", "hunter2-staging-password").exit_code == 0
    assert run("observe", "off").exit_code == 0
    path = tmp_path / ".config" / "keepmum" / "audit.jsonl"
    assert "hunter2" not in path.read_text()
    entries = read(path)
    # The first change is the baseline, compared with the defaults, so it is still flagged.
    assert [(bool(e.get("baseline")), e["weakens"]) for e in entries] == [(True, True), (False, True), (False, False)]
    assert entries[0]["by"] == "keepmum observe on (from Keepmum.app)"
    assert entries[1]["by"] == "keepmum rules allow (from Keepmum.app)"
    out = run("audit", "list", "--weakening")
    assert out.exit_code == 0 and "! config   settings recorded; detect.observe false -> true  [keepmum observe on" in out.output
    assert run("audit", "verify").exit_code == 0
    head = run("audit", "head").output.strip()
    assert run("audit", "verify", "--anchor", head).exit_code == 0
    export = tmp_path / "export.jsonl"
    assert run("audit", "export", "--since", "1d", "-o", str(export)).exit_code == 0
    assert run("audit", "verify", "--file", str(export)).exit_code == 0
    assert run("audit", "export", "--format", "csv").output.startswith("seq,time_utc,event,weakens")


def test_a_running_proxy_records_start_stop_and_edits_of_the_file(tmp_path, monkeypatch):
    from keepmum import config as config_mod
    from keepmum import launch

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[detect]\npii = true\n")
    proxy = launch.build_proxy(config_mod.load(cfg_path), rules_only=True, upstream="http://up")
    server = launch.BackgroundServer(proxy)
    server.start()
    cfg_path.write_text("[detect]\npii = true\nobserve = true\n")  # edited by hand, not with keepmum
    assert proxy._observe()  # what the next request does: re-read the file
    server.stop()
    entries = read(tmp_path / ".config" / "keepmum" / "audit.jsonl")
    assert [e["event"] for e in entries] == ["config", "start", "config", "stop"]
    assert entries[0]["baseline"] and not entries[0]["weakens"]  # pii on is stronger than the default
    assert entries[1]["mode"] == "serve" and entries[1]["port"] == server.port
    assert entries[2]["by"] == "config file" and entries[2]["weakens"]
    assert verify(tmp_path / ".config" / "keepmum" / "audit.jsonl")["ok"]
