"""stats.py: the history file's compaction and unreadable lines, the stats token, providers and
clients by name, and the less common events in the flow diagram."""

import json
import time

from keepmum import stats
from keepmum.stats import History, Stats, event_kind, flows, provider_of


def test_stats_token_is_made_once_and_read_back(tmp_path):
    p = tmp_path / "cfg" / "stats-token"
    token = stats.stats_token(p)
    assert len(token) > 30 and p.read_text() == token and (p.stat().st_mode & 0o777) == 0o600
    assert stats.stats_token(p) == token


def test_stats_token_written_by_another_process_meanwhile_is_used(tmp_path, monkeypatch):
    p = tmp_path / "stats-token"
    real_open = stats.os.open

    def racing_open(path, flags, mode=0o777):
        if path == p and flags & stats.os.O_EXCL:
            p.write_text("theirs\n")  # another keepmum made it between the check and the open
        return real_open(path, flags, mode)

    monkeypatch.setattr(stats.os, "open", racing_open)
    assert stats.stats_token(p) == "theirs"


def test_stats_token_default_path_is_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert stats.token_file() == tmp_path / ".config" / "keepmum" / "stats-token"
    assert stats.history_file() == tmp_path / ".config" / "keepmum" / "history.jsonl"


def test_history_compacts_to_its_newest_events(tmp_path):
    h = History(tmp_path / "history.jsonl", keep=400)
    for i in range(1000):  # the 1,000th append compacts: 1,000 lines is over 1.5 x 400
        h.append({"ts": i, "n": i})
    events = h.events()
    assert len(events) == 400 and events[0]["n"] == 600 and events[-1]["n"] == 999
    assert (h.path.stat().st_mode & 0o777) == 0o600 and not h.path.with_suffix(".tmp").exists()


def test_history_under_the_limit_is_not_compacted(tmp_path):
    h = History(tmp_path / "history.jsonl", keep=1000)
    for i in range(1000):
        h.append({"ts": i})
    assert len(h.events()) == 1000


def test_history_that_cannot_be_written_or_read_is_quiet(tmp_path):
    (tmp_path / "blocker").write_text("")
    h = History(tmp_path / "blocker" / "history.jsonl")
    h.append({"ts": 1})  # no exception
    assert h.events() == [] and h.page() == []


def test_history_skips_unreadable_lines(tmp_path):
    p = tmp_path / "history.jsonl"
    p.write_text('{"ts": 1, "outcome": "forwarded"}\nnot json\n{"ts": 2, "outcome": "blocked"}\n')
    h = History(p)
    assert [e["ts"] for e in h.events()] == [1, 2]
    assert [e["ts"] for e in h.page()] == [2, 1]
    assert [e["ts"] for e in h.page(outcome="blocked")] == [2]
    assert [e["ts"] for e in h.page(before=2)] == [1]


def test_provider_of_names_google_and_gateways():
    assert provider_of("https://generativelanguage.googleapis.com/v1beta") == "google"
    assert provider_of("https://chatgpt.com/backend-api") == "openai"
    assert provider_of("http://127.0.0.1:3425/v1") == "127.0.0.1:3425"
    assert provider_of("https://gw.corp.example:8443/v1") == "gw.corp.example"
    assert provider_of("http://localhost:99999") == "localhost"  # a port out of range is left out
    assert provider_of("not a url") == "unknown"


def test_client_names():
    assert stats.client_of("GeminiCLI/0.66.0 (darwin)") == "gemini"
    assert stats.client_of("curl/8.4") == "curl" and stats.client_of("   ") == "unknown"
    assert stats.client_name("unknown") == "Other" and stats.client_name("curl") == "curl"
    assert stats.client_name("antigravity") == "Antigravity CLI"


def test_event_kind_for_each_outcome():
    assert event_kind({"outcome": "flagged"}) == "flagged"
    assert event_kind({"outcome": "local"}) == "local"
    assert event_kind({"outcome": "rejected"}) == "blocked"
    assert event_kind({"outcome": "observed"}) == "observed"
    assert event_kind({"outcome": "forwarded", "new": 2}) == "masked"
    assert event_kind({"outcome": "forwarded"}) == "clean"


def test_flows_skip_old_and_bodyless_events_and_count_failures():
    events = [
        {"ts": 1, "outcome": "forwarded", "scan_ms": 1.0},  # before `since`
        {"ts": 10, "outcome": "forwarded", "scan_ms": None, "client": "codex"},  # /api/hello: no body
        {"ts": 10, "outcome": "upstream_error", "client": "codex", "provider": "openai"},
        {"ts": 10, "outcome": "observed", "scan_ms": 2.0, "client": "pi", "provider": "openai"},
        {"ts": 10, "outcome": "blocked", "scan_ms": 2.0},
    ]
    f = flows(events, since=5)
    assert f["out"] == [{"client": "claude-code", "provider": None, "kind": "blocked", "n": 1},
                        {"client": "codex", "provider": None, "kind": "failed", "n": 1},
                        {"client": "pi", "provider": "openai", "kind": "observed", "n": 1}]
    assert {c["id"]: c["sent"] for c in f["clients"]} == {"codex": 1, "pi": 1, "claude-code": 1}
    assert f["providers"] == [{"id": "openai", "name": "OpenAI", "replies": 1}] and f["checks"] == {}


def test_a_warning_for_a_request_not_counted_moves_nothing():
    f = flows([{"ts": 10, "outcome": "flagged", "kind": "clean", "guard": [{"check": "harm"}]}], since=0)
    assert f["out"] == [] and f["checks"] == {}


def test_a_stale_flight_is_no_longer_drawn(monkeypatch):
    s = Stats()
    old = s.depart(("codex", "openai"), "clean")
    s.inflight[old]["ts"] -= stats.FLIGHT_STALE + 1
    new = s.depart(("pi", "anthropic"), "masked", handed_back="summary")
    live = s.live()
    assert [f["id"] for f in live["inflight"]] == [new] and live["inflight"][0]["kind"] == "handed"


def test_record_without_origin_and_flagged_without_request_or_origin(tmp_path):
    h = History(tmp_path / "h.jsonl")
    s = Stats(history=h)
    s.record("/v1/messages", "forwarded", 200, scan_ms=3.0)
    s.record_flagged("/v1/messages", [{"check": "injection", "score": 0.91234, "source": "Read"}])
    ev = h.events()
    assert "client" not in ev[0] and "client" not in ev[1] and "request" not in ev[1]
    assert ev[1]["guard"] == [{"check": "injection", "action": "warned", "source": "Read", "score": 0.912,
                               "categories": None}]
    assert ev[1]["outcome"] == "flagged" and ev[1]["kind"] == "clean"


def test_check_texts_go_when_their_hits_are_dropped():
    s = Stats(recent=2)
    hit = {"check": "injection", "score": 0.9, "chars": 50}
    for i in range(3):
        s.record_guard([hit], "warned", shown=[(f"text {i}", None)])
    kept = s.check_texts_page()
    assert sorted(kept) == ["2", "3"] and kept["3"] == {"text": "text 2", "prompt": None}
    assert [h["seq"] for h in s.snapshot()["guard"]["hits"]] == [2, 3]


def test_snapshot_percentiles_and_timeline():
    s = Stats()
    for ms in (1.0, 2.0, 3.0, 4.0):
        s.record("/v1/messages", "forwarded", 200, scan_ms=ms, redacted={"email": 1}, new=1)
    s.record("/v1/messages", "rejected", 400)
    snap = s.snapshot(extra=1)
    assert snap["scan_ms"] == {"p50": 3.0, "p95": 4.0, "n": 4} and snap["extra"] == 1
    assert snap["funnel"]["clean"] == 0 and snap["funnel"]["masked"] == 4
    assert snap["timeline"][-1]["blocked"] == 1 and snap["timeline"][-1]["masked"] == {"email": 4}
    assert Stats().snapshot()["scan_ms"] == {"p50": None, "p95": None, "n": 0}
    assert time.time() - snap["started"] < 60
    json.dumps(snap)  # the feed is JSON
