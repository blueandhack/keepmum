"""audit.py: failures to write never raise, unreadable logs are reported, and every event reads back
as one line without values."""

import json

from keepmum.audit import GENESIS, AuditLog, config_changes, read, summary, verify, weakens
from keepmum.config import Config


def blocked(tmp_path):
    """A log whose folder cannot be made: a file stands where it should be."""
    (tmp_path / "blocker").write_text("")
    return AuditLog(tmp_path / "blocker" / "audit.jsonl")


# --- writing that fails ------------------------------------------------------------------------

def test_an_append_that_cannot_write_returns_none_logs_and_releases_the_lock(tmp_path, caplog):
    log = blocked(tmp_path)
    with caplog.at_level("ERROR", logger="keepmum"):
        assert log.append("request", outcome="forwarded") is None
    assert "audit log not written" in caplog.text
    assert not log._lock.locked()
    assert log.append("stop", mode="proxy") is None  # and the next call does not deadlock


def test_note_config_and_prune_that_cannot_write_do_nothing(tmp_path):
    log = blocked(tmp_path)
    assert log.note_config(Config(), "keepmum test") is None
    assert log.prune() == 0
    assert not log._lock.locked()


def test_prune_never_goes_past_an_unreadable_line(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", keep_days=1)
    log.path.write_text("not json\n" + json.dumps({"seq": 1, "ts": 0, "hash": "h"}) + "\n")
    before = log.path.read_text()
    assert log.prune(now=10 * 86400) == 0
    assert log.path.read_text() == before


def test_an_append_after_only_unreadable_lines_starts_the_chain(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.path.write_text('garbage\n{"seq": "one", "hash": "x"}\n[1, 2]\n{"no": "hash"}\n')
    entry = log.append("start", mode="proxy", pid=1, detector="rules")
    assert entry["seq"] == 1 and entry["prev"] == GENESIS
    assert read(log.path)[-1]["hash"] == entry["hash"]


def test_an_append_chains_to_the_last_whole_entry_past_trailing_junk(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    first = log.append("start", mode="proxy")
    with open(log.path, "a") as f:
        f.write('{"half": \n')
    second = log.append("stop", mode="proxy")
    assert second["seq"] == 2 and second["prev"] == first["hash"]


# --- reading -----------------------------------------------------------------------------------

def test_read_skips_what_is_not_an_entry_and_a_folder_reads_as_empty(tmp_path):
    p = tmp_path / "audit.jsonl"
    p.write_text('{"seq": 1}\n[1, 2]\n"text"\nbroken{\n{"seq": 2}\n')
    assert read(p) == [{"seq": 1}, {"seq": 2}]
    assert read(tmp_path) == []
    assert read(tmp_path / "missing.jsonl") == []


def test_verify_reports_a_log_it_cannot_read(tmp_path):
    folder = tmp_path / "audit.jsonl"
    folder.mkdir()
    r = verify(folder)
    assert not r["ok"] and r["entries"] == 0 and r["problems"][0].startswith("cannot read the log")


def test_verify_skips_blank_lines(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("start", mode="proxy")
    with open(log.path, "a") as f:
        f.write("\n   \n")
    log.append("stop", mode="proxy")
    r = verify(log.path)
    assert r["ok"] and r["entries"] == 2, r["problems"]


def test_verify_an_emptied_log_whose_head_remembers_entries(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for _ in range(3):
        log.append("request", outcome="forwarded")
    log.path.write_text("")
    r = verify(log.path)
    assert not r["ok"] and r["problems"] == ["the log is empty, but entries up to seq 3 were written"]


def test_verify_anchors_that_are_malformed_missing_or_too_short(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    e = log.append("start", mode="proxy")
    assert verify(log.path, anchor=f"1:{e['hash'][:12]}")["ok"]
    assert verify(log.path, anchor=f"1:{e['hash'][:12].upper()}")["ok"]
    assert verify(log.path, anchor="nonsense")["problems"] == ["anchor 'nonsense' is not <seq>:<hash>"]
    assert verify(log.path, anchor="x:abc")["problems"] == ["anchor 'x:abc' is not <seq>:<hash>"]
    assert verify(log.path, anchor=f"9:{e['hash']}")["problems"] == ["anchor seq 9 is not in the log"]
    assert verify(log.path, anchor=f"1:{e['hash'][:6]}")["problems"] == [
        "anchor hash is too short: give at least 12 characters"]


def test_an_export_may_start_mid_chain_and_ignores_the_head(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for _ in range(3):
        log.append("request", outcome="forwarded")
    lines = log.path.read_text().splitlines()
    export = tmp_path / "export.jsonl"
    export.write_text(lines[1] + "\n")
    assert verify(export, export=True)["ok"] and verify(export, export=True)["head"] is None
    assert not verify(export)["ok"]


# --- settings ----------------------------------------------------------------------------------

def test_a_new_harm_model_is_recorded_and_does_not_weaken():
    assert config_changes({"harm_model": "small"}, {"harm_model": "4b"}) == [
        {"setting": "guard.harm_model", "from": "small", "to": "4b", "weakens": False}]
    assert config_changes({}, {"harm_model": "4b"}) == []  # a log older than the setting


# --- one line per event ------------------------------------------------------------------------

def test_request_summary_counts_masked_tools_and_web_addresses():
    line = summary({"event": "request", "client": "claude", "provider": "anthropic", "outcome": "forwarded",
                    "masked": {"email": 2, "key": 1},
                    "tools": [{"tool": "Bash", "action": "restore"}, {"tool": "WebFetch", "action": "keep"}],
                    "in_urls": {"email": 1, "key": 2}})
    assert line == ("claude -> anthropic; forwarded; masked 3; real values to Bash; "
                    "placeholders kept for WebFetch; 3 kept in web addresses in the reply")
    assert summary({"event": "request"}) == "? -> ?; ?"


def test_project_and_process_events():
    assert summary({"event": "exclude", "project": "/p"}) == "/p excluded: goes straight to the provider, unscanned"
    assert summary({"event": "include", "project": "/p"}) == "/p scanned again"
    assert summary({"event": "start", "mode": "proxy", "pid": 7, "detector": "rules"}) == \
        "keepmum proxy started (pid 7, rules)"
    assert summary({"event": "stop", "mode": "proxy", "pid": 7}) == "keepmum proxy stopped (pid 7)"


def test_extension_tool_and_pruning_events():
    assert summary({"event": "extension_removed", "agent": "claude", "kind": "hook", "name": "h",
                    "file": "settings.json"}) == "claude hook h removed from settings.json"
    assert summary({"event": "extensions_kept", "count": 3}) == "3 new item(s) in coding agents' settings kept"
    assert summary({"event": "tools", "server": "github"}) == "github trusted again"
    assert summary({"event": "tools", "server": "github", "changed": ["a", "b"]}) == \
        "github trusted again after its tools changed (a, b)"
    assert summary({"event": "pruned", "dropped": 4, "keep_days": 90}) == "4 entries older than 90 days removed"
    assert summary({"event": "guard-model", "action": "downloaded", "model": "m", "licence": "Apache-2.0"}) == \
        "guard model downloaded: m (Apache-2.0)"
    assert summary({"event": "something-new"}) == "something-new"
    assert summary({}) == "?"


def test_guard_events_for_each_check():
    base = {"event": "guard", "client": "codex", "provider": "openai"}
    cmd = {**base, "check": "commands", "action": "blocked",
           "hits": [{"source": "zsh", "categories": ["exfil", "delete"]}, {"categories": ["delete"]}]}
    assert summary(cmd) == ("codex -> openai; commands check flagged a shell, zsh command in the reply "
                            "(delete, exfil); the agent got a refusal in its place")
    assert summary({**cmd, "action": "warned"}).endswith("; sent with a warning")
    script = {**base, "check": "script", "hits": [{"categories": ["secret"]}]}
    assert summary(script) == ("codex -> openai; script check flagged a Codex script in the reply (secret): "
                               "values keepmum could not check; sent with a note to the user")
    harm = {**base, "check": "harm", "action": "local", "hits": [{"score": 0.91, "categories": ["Violent"]},
                                                                 {"score": 0.97}]}
    assert summary(harm) == ("codex -> openai; harm check flagged what the user typed (Violent, 0.97); "
                             "answered by the local model, not sent")
    assert "(no category, 0.50)" in summary({**base, "check": "harm", "hits": [{"score": 0.5}]})
    inj = {**base, "check": "injection", "action": "blocked",
           "hits": [{"source": "Read", "score": 0.9}, {"tool": "Bash", "score": 0.85}, {}]}
    assert summary(inj) == ("codex -> openai; possible prompt injection in Read (0.90), Bash (0.85), a tool (0.00); "
                            "request not sent")


def test_weakens_by_event():
    assert weakens({"event": "config", "weakens": True}) and not weakens({"event": "config"})
    assert weakens({"event": "protect", "action": "off"}) and not weakens({"event": "protect", "action": "on"})
    assert weakens({"event": "tools", "changed": ["x"]}) and not weakens({"event": "tools", "changed": []})
    assert weakens({"event": "exclude"}) and not weakens({"event": "include"})
    assert not weakens({"event": "request"})
