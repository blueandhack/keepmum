"""localmodels.py against a fake Ollama (httpx mock transports): odd pull streams, failures of
show, create and delete, and Ollama's error texts."""

import json

import httpx
import pytest

from keepmum import localmodels
from keepmum.localmodels import PullError

URL = "http://ollama.test"


def stream_from(monkeypatch, handler):
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: httpx.Client(
        transport=httpx.MockTransport(handler)).stream(method, url, json=kw.get("json")))


def test_pull_skips_blank_and_unreadable_lines_and_works_without_progress(monkeypatch):
    body = "\n".join(["", "not json", json.dumps({"digest": "a", "total": 10, "completed": 10}),
                      json.dumps({"status": "success"})]) + "\n"
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=body.encode())

    stream_from(monkeypatch, handler)
    assert localmodels.pull(URL, "m:q") is None
    assert seen == [{"model": "m:q", "stream": True}]


def test_pull_that_breaks_off_says_where(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    stream_from(monkeypatch, handler)
    with pytest.raises(PullError, match=f"Ollama at {URL}: timed out"):
        localmodels.pull(URL, "m")


def test_pull_error_status_with_plain_text(monkeypatch):
    stream_from(monkeypatch, lambda r: httpx.Response(500, content=b"  internal trouble  "))
    with pytest.raises(PullError, match="^internal trouble$"):
        localmodels.pull(URL, "m")
    stream_from(monkeypatch, lambda r: httpx.Response(502, content=b""))
    with pytest.raises(PullError, match="Ollama answered 502"):
        localmodels.pull(URL, "m")


def test_ollama_error_texts():
    assert localmodels._ollama_error('{"error": "no such model"}') == "no such model"
    assert localmodels._ollama_error('{"error": ""}') is None
    assert localmodels._ollama_error("[1]") == "[1]"  # JSON, but not an object
    assert localmodels._ollama_error("x" * 300) == "x" * 200
    assert localmodels._ollama_error("   ") is None


def test_show_when_ollama_lacks_the_model_or_does_not_answer(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: httpx.Response(404, json={"error": "not found"}))
    assert localmodels.show(URL, "m") is None
    assert localmodels.base_of(URL, "keepmum/m:ctx64k") == "keepmum/m:ctx64k"  # no parent known: its own base

    def down(url, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    assert localmodels.show(URL, "m") is None
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: httpx.Response(200, content=b"not json"))
    assert localmodels.show(URL, "m") is None


def test_base_of_a_model_that_is_not_a_copy_asks_nothing(monkeypatch):
    monkeypatch.setattr(localmodels, "show", lambda *a, **k: pytest.fail("asked Ollama"))
    assert localmodels.base_of(URL, "qwen3:8b") == "qwen3:8b"


def test_with_context_reports_create_failures(monkeypatch):
    monkeypatch.setattr(localmodels, "base_of", lambda url, model: model)

    def down(url, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(PullError, match=f"Ollama at {URL}: refused"):
        localmodels.with_context(URL, "base:q", 32768)
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: httpx.Response(400, json={"error": "no base"}))
    with pytest.raises(PullError, match="^no base$"):
        localmodels.with_context(URL, "base:q", 32768)


def test_delete_copy_only_deletes_keepmums_copies(monkeypatch):
    asked = []

    def request(method, url, json=None, timeout=None):
        asked.append((method, url, json))
        return httpx.Response(200 if json["model"].endswith("ctx64k") else 404)

    monkeypatch.setattr(httpx, "request", request)
    assert localmodels.delete_copy(URL, "keepmum/m:ctx64k") is True
    assert localmodels.delete_copy(URL, "keepmum/m:ctx32k") is False
    assert asked[0] == ("DELETE", f"{URL}/api/delete", {"model": "keepmum/m:ctx64k"})

    def down(method, url, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "request", down)
    assert localmodels.delete_copy(URL, "keepmum/m:ctx64k") is False


def test_context_labels_and_limits():
    assert localmodels.context_label(None) == "default" and localmodels.context_label(65536) == "64k"
    assert localmodels.context_label(5000) == "5000"
    with pytest.raises(PullError, match="2k to 1024k"):
        localmodels.parse_context("1k")
    with pytest.raises(PullError, match="give a size"):
        localmodels.parse_context("lots")
