"""The e2e oracle (e2e/oracle.py) catches what it is there to catch. No agent runs here."""

import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from e2e.agents import Transcript  # noqa: E402
from e2e.case import Case, Value  # noqa: E402
from e2e.oracle import judge, snapshot  # noqa: E402
from e2e.servers import Exchange  # noqa: E402
from keepmum.vault import Vault  # noqa: E402

TOK = "ghp_" + "A1b2C3d4" * 4 + "Zz9Q"


def case(**kw) -> Case:
    base = dict(id="t", title="t", why="", path=Path("t.toml"), agents=["codex"], modes=["scripted"], config="",
                model=False, prompt="p", values={"tok": Value("tok", TOK, "SECRET")}, plain=[], files={},
                workspace_dir="ws", script=[], expect={})
    return Case(**{**base, **kw})


def run(c: Case, sent: list[str] = (), before=None, after=None, final="", commands=(), masked=None, salt=None):
    salt = salt or secrets.token_bytes(32)
    ex = [Exchange("t", "POST", "/v1/messages", "", {}, s.encode()) for s in sent]
    found = judge(c, salt, ex, before or {}, after or {}, Transcript(final=final, commands=list(commands)),
                  masked or {}, {"unresolved": 0, "errors": []}, 0, None)
    return {(f.cls, f.what) for f in found}, salt


def classes(found) -> set[str]:
    return {c for c, _ in found}


def test_a_value_in_any_form_is_a_leak():
    escaped_twice = json.dumps(json.dumps({"output": TOK + '"'}))  # Codex: JSON text inside JSON
    for body in (TOK, json.dumps({"t": TOK}), escaped_twice):
        assert "leak" in classes(run(case(), [body])[0])
    assert "leak" not in classes(run(case(), ["KEEPMUM_SECRET_0011aabb"])[0])


def test_a_value_masked_only_in_part_is_a_leak():
    c = case(values={"pw": Value("pw", 'Tr0ub4dor"&\\3x', "SECRET")})
    sent = json.dumps({"text": 'ADMIN_PASSWORD=KEEPMUM_SECRET_0011aabb"&\\3x'})
    found, _ = run(c, [sent], masked={"KEEPMUM_SECRET_0011aabb": ("SECRET", "Tr0ub4dor")})
    assert any(cls == "leak" and "only in part" in what for cls, what in found)


def test_a_placeholder_on_disk_in_any_spelling_is_residue():
    for name in ("notes-KEEPMUM_SECRET_0011aabb.md", "notes-keepmum_secret_0011aabb.md", "keepmum-secret-0011aabb.md"):
        assert "residue" in classes(run(case(), after={name: b"x"})[0])
    assert "residue" in classes(run(case(), after={"a.env": b"T=KEEPMUM_SECRET_0011aabb\n"})[0])
    assert "residue" in classes(run(case(), commands=["mv a KEEPMUM_SECRET_0011aabb.txt"])[0])
    assert "residue" not in classes(run(case(), after={"keepmum.md": b"keepmum masks secrets"})[0])


def test_a_workspace_name_sent_as_a_placeholder_is_a_false_mask():
    names = {"bench/results/PHASE0.md": b"x"}
    found, _ = run(case(), before=names, after=names, masked={"KEEPMUM_SECRET_0011aabb": ("SECRET", "bench/results/PHASE0")})
    assert "false-mask" in classes(found)
    salt = secrets.token_bytes(32)
    ph = Vault(salt).placeholder("SECRET", "guard-20261104-231405.md")
    found, _ = run(case(plain=["guard-20261104-231405.md"]), [f"see {ph}"], salt=salt)
    assert "false-mask" in classes(found)


def test_a_value_masked_with_its_neighbours_is_a_false_mask():
    found, _ = run(case(), masked={"KEEPMUM_SECRET_0011aabb": ("SECRET", TOK + "\\nLOG_LEVEL=info\\n")})
    assert "false-mask" in classes(found)


def test_the_workspace_the_case_expects(tmp_path):
    c = case(expect={"files": {"backup.txt": "nightly\n", "MOVED.md": {"contains": "backup-{{v:tok}}.txt"}},
                     "absent": ["backup-{{v:tok}}.txt"], "only_new": ["backup.txt", "MOVED.md"]})
    before = {f"backup-{TOK}.txt": b"nightly\n"}
    good = {"backup.txt": b"nightly\n", "MOVED.md": f"backup-{TOK}.txt\n".encode()}
    assert not classes(run(c, before=before, after=good)[0])
    wrong = {"backup.txt": b"nightly\n", "MOVED.md": b"backup-KEEPMUM_SECRET_0011aabb.txt\n", "x.tmp": b""}
    found, _ = run(c, before=before, after=wrong)
    assert {"task", "residue"} <= classes(found)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("x")
    (tmp_path / "a.txt").write_text("a")
    assert set(snapshot(tmp_path)) == {"a.txt"}
