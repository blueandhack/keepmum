"""The vault under long use: bounded, and its known-values pass cheap at any size (vault.py)."""

import random
import string

from keepmum import vault as vault_mod
from keepmum.vault import Vault


def _token(rng: random.Random) -> str:
    return "ghp_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


def _found(v: Vault, text: str) -> list[str]:
    known = v.known_values()
    return sorted(m.group() for m in known.finditer(text)) if known else []


def test_every_value_is_found_however_many_there_are():
    rng = random.Random(1)
    v = Vault(b"k" * 32)
    values = [_token(rng) for _ in range(3000)]
    for value in values:
        v.placeholder("SECRET", value)
    some = rng.sample(values, 50)
    text = " ".join(f"x={s};" for s in some) + " ghp_notone"
    assert _found(v, text) == sorted(some)
    # the levels: a few patterns, not one per value
    assert len(v._levels) <= 4 and sum(len(level.values) for level in v._levels) == 3000


def test_a_longer_value_wins_where_one_is_a_prefix_of_another():
    v = Vault(b"k" * 32)
    for value in ("Anna", "Anna Smith", "Annabel"):
        v.placeholder("PERSON", value)
    assert _found(v, "Anna Smith met Annabel; Anna left. Annapolis stays.") == ["Anna", "Anna Smith", "Annabel"]


def test_the_vault_keeps_the_values_used_most_recently():
    v = Vault(b"k" * 32, max_values=100)
    rng = random.Random(2)
    first = _token(rng)
    ph = v.placeholder("SECRET", first)
    others = [_token(rng) for _ in range(150)]
    for i, value in enumerate(others):
        v.placeholder("SECRET", value)
        if i % 10 == 0:
            assert v.placeholder("SECRET", first) == ph  # masked again: still in use
    assert 90 <= len(v._kinds) <= 100  # over 100, the oldest tenth goes at once
    assert v.value(ph) == first and _found(v, first) == [first]
    gone = others[0]
    assert gone not in v._kinds
    assert v.value(back := v.placeholder("SECRET", gone)) == gone  # back when it is seen again
    old = others[1]
    assert old not in v._kinds and _found(v, f"see {old}") == [] and v.kind_of(old) == "SECRET"
    import hmac, hashlib
    assert back == "KEEPMUM_SECRET_" + hmac.new(b"k" * 32, f"SECRET\0{gone}".encode(), hashlib.sha256).hexdigest()[:8]


def test_a_value_let_go_takes_its_placeholders_and_escaped_forms():
    v = Vault(b"k" * 32, max_values=1)
    ph = v.placeholder("SECRET", 'pa"ss/word')
    assert v._forms and v.value(ph) == 'pa"ss/word'
    v.placeholder("SECRET", "another-secret")
    assert v.value(ph) is None and not any(p == ph for p in v._forms.values()) and 'pa"ss/word' not in v._made


def test_a_value_let_go_leaves_no_copy_in_the_vault():
    """Not in a table, a pattern, or the log of what was learned: the vault holds the values it
    keeps, and nothing of those it let go."""
    v = Vault(b"k" * 32, max_values=50)
    rng = random.Random(3)
    values = [_token(rng) for _ in range(2000)]
    for value in values:
        v.placeholder("SECRET", value, ["rules:github-token"], "mcp__github")
        v.known_values()
    kept = set(v._kinds)
    gone = [x for x in values if x not in kept]
    assert len(gone) >= 1950
    held = [x for level in v._levels for x in level.values]
    assert set(held) <= kept | set(v._forms) and not set(v._log) - kept
    patterns = " ".join(level.pattern.pattern for level in v._levels if level.pattern is not None)
    assert not any(x in patterns or x[4:12] in patterns for x in gone[-200:])
    assert not set(gone) & (set(v._values.values()) | set(v._sources) | set(v._origins) | set(v._made))


def test_a_new_value_rebuilds_only_the_smallest_pattern():
    v = Vault(b"k" * 32)
    rng = random.Random(4)
    for _ in range(1000):
        v.placeholder("SECRET", _token(rng))
    v.known_values()
    big = [level.pattern for level in v._levels[1:]]
    v.placeholder("SECRET", _token(rng))
    v.known_values()
    assert [level.pattern for level in v._levels[1:]] == big  # the same objects: not built again


def test_a_text_seen_before_finds_what_a_full_scan_finds():
    """The redactor keeps each text's known values with the vault's mark, and later looks only for
    the values learned since: what it masks must be what a full scan masks, as values come and go."""
    from keepmum.detect.rules import RulesDetector
    from keepmum.redact import Redactor

    rng = random.Random(5)
    names = ["Anna", "Anna Smith", "Bob Jones", "carol@example.com", "x-team-17", "EMP-004211"]
    tokens = [_token(rng) for _ in range(40)]
    history = [f"{rng.choice(names)} sent {rng.choice(tokens)} to {rng.choice(names)}; Annapolis, {rng.choice(tokens)}."
               for _ in range(30)]
    r = Redactor(RulesDetector(), Vault(b"k" * 32, max_values=25))

    def covered(text, matches):
        return {i for a, b, _ in matches for i in range(a, b)}

    for step in range(120):
        value = rng.choice(names + tokens)
        r.vault.placeholder("PERSON" if "@" not in value and not value.startswith("ghp_") else "SECRET", value)
        for text in rng.sample(history, 5):
            r._spans(text)  # the cache, kept up to date as requests come
            derived = r._derived[r._key(text)]
            cached = r._known_in(text, derived, r.vault.known_values())
            full = [(m.start(), m.end(), m.group()) for m in r.vault.known_values().finditer(text)]
            assert covered(text, cached) == covered(text, full), (step, text)


def test_a_vault_cleared_scans_every_text_again_in_full():
    v = Vault(b"k" * 32)
    v.placeholder("SECRET", "ghp_aaaaaaaaaaaaaaaaaaaa")
    mark = v.mark()
    v.clear()
    assert v.learned_since(mark) is None


def test_a_value_let_go_never_hides_a_shorter_one_still_kept():
    """"Anna Smith" gone, "Anna" kept: "Anna" is still found where "Anna Smith" was, and a kept
    value that starts inside it."""
    v = Vault(b"k" * 32, max_values=10)
    fillers = [f"filler-{i}" for i in range(7)]
    for value in ("Anna Smith", fillers[0], "Anna", "Smith Co", "Bobby", *fillers[1:]):
        v.placeholder("PERSON", value)
        v.known_values()
    assert set(v._kinds) == {"Anna", "Smith Co", "Bobby", *fillers[1:]}  # the oldest two went
    assert _found(v, "Anna Smith Co. and Bobby") == ["Anna", "Bobby", "Smith Co"]


def test_the_redactors_cache_holds_positions_never_text():
    """A value the vault lets go leaves no copy in the cache, nor does a decoded block's secret."""
    import base64

    from keepmum.detect.rules import RulesDetector
    from keepmum.redact import Redactor

    from keepmum.detect.pipeline import Pipeline

    rng = random.Random(6)
    r = Redactor(Pipeline(RulesDetector(), None), Vault(b"k" * 32, max_values=20))
    secret = _token(rng)
    texts = [f"token {_token(rng)} and Anna Smith" for _ in range(60)]
    texts.append("blob " + base64.b64encode(f"GITHUB_TOKEN={secret}".encode()).decode() + " end")
    for value in ("Anna Smith",):
        r.vault.placeholder("PERSON", value)
    for _ in range(2):
        for text in texts:
            r.redact_text(text)

    def strings(x):
        if isinstance(x, str):
            yield x
        elif isinstance(x, (list, tuple)):
            for y in x:
                yield from strings(y)

    assert not [s for entry in r._derived.values() for s in strings(entry)]
    assert any(entry[0] for entry in r._derived.values())  # the base64 block: flagged, not kept
    assert "KEEPMUM_SECRET_" in r.redact_text(texts[-1])  # and still masked when the text comes back


def test_a_match_on_a_value_let_go_never_hides_one_kept_inside_it():
    """The safeguard for a pattern that still holds a value the vault let go: "Anna Smith" gone
    from the vault but still in a built level must not swallow "Anna", nor "Smith Co" starting in it.
    _evict builds the levels again, so this is a value forgotten without that."""
    v = Vault(b"k" * 32)
    for value in ["Anna Smith", "Anna", "Smith Co", *[f"filler-{i:03d}" for i in range(40)]]:
        v.placeholder("PERSON", value)
    v.known_values()
    assert any("Anna Smith" in level.values for level in v._levels[1:])  # in a compiled level
    v._forget("Anna Smith")
    v._known = None
    assert _found(v, "Anna Smith Co. and filler-007") == ["Anna", "Smith Co", "filler-007"]
    assert _found(v, "Annapolis") == []  # inside a longer word


def test_a_match_let_go_steps_down_to_the_longest_value_kept():
    """Two values let go, one inside the other: the safeguard steps down past both to "Anna";
    and a value let go with nothing kept inside it is passed over."""
    v = Vault(b"k" * 32)
    for value in ["Anna Smith Jones", "Anna Smith", "Anna", "Zed Zebra", *[f"filler-{i:03d}" for i in range(40)]]:
        v.placeholder("PERSON", value)
    v.known_values()
    for gone in ("Anna Smith Jones", "Anna Smith", "Zed Zebra"):
        v._forget(gone)
    v._known = None
    assert _found(v, "Anna Smith Jones met Zed Zebra and filler-001") == ["Anna", "filler-001"]


def test_eviction_puts_what_is_left_in_the_level_that_holds_it():
    """Over 256 values left, they go a level up, under empty smaller ones, and are all found."""
    rng = random.Random(7)
    v = Vault(b"k" * 32, max_values=400)
    values = [_token(rng) for _ in range(450)]
    for value in values:
        v.placeholder("SECRET", value)
    kept = [x for x in values if x in v._kinds]
    assert 360 <= len(kept) <= 400  # the oldest tenth went each time it passed 400
    assert len(v._levels) >= 3 and len(v._levels[-1].values) >= 300  # past 256: a level up
    assert all(len(level.values) <= 256 for level in v._levels[:-1])
    sample = rng.sample(kept, 20)
    assert _found(v, " ".join(sample)) == sorted(sample)


def test_the_shared_sets_start_over_past_their_bound():
    v = Vault(b"k" * 32)
    for i in range(4100):
        v._shared(frozenset({f"user:rule-{i}"}))
    assert len(v._sets) <= 4097
    same = v._shared(frozenset({"rules:github-token"}))
    assert v._shared(frozenset({"rules:github-token"})) is same


def test_touching_a_value_the_vault_does_not_hold_changes_nothing():
    v = Vault(b"k" * 32)
    v.placeholder("SECRET", "first-secret-value")
    v.placeholder("SECRET", "second-secret-value")
    order = list(v._kinds)
    v._touch(None)
    v._touch("never-seen-value")
    assert list(v._kinds) == order


def test_forgetting_a_value_leaves_a_form_another_value_holds():
    v = Vault(b"k" * 32)
    v.placeholder("SECRET", 'pa"ss/word')
    other = v.placeholder("SECRET", "another-secret")
    form = next(f for f in v._made['pa"ss/word'] if not f.startswith("KEEPMUM_"))
    v._forms[form] = other  # the form is the other value's now
    v._forget('pa"ss/word')
    assert v._forms.get(form) == other


def test_the_redactors_cache_keeps_its_size():
    from keepmum.detect.pipeline import Pipeline
    from keepmum.detect.rules import RulesDetector
    from keepmum.redact import Redactor

    r = Redactor(Pipeline(RulesDetector(), None), Vault(b"k" * 32), cache_size=3)
    for i in range(6):
        r.redact_text(f"note {i}: nothing to mask here")
    assert len(r._derived) == 3 and len(r._cache) == 3


def test_sub_replaces_as_one_pattern_over_every_value_would():
    """The matcher stands in for a regex: sub() takes the leftmost, longest value, none overlapping,
    across its levels (the e2e harness writes back what the model saw with it)."""
    import re

    rng = random.Random(8)
    values = ["Anna", "Anna Smith", "Smith Co", "Margaret Thornbury", *[_token(rng) for _ in range(60)]]
    v = Vault(b"k" * 32)
    for value in values:
        v.placeholder("PERSON", value)
    text = f"Anna Smith Co. wrote Margaret Thornbury_resume.md, {values[20]} and {values[50]}; Annapolis."
    one = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(map(re.escape, sorted(values, key=len, reverse=True))) + r")(?![A-Za-z0-9])")
    assert v.known_values().sub(lambda m: f"<{m.group()}>", text) == one.sub(lambda m: f"<{m.group()}>", text)
