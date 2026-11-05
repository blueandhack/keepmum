"""The audit log: a tamper-evident record of what keepmum did and of every change to its protection.

~/.config/keepmum/audit.jsonl (0600), one JSON entry per line, shared by every keepmum process
on this Mac. Each entry carries `seq` (one more than the entry before), `prev` (the hash of the
entry before) and `hash`: SHA-256 of the entry's canonical JSON without `hash`. Editing,
inserting, reordering or deleting an entry breaks the chain from there on, and
`keepmum audit verify` says where.

What is recorded, and what never is:

- request: who sent it (client), where it went (provider, path), what keepmum did (outcome,
  status), counts by type, which detectors found what in new text, and which tools got real
  values back or kept placeholders, and how many placeholders stayed inside web addresses in the
  reply. Never a value, a preview, a placeholder or a header.
- config: every change to a protection setting, with whether it weakens protection, and who
  made it (the keepmum command, the app, or an edit of the file). Allowlist entries and your
  own patterns are recorded as keyed fingerprints, since they may be values themselves.
- protect, exclude, include: an agent or a project taken out of keepmum's path, or put back.
- start, stop: a keepmum proxy began or ended, so gaps in coverage show.
- pruned: entries older than `[audit] keep_days` were removed; the chain continues from them.

What the chain cannot do on its own: someone who can write the file can also rewrite it from
some point on, recomputing every hash, or cut off its end. Both are caught only by comparing
with a hash kept elsewhere: `keepmum audit head` prints the latest one, `audit verify --anchor`
checks the log still contains it, and the app keeps one. A keyed signature would not help: the
key would sit in a file readable by the same user (the Keychain cannot be shared by keepmum's
processes without a prompt).
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path

GENESIS = "0" * 64
KEEP_DAYS = 90


def audit_dir() -> Path:
    return Path.home() / ".config" / "keepmum"


def audit_file() -> Path:
    return audit_dir() / "audit.jsonl"


def _canonical(entry: dict) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogatepass")


def entry_hash(entry: dict) -> str:
    """The hash of an entry: SHA-256 of its canonical JSON, without its own `hash`."""
    return hashlib.sha256(_canonical({k: v for k, v in entry.items() if k != "hash"})).hexdigest()


def caller(command: str) -> str:
    """Who made a change: the keepmum command (never its arguments, which may be values), and the
    app when it ran the command for you."""
    via = os.environ.get("KEEPMUM_CALLER", "").strip()
    return f"{command} (from {via})" if via else command


class AuditLog:
    """Appends to the audit log. Never raises: auditing must not stop a request, so a failure to
    write is logged and the request goes on (the gap then shows in `audit verify` as missing seq
    numbers or a missing stop)."""

    def __init__(self, path: Path | None = None, keep_days: int = KEEP_DAYS):
        self.path = path or audit_file()
        self.keep_days = keep_days
        self._lock = threading.Lock()

    @property
    def head_path(self) -> Path:
        return self.path.with_name(self.path.stem + ".head.json")

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.stem + ".lock")

    # --- Writing --------------------------------------------------------------------

    def append(self, event: str, **fields) -> dict | None:
        try:
            with self._locked():
                return self._append(event, fields)
        except OSError as e:
            import logging

            logging.getLogger("keepmum").error("audit log not written (%s): %s", type(e).__name__, e)
            return None

    def _append(self, event: str, fields: dict, state: dict | None = None, after: dict | None = None) -> dict:
        last = after if after is not None else self._last()
        entry = {"seq": (last["seq"] + 1) if last else 1, "ts": round(time.time(), 3), "event": event,
                 **{k: v for k, v in fields.items() if v is not None}, "prev": last["hash"] if last else GENESIS}
        entry["hash"] = entry_hash(entry)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as f:
            if f.tell() and not self._ends_with_newline():
                f.write(b"\n")  # a torn last line (a crash mid-write) stays where it is, on its own line
            f.write(_canonical(entry) + b"\n")
        head = self._head()
        self._write_head({"seq": entry["seq"], "hash": entry["hash"], "ts": entry["ts"],
                          "config": state if state is not None else head.get("config")})
        return entry

    def note_config(self, cfg, by: str) -> dict | None:
        """Record what changed in the protection settings since the last recorded state. Every
        keepmum process calls this when it sees the file change; only the first records it."""
        try:
            with self._locked():
                return self._note_config_unlocked(cfg, by)
        except OSError:
            return None

    def _note_config_unlocked(self, cfg, by: str) -> dict | None:
        state = config_state(cfg)
        old = self._head().get("config")
        if old == state:
            return None
        # The first record compares with keepmum's defaults: what was turned down before
        # the log began still shows.
        changes = config_changes(old if old is not None else config_state(_defaults()), state)
        if old is None:
            return self._append("config", {"by": by, "baseline": True, "state": state, "changes": changes,
                                           "weakens": any(c["weakens"] for c in changes)}, state)
        return self._append("config", {"by": by, "changes": changes,
                                       "weakens": any(c["weakens"] for c in changes)}, state)

    def prune(self, now: float | None = None) -> int:
        """Drop entries older than keep_days, then record how many and the hash they ended with,
        so the chain still checks from the first kept entry. Returns how many were dropped."""
        cutoff = (now or time.time()) - self.keep_days * 86400
        try:
            with self._locked():
                lines = self.path.read_bytes().splitlines() if self.path.exists() else []
                drop = 0
                for line in lines:
                    try:
                        if json.loads(line)["ts"] >= cutoff:
                            break
                    except (ValueError, KeyError, TypeError):
                        break  # never prune past something unreadable: verify must still see it
                    drop += 1
                if not drop:
                    return 0
                last = json.loads(lines[drop - 1])
                tmp = self.path.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(b"".join(line + b"\n" for line in lines[drop:]))
                tmp.replace(self.path)
                self._append("pruned", {"dropped": drop, "through": {"seq": last["seq"], "hash": last["hash"]},
                                        "keep_days": self.keep_days}, after=None if lines[drop:] else last)
                return drop
        except OSError:
            return 0

    # --- Plumbing -------------------------------------------------------------------

    def _locked(self):
        log = self

        class _Lock:
            def __enter__(self):
                log._lock.acquire()
                try:
                    log.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    self.fd = os.open(log.lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
                    fcntl.flock(self.fd, fcntl.LOCK_EX)
                except BaseException:
                    log._lock.release()
                    raise

            def __exit__(self, *exc):
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                    os.close(self.fd)
                finally:
                    log._lock.release()

        return _Lock()

    def _last(self) -> dict | None:
        """The last whole entry in the file: what the next one chains to."""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read()
        except FileNotFoundError:
            return None
        for line in reversed(tail.splitlines()):
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and "hash" in entry and isinstance(entry.get("seq"), int):
                    return entry
            except ValueError:
                continue
        return None

    def _ends_with_newline(self) -> bool:
        with open(self.path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"

    def _head(self) -> dict:
        try:
            head = json.loads(self.head_path.read_text())
            return head if isinstance(head, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_head(self, head: dict) -> None:
        tmp = self.head_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(head, f, separators=(",", ":"))
        tmp.replace(self.head_path)


# --- Protection settings ------------------------------------------------------------


def _fingerprint(kind: str, value: str) -> str:
    """A keyed fingerprint of something that may be a value (an allowlist entry, a pattern): it
    shows that an entry was added or removed, and which, without what it says."""
    from .vault import install_salt

    return hmac.new(install_salt(), f"audit\0{kind}\0{value}".encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:12]


def _defaults():
    from .config import Config

    return Config()


def config_state(cfg) -> dict:
    """The settings that decide what is masked and where it goes, with nothing that is a value."""
    return {
        "observe": cfg.observe, "model": cfg.model, "pii": cfg.pii,
        "pass": sorted(cfg.pass_types), "rules_off": sorted(cfg.off_rules),
        "mask_rules": sorted(f"{r['name']} ({r['type']}) #{_fingerprint('pattern', r['pattern'])}" for r in cfg.mask_rules),
        "allow_strings": sorted(_fingerprint("string", s) for s in cfg.allow_strings),
        "allow_patterns": sorted(_fingerprint("pattern", p) for p in cfg.allow_patterns),
        "restore": dict(sorted(cfg.restore.items())), "upstream": cfg.upstream,
        "gateways": sorted(f"{name} = {url}" for name, url in cfg.gateways.items()),
        "destinations": sorted(f"{rule} = {h}" for rule, hosts in cfg.destinations.items() for h in hosts),
        "previews": cfg.log_previews, "context": cfg.log_context, "keep_days": cfg.audit_keep_days,
        "guard": cfg.guard_injection, "harm": cfg.guard_harm, "harm_model": cfg.guard_harm_model,
        "commands": cfg.guard_commands,
    }


# For each setting: (name shown, which way weakens protection). For a list, "added" means entries
# added to it weaken (types let through, rules turned off, exceptions); "removed" means entries
# removed from it do (your own mask rules).
_SETTINGS = {
    "observe": ("detect.observe", True), "model": ("detect.model", False), "pii": ("detect.pii", False),
    "pass": ("detect.pass", "added"), "rules_off": ("rules.off", "added"), "mask_rules": ("rules.mask", "removed"),
    "allow_strings": ("allowlist.strings", "added"), "allow_patterns": ("allowlist.patterns", "added"),
    "upstream": ("server.upstream", "any"), "gateways": ("gateways", "added"), "destinations": ("destinations", "added"), "previews": ("log.previews", None), "context": ("log.context", None),
}
# Settings newer than some logs: missing from a recorded state is not a change.
_ADDED_LATER = {"context", "gateways", "destinations", "guard", "harm", "harm_model", "commands"}
_GUARD_RANK = {"off": 0, "warn": 1, "block": 2}


def config_changes(old: dict, new: dict) -> list[dict]:
    """What changed between two config states, each change marked if it weakens protection."""
    out = []
    for key, (name, weak) in _SETTINGS.items():
        a, b = old.get(key), new.get(key)
        if a == b or (key in _ADDED_LATER and key not in old):
            continue
        if isinstance(b, list):
            added = [x for x in b if x not in (a or [])]
            removed = [x for x in (a or []) if x not in b]
            weakens = bool(added) if weak == "added" else bool(removed) if weak == "removed" else False
            out.append({"setting": name, "added": added, "removed": removed, "weakens": weakens})
        else:
            # observe on weakens; model or pii off weakens; any new upstream is flagged for a look.
            weakens = weak == "any" or (isinstance(weak, bool) and b == weak)
            out.append({"setting": name, "from": a, "to": b, "weakens": weakens})
    for key, name in (("guard", "guard.injection"), ("harm", "guard.harm"), ("commands", "guard.commands")):
        if key in old and old[key] != new.get(key):
            # A check that does less than before: block to warn, or either to off.
            out.append({"setting": name, "from": old[key], "to": new.get(key),
                        "weakens": _GUARD_RANK.get(new.get(key), 0) < _GUARD_RANK.get(old[key], 0)})
    if "harm_model" in old and old["harm_model"] != new.get("harm_model"):
        out.append({"setting": "guard.harm_model", "from": old["harm_model"], "to": new.get("harm_model"),
                    "weakens": False})
    if old.get("keep_days") != new.get("keep_days") and "keep_days" in old:
        # A shorter retention removes evidence sooner.
        out.append({"setting": "audit.keep_days", "from": old["keep_days"], "to": new.get("keep_days"),
                    "weakens": (new.get("keep_days") or 0) < old["keep_days"]})
    a, b = old.get("restore") or {}, new.get("restore") or {}
    for tool in sorted(set(a) | set(b)):
        if a.get(tool) != b.get(tool):
            # A tool that now gets real values it did not get before weakens.
            from .restore import rule_for

            # A key added or removed: what the next broader rule ("mcp__*", the default) gave.
            was = rule_for(tool, a)[1]
            now = rule_for(tool, b)[1]
            out.append({"setting": f"restore.{tool}", "from": a.get(tool), "to": b.get(tool),
                        "weakens": _lets_out_more(was, now)})
    return out


def _lets_out_more(was, now) -> bool:
    """Whether restore setting `now` gives a tool real values `was` did not: keep < own < a list
    of types (more types, more values) < restore."""
    def rank(x):
        return 2 if isinstance(x, list) else {"keep": 0, "own": 1, "restore": 3}.get(x, 3)

    if isinstance(was, list) and isinstance(now, list):
        return bool(set(now) - set(was))
    return rank(now) > rank(was)


# --- Reading and checking -------------------------------------------------------------


def read(path: Path | None = None) -> list[dict]:
    """Every readable entry, oldest first (for listing and export; `verify` reads the raw lines)."""
    path = path or audit_file()
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
            if isinstance(entry, dict):
                out.append(entry)
        except ValueError:
            continue
    return out


def verify(path: Path | None = None, anchor: str | None = None, export: bool = False) -> dict:
    """Check the chain. Returns {ok, entries, first, last, problems, head}. `anchor` is
    "<seq>:<hash>" from an earlier `keepmum audit head`: the log must still hold that entry.
    `export`: the file is an `audit export`, which starts and ends wherever its period did."""
    log = AuditLog(path)
    problems: list[str] = []
    try:
        lines = log.path.read_bytes().splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as e:
        return {"ok": False, "entries": 0, "first": None, "last": None, "problems": [f"cannot read the log: {e}"], "head": None}
    prev_hash, prev_seq, first, first_prev, last, n = None, None, None, None, None, 0
    vouched: set[str] = set()  # hashes a "pruned" entry says the dropped part ended with
    by_seq: dict[int, str] = {}
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            seq, h, prev = entry["seq"], entry["hash"], entry["prev"]
        except (ValueError, KeyError, TypeError):
            problems.append(f"line {i}: not a readable entry")
            prev_hash, prev_seq = None, None
            continue
        n += 1
        if entry_hash(entry) != h:
            problems.append(f"seq {seq}: its content does not match its hash (edited)")
        if first is None:
            first, first_prev = {"seq": seq, "hash": h}, prev
        elif prev_hash is not None:
            if prev != prev_hash:
                problems.append(f"seq {seq}: does not follow seq {prev_seq} (an entry was removed, added or changed before it)")
            if seq != prev_seq + 1:
                problems.append(f"seq {seq}: follows seq {prev_seq} (missing or reordered entries)")
        if entry.get("event") == "pruned":
            through = entry.get("through") or {}
            vouched.add(through.get("hash"))
        prev_hash, prev_seq, last = h, seq, {"seq": seq, "hash": h, "ts": entry.get("ts")}
        by_seq[seq] = h
    # A log that starts mid-chain is fine after pruning, which records the hash it cut at.
    if not export and first is not None and first_prev != GENESIS and first_prev not in vouched:
        problems.append(f"the log starts at seq {first['seq']}, and no pruning record covers what came before "
                        "(the start of the log was removed)")
    head = {} if export else log._head()
    if head.get("seq") and last and head["seq"] > last["seq"]:
        problems.append(f"the log ends at seq {last['seq']}, but entries up to seq {head['seq']} were written "
                        "(the end of the log was removed)")
    elif head.get("seq") and not last:
        problems.append(f"the log is empty, but entries up to seq {head['seq']} were written")
    if anchor:
        try:
            a_seq, a_hash = anchor.split(":", 1)
            a_seq = int(a_seq)
        except ValueError:
            problems.append(f"anchor {anchor!r} is not <seq>:<hash>")
        else:
            if by_seq.get(a_seq) is None:
                # Older than the oldest kept entry and covered by a pruning record: retention
                # removed it, which the chain records. It cannot be checked, but nothing is wrong.
                pruned = first is not None and a_seq < first["seq"] and first_prev in vouched
                if not pruned:
                    problems.append(f"anchor seq {a_seq} is not in the log")
            elif len(a_hash) < 12:
                problems.append("anchor hash is too short: give at least 12 characters")
            elif not by_seq[a_seq].startswith(a_hash.lower()):
                problems.append(f"anchor seq {a_seq} has a different hash: the log was rewritten from there or before")
    return {"ok": not problems, "entries": n, "first": first, "last": last, "problems": problems, "head": head or None}


def summary(entry: dict) -> str:
    """One line for `keepmum audit` and the export: what happened, without values. Who did it is
    the entry's `by`; whether it weakened protection, `weakens(entry)`."""
    e = entry.get("event")
    if e == "request":
        masked = sum((entry.get("masked") or {}).values())
        bits = [f"{entry.get('client', '?')} -> {entry.get('provider', '?')}", entry.get("outcome", "?")]
        if masked:
            bits.append(f"masked {masked}" + (f" ({entry['new']} new)" if entry.get("new") else ""))
        tools = entry.get("tools") or []
        kept = [t["tool"] for t in tools if t["action"] == "keep"]
        got = [t["tool"] for t in tools if t["action"] == "restore"]
        if got:
            bits.append("real values to " + ", ".join(got))
        if kept:
            bits.append("placeholders kept for " + ", ".join(kept))
        if entry.get("in_urls"):
            bits.append(f"{sum(entry['in_urls'].values())} kept in web addresses in the reply")
        return "; ".join(bits)
    if e == "config":
        changes = entry.get("changes") or []
        parts = ["settings recorded"] if entry.get("baseline") else []
        for c in changes:
            if "added" in c:
                change = ", ".join([f"+{x}" for x in c["added"]] + [f"-{x}" for x in c["removed"]])
            else:
                change = f"{json.dumps(c.get('from'))} -> {json.dumps(c.get('to'))}"
            # With several changes in one entry, say which of them weakened protection.
            parts.append(f"{c['setting']} {change}" + (" (weakens)" if c["weakens"] and len(changes) > 1 else ""))
        return "; ".join(parts)
    if e == "protect":
        via = entry.get("via") or {}
        return (f"protection {entry.get('action')} for {', '.join(entry.get('clients') or [])}"
                + "".join(f"; {who} through keepmum to {url}" for who, url in sorted(via.items())))
    if e in ("exclude", "include"):
        return f"{entry.get('project')} {'excluded: goes straight to the provider, unscanned' if e == 'exclude' else 'scanned again'}"
    if e == "start":
        return f"keepmum {entry.get('mode')} started (pid {entry.get('pid')}, {entry.get('detector')})"
    if e == "stop":
        return f"keepmum {entry.get('mode')} stopped (pid {entry.get('pid')})"
    if e == "tools":
        changed = entry.get("changed") or []
        return f"{entry.get('server')} trusted again" + (f" after its tools changed ({', '.join(changed)})" if changed else "")
    if e == "pruned":
        return f"{entry.get('dropped')} entries older than {entry.get('keep_days')} days removed"
    if e == "guard":
        hits = entry.get("hits") or []
        if entry.get("check") == "commands":
            cats = sorted({c for h in hits for c in h.get("categories") or []})
            what = f"commands check flagged a {', '.join(sorted({h.get('source') or 'shell' for h in hits}))} command " \
                   f"in the reply ({', '.join(cats)})"
            return (f"{entry.get('client', '?')} -> {entry.get('provider', '?')}; {what}; "
                    + ("the agent got a refusal in its place" if entry.get("action") == "blocked" else "sent with a warning"))
        if entry.get("check") == "harm":
            cats = sorted({c for h in hits for c in h.get("categories") or []})
            what = f"harm check flagged what the user typed ({', '.join(cats) or 'no category'}, " \
                   f"{max(h.get('score', 0) for h in hits):.2f})"
        else:
            what = "possible prompt injection in " + ", ".join(
                f"{h.get('source') or h.get('tool') or 'a tool'} ({h.get('score', 0):.2f})" for h in hits)
        return (f"{entry.get('client', '?')} -> {entry.get('provider', '?')}; {what}; "
                + ("request not sent" if entry.get("action") == "blocked" else "sent with a warning"))
    if e == "guard-model":
        return f"guard model {entry.get('action')}: {entry.get('model')} ({entry.get('licence')})"
    return e or "?"


def weakens(entry: dict) -> bool:
    """Whether an entry records protection turned down."""
    e = entry.get("event")
    if e == "config":
        return bool(entry.get("weakens"))
    if e == "protect":
        return entry.get("action") == "off"
    if e == "tools":  # changed tool definitions trusted: the server gets real values again
        return bool(entry.get("changed"))
    return e == "exclude"
