"""The Logs page's feed: what new text held, with the first and last character only, in memory only."""

import secrets

import anyio
import httpx

from keepmum import selftest
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.proxy import Proxy
from keepmum.redact import Redactor, preview
from keepmum.stats import History, Stats
from keepmum.vault import Vault


def make(tmp_path, previews=True):
    mock = selftest.MockUpstream()
    up = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    stats = Stats(history=History(tmp_path / "history.jsonl"))
    proxy = Proxy(Redactor(Pipeline(RulesDetector(), None, pii=True), Vault(secrets.token_bytes(32))), upstream="http://up",
                  client=up, stats=stats, stats_token="t", previews=previews)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
    return proxy, client


def body(*turns):
    return {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": t} for t in turns]}


async def findings(client, token="t"):
    r = await client.get("/_keepmum/findings", headers={"authorization": f"Bearer {token}"})
    return r.status_code, r.json()


def test_preview_shows_only_the_ends_of_long_values():
    assert preview("ghp_abcdefgh") == "g…h"
    assert preview("hunter2") == "•••"  # too short to show any of it


def test_findings_show_new_text_only_and_never_reach_disk(tmp_path):
    canary = selftest.make_canary()
    first = f"export GITHUB_TOKEN={canary}"

    async def go():
        _, client = make(tmp_path)
        await client.post("/v1/messages", json=body(first), headers={"x-keepmum-client": "claude-code"})
        # The next turn resends the first: only the new text's values are new findings.
        await client.post("/v1/messages", json=body(first, "and mail jane.roe@fastmail.com"))
        return await findings(client), await findings(client, "wrong")

    (code, data), (denied, _) = anyio.run(go)
    assert code == 200 and denied == 401 and data["previews"] is True
    got = [(f["type"], f["detector"], f["rule"], f["preview"], f["chars"]) for f in data["findings"]]
    assert got == [("EMAIL", "rules", "email", "j…m", 21),
                   ("SECRET", "rules", "github-token", f"{canary[0]}…{canary[-1]}", len(canary))]
    assert canary not in str(data) and "jane.roe" not in str(data)
    history = (tmp_path / "history.jsonl").read_text()
    assert canary[0] + "…" not in history and "j…m" not in history and "preview" not in history


def test_previews_off_keeps_types_and_drops_the_characters(tmp_path):
    canary = selftest.make_canary()
    state = {"on": True}

    async def go():
        proxy, client = make(tmp_path, previews=lambda: state["on"])
        await client.post("/v1/messages", json=body(f"export GITHUB_TOKEN={canary}"))
        before = await findings(client)
        state["on"] = False
        await client.post("/v1/messages", json=body("export STRIPE_KEY=" + "sk_live_" + "Zq8vX2mN4pL7kR9tY3wB6cF1"))
        return before, await findings(client), proxy

    (_, before), (_, after), proxy = anyio.run(go)
    assert before["findings"][0]["preview"] is not None
    assert after["previews"] is False and [f["preview"] for f in after["findings"]] == [None, None]
    assert all(f["preview"] is None for f in proxy.stats.findings)  # forgotten, not just hidden


def test_context_shows_the_text_around_a_value_with_every_value_as_its_type(tmp_path):
    canary = selftest.make_canary()
    text = (f"deploy notes: rotate GITHUB_TOKEN={canary} tonight, then mail jane.roe@fastmail.com; "
            "the old key was KEEPMUM_SECRET_9a6c1c4b in yesterday's run")

    async def go():
        _, client = make(tmp_path)
        await client.post("/v1/messages", json=body(text))
        return await findings(client)

    _, data = anyio.run(go)
    assert data["context"] is True
    token = next(f for f in data["findings"] if f["rule"] == "github-token")
    assert token["before"] == "deploy notes: rotate GITHUB_TOKEN="
    assert token["after"].startswith(" tonight, then mail ‹EMAIL›;")
    assert canary not in str(data) and "jane.roe" not in str(data) and "KEEPMUM_" not in str(data)
    assert "context" not in (tmp_path / "history.jsonl").read_text() and "tonight" not in (tmp_path / "history.jsonl").read_text()


def test_context_never_shows_part_of_a_value_or_placeholder_cut_by_the_window():
    from keepmum.detect import Span
    from keepmum.redact import CONTEXT, context

    secret = "s3cr3t-" * 20  # longer than the window
    text = secret + " sits right before " + "x" * 10 + " KEEPMUM_SECRET_0123abcd and more"
    start = text.index("x")
    before, after = context(text, [Span(0, len(secret), "SECRET", 1.0, "rules:x")], start, start + 10)
    # The window starts inside the value: "…" for what is left out, the value whole as its type.
    assert len(secret) > CONTEXT and before == "…‹SECRET› sits right before "
    assert after == " ‹SECRET› and more"  # the placeholder, too, shows only as its type


def test_context_off_drops_the_text_already_kept(tmp_path):
    state = {"on": True}

    async def go():
        mock = selftest.MockUpstream()
        up = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
        proxy = Proxy(Redactor(Pipeline(RulesDetector(), None, pii=True), Vault(secrets.token_bytes(32))),
                      upstream="http://up", client=up, stats=Stats(), stats_token="t", context=lambda: state["on"])
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
        await client.post("/v1/messages", json=body("mail jane.roe@fastmail.com today"))
        state["on"] = False
        return await findings(client), proxy

    (_, data), proxy = anyio.run(go)
    assert data["context"] is False and data["findings"][0]["before"] is None
    assert all(f["before"] is None and f["after"] is None for f in proxy.stats.findings)


def test_findings_name_every_detector_and_who_found_a_value_first():
    from keepmum.detect import Span

    class Model:
        name = "pf-serve"

        def detect(self, text):
            i = text.find("hunter2-correct-horse")
            return [Span(i, i + 21, "SECRET", 0.9, "pf-serve")] if i >= 0 else []

    r = Redactor(Pipeline(RulesDetector(), Model(), pii=True), Vault(secrets.token_bytes(32)))
    r.redact_text("db password=hunter2-correct-horse")  # the rule and the model both find it
    first = r.findings[-1]
    assert first["detector"] == "rules" and first["by"] == ["rule:secret-assignment", "model"] and not first["earlier"]
    r.findings = []
    r.redact_text("try hunter2-correct-horse again")  # matched as a value masked before
    again = r.findings[-1]
    assert again["detector"] in ("known", "model") and again["earlier"] is True
    assert "rule:secret-assignment" in again["by"] and "model" in again["by"]
