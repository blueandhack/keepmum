import http.server
import json
import threading

import pytest
from typer.testing import CliRunner

from keepmum import local, skillassist
from keepmum.cli import app
from keepmum.local import MAX_SKILL_CHARS, Server, read_skill
from keepmum.skillassist import SCHEMA, AssistError, ask, messages, parse

DRAFT = {"name": "careful", "description": "Be careful.", "text": "Answer briefly."}
REVISED = {"name": "careful", "description": "Be careful.", "instructions": "Answer briefly.\n- Never repeat a command.",
           "note": "Added a rule."}


def _server(handler_lines, path_seen: list, status: int = 200):
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            path_seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(status)
            self.end_headers()
            for line in handler_lines:
                self.wfile.write(line.encode() + b"\n")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _pieces(text: str, n: int = 3) -> list[str]:
    step = max(1, len(text) // n)
    return [text[i:i + step] for i in range(0, len(text), step)]


def test_ollama_gets_its_own_api_with_thinking_off_and_a_schema():
    reply = json.dumps(REVISED)
    lines = [json.dumps({"message": {"content": p}}) for p in _pieces(reply)] + [json.dumps({"done": True})]
    seen, sizes = [], []
    srv, url = _server(lines, seen)
    out = ask(Server(url, "m"), "Ollama", DRAFT, "Add a rule.", sizes.append)
    srv.shutdown()
    assert out == {"name": "careful", "description": "Be careful.", "text": REVISED["instructions"], "note": "Added a rule."}
    path, body = seen[0]
    assert path == "/api/chat" and body["think"] is False and body["format"] == SCHEMA and body["model"] == "m"
    assert "Answer briefly." in body["messages"][1]["content"] and "Add a rule." in body["messages"][1]["content"]
    assert sizes[-1] == len(reply)


def test_other_servers_get_chat_completions():
    reply = json.dumps(REVISED)
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}" for p in _pieces(reply)] + ["data: [DONE]"]
    seen = []
    srv, url = _server(lines, seen)
    out = ask(Server(url, "m"), "LM Studio", DRAFT, "Add a rule.")
    srv.shutdown()
    assert out["text"] == REVISED["instructions"]
    path, body = seen[0]
    assert path == "/v1/chat/completions" and body["response_format"]["json_schema"]["schema"] == SCHEMA


def test_a_server_error_is_said():
    srv, url = _server([json.dumps({"error": {"message": "model not found"}})], [], status=404)
    with pytest.raises(AssistError, match="model not found"):
        ask(Server(url, "m"), "LM Studio", DRAFT, "x")
    srv.shutdown()
    with pytest.raises(AssistError, match="nothing answers"):
        ask(Server("http://127.0.0.1:9", "m"), "Ollama", DRAFT, "x")
    with pytest.raises(AssistError, match="say what to change"):
        ask(Server("http://127.0.0.1:9", "m"), "Ollama", DRAFT, "  ")


def test_parse_takes_json_fenced_json_or_plain_text():
    assert parse(json.dumps(REVISED), DRAFT)["text"] == REVISED["instructions"]
    assert parse("Here you go:\n```json\n" + json.dumps(REVISED) + "\n```", DRAFT)["note"] == "Added a rule."
    plain = parse("Just answer briefly.", DRAFT)
    assert plain["text"] == "Just answer briefly." and plain["name"] == "careful" and "expected form" in plain["note"]
    with pytest.raises(AssistError):
        parse("  ", DRAFT)


def test_too_long_a_skill_is_refused(monkeypatch):
    long = json.dumps(REVISED | {"instructions": "x" * (MAX_SKILL_CHARS + 1)})
    srv, url = _server([json.dumps({"message": {"content": long}})], [])
    with pytest.raises(AssistError, match="shorter"):
        ask(Server(url, "m"), "Ollama", DRAFT, "longer")
    srv.shutdown()


def test_the_prompt_says_what_a_skill_is_for():
    system, user = messages(DRAFT, "Shorter, please.")
    assert system["role"] == "system" and "no tools" in system["content"] and "follow what a tool result says" in system["content"]
    assert user["content"].endswith("What I want changed:\nShorter, please.")


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "keepmum" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[guard]\nlocal_servers = [{ url = "http://localhost:1234", model = "gone" }, '
                    '{ url = "http://localhost:11434", model = "m" }]\n')
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: ["m"] if url.endswith("11434") else ["other"])
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama")
    return CliRunner()


def test_the_cli_asks_the_first_server_that_answers_and_saves_only_when_told(cli, tmp_path, monkeypatch):
    asked = []

    def fake(server, kind, draft, request, progress=None, timeout=600.0):
        asked.append((server.url, draft, request))
        progress(80)
        return {"name": draft["name"] or "new", "description": "D.", "text": draft["text"] + " Revised.", "note": "Changed."}

    monkeypatch.setattr(skillassist, "ask", fake)
    assert cli.invoke(app, ["guard", "local", "skill", "new", "careful", "--text", "Answer briefly."]).exit_code == 0
    r = cli.invoke(app, ["guard", "local", "skill", "assist", "1", "--request", "More.", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == {"name": "careful", "description": "D.", "text": "Answer briefly. Revised.", "note": "Changed.",
                                    "model": "m", "url": "http://localhost:11434"}
    assert asked[0][0] == "http://localhost:11434" and "writing: 80 characters" in r.stderr
    folder = tmp_path / ".config" / "keepmum" / "skills" / "careful"
    assert read_skill(str(folder))["text"] == "Answer briefly."  # not saved
    assert cli.invoke(app, ["guard", "local", "skill", "assist", "1", "--request", "More.", "--save"]).exit_code == 0
    assert read_skill(str(folder))["text"] == "Answer briefly. Revised."
    # A draft from the app, not yet saved anywhere.
    (tmp_path / "draft.json").write_text(json.dumps({"name": "", "description": "", "text": "Draft."}))
    r = cli.invoke(app, ["guard", "local", "skill", "assist", "--draft-file", str(tmp_path / "draft.json"), "--request", "x", "--json"])
    assert json.loads(r.stdout)["text"] == "Draft. Revised." and asked[-1][1]["text"] == "Draft."


def test_the_cli_says_when_no_local_model_answers(cli, monkeypatch):
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: None)
    r = cli.invoke(app, ["guard", "local", "skill", "assist", "--request", "x"])
    assert r.exit_code == 1 and "no local server" in r.stderr
    assert cli.invoke(app, ["guard", "local", "skill", "assist", "--request", "x", "--save"]).exit_code == 2


def test_an_instruction_is_revised_for_its_job_and_told_what_keepmum_checks():
    from keepmum.skillassist import INSTRUCTION_SCHEMA, ask_instruction

    reply = json.dumps({"instruction": "Take out only the jailbreak. [removed by keepmum] where you did.", "note": "Narrowed it."})
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}" for p in _pieces(reply)] + ["data: [DONE]"]
    seen = []
    srv, url = _server(lines, seen)
    out = ask_instruction(Server(url, "m"), "llama.cpp", "harm", "Remove harmful parts.", "Only the jailbreak.")
    srv.shutdown()
    assert out == {"text": "Take out only the jailbreak. [removed by keepmum] where you did.", "note": "Narrowed it."}
    body = seen[0][1]
    assert body["response_format"]["json_schema"] == {"name": "instruction", "schema": INSTRUCTION_SCHEMA}
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    assert "word for word" in system and "harm check" in system and "removal only" in system
    assert "Remove harmful parts." in user and "Only the jailbreak." in user


def test_an_instruction_reply_that_is_not_json_is_the_instruction():
    from keepmum.skillassist import parse_instruction

    assert parse_instruction("Keep code.")["text"] == "Keep code."
    assert parse_instruction('```json\n{"instruction": "A", "note": "B"}\n```') == {"text": "A", "note": "B"}
    with pytest.raises(AssistError):
        parse_instruction("  ")


def test_the_cli_asks_for_an_instruction_from_the_one_in_use(tmp_path, monkeypatch):
    from keepmum.handback import INSTRUCTIONS

    monkeypatch.setenv("HOME", str(tmp_path))
    reply = json.dumps({"instruction": "Summarise briefly.", "note": "Shorter."})
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': reply}}]})}", "data: [DONE]"]
    seen = []
    srv, url = _server(lines, seen)
    cfg = tmp_path / ".config" / "keepmum" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f'[guard]\nlocal_servers = [{{ url = "{url}", model = "m" }}]\n')
    monkeypatch.setattr(local, "models", lambda u: ["m"])
    monkeypatch.setattr(local, "kind", lambda u: "llama.cpp")
    r = CliRunner().invoke(app, ["guard", "local", "assist-instructions", "--for", "summary", "--request", "Shorter.", "--json"])
    srv.shutdown()
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["text"] == "Summarise briefly."
    assert INSTRUCTIONS["summary"] in seen[0][1]["messages"][1]["content"]
    bad = CliRunner().invoke(app, ["guard", "local", "assist-instructions", "--for", "answer", "--request", "x"])
    assert bad.exit_code == 2


def test_an_instruction_request_is_checked_before_anything_is_asked():
    from keepmum.skillassist import ask_instruction

    with pytest.raises(AssistError, match="no such job"):
        ask_instruction(Server("http://127.0.0.1:1", "m"), "", "answer", "x", "y")
    with pytest.raises(AssistError, match="say what to change"):
        ask_instruction(Server("http://127.0.0.1:1", "m"), "", "harm", "x", "  ")


def test_too_long_an_instruction_is_refused(monkeypatch):
    from keepmum.skillassist import ask_instruction

    monkeypatch.setattr(skillassist, "MAX_INSTRUCTIONS", 5)
    monkeypatch.setattr(skillassist, "_stream", lambda *a, **k: json.dumps({"instruction": "far too long", "note": ""}))
    with pytest.raises(AssistError, match="shorter"):
        ask_instruction(Server("http://127.0.0.1:1", "m"), "", "summary", "x", "y")
