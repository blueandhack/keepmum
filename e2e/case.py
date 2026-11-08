"""A case: a workspace, values to protect, what the agent is asked, and what must be true after.

Cases are TOML files in e2e/cases/. Templates in any string:

  {{v:name}}         the real value (files on disk, expectations; never in a script)
  {{ph:name}}        the placeholder keepmum gives that value (scripts: what a model would write)
  {{ph:name|lower}}  the placeholder as a careless model rewrites it: lower, slug, typo, upper
  {{cwd}}            the workspace path as the model sees it in the request (scripts only)
  {{seen:text}}      text (templates allowed) as the model saw it: every value keepmum masked in
                     the run replaced by its placeholder (scripts only: a file name from a listing)
"""

from __future__ import annotations

import re
import secrets
import string
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from keepmum.vault import Vault

CASES = Path(__file__).parent / "cases"
_TEMPLATE = re.compile(r"\{\{(v|ph):([A-Za-z0-9_]+)(?:\|([a-z]+))?\}\}")
_SEEN = re.compile(r"\{\{seen:(.+?)\}\}", re.S)
_ALNUM = string.ascii_letters + string.digits


def _gen(kind: str) -> str:
    """A fresh synthetic value in a provider's format, new for every run: it opens nothing."""
    if kind == "github-token":
        return "ghp_" + "".join(secrets.choice(_ALNUM) for _ in range(36))
    if kind == "anthropic-key":
        return "sk-ant-api03-" + "".join(secrets.choice(_ALNUM + "-_") for _ in range(93)) + "AA"
    if kind == "stripe-key":
        return "sk_live_" + "".join(secrets.choice(_ALNUM) for _ in range(24))
    raise ValueError(f"unknown generator {kind!r}")


def mangle(ph: str, how: str | None) -> str:
    """What a model might do to a placeholder it should have copied exactly."""
    if not how:
        return ph
    if how == "lower":
        return ph.lower()
    if how == "upper":
        return ph.upper()
    if how == "slug":
        return ph.lower().replace("_", "-")
    if how == "typo":  # one hex digit off
        last = ph[-1]
        return ph[:-1] + ("0" if last != "0" else "1")
    raise ValueError(f"unknown filter {how!r}")


@dataclass
class Value:
    name: str
    text: str
    kind: str  # the placeholder type keepmum is expected to give it: SECRET, PERSON, ...
    must_mask: bool = True  # it must reach the provider only as its placeholder


@dataclass
class Case:
    id: str
    title: str
    why: str
    path: Path
    agents: list[str]
    modes: list[str]
    config: str  # keepmum's config.toml for the run
    model: bool  # needs pf-serve and the Privacy Filter model
    prompt: str
    values: dict[str, Value]
    plain: list[str]  # strings that must reach the provider as they are: never masked
    files: dict[str, str]  # path -> content, both templates
    workspace_dir: str  # the workspace folder's own name (a template)
    script: list[dict]
    expect: dict
    # The case shows a gap not fixed yet: reported, not counted as a regression. A string, or one
    # per agent ({codex = "…"}) when only some agents have it.
    known_gap: str | dict | None = None
    mcp: bool = False  # the agent gets the stub MCP server `notes` (mcp_notes.py)
    timeout: int = 300
    raw: dict = field(default_factory=dict)

    def resolve(self, text: str, salt: bytes | None = None, real_only: bool = False, seen=None) -> str:
        """Fill {{v:…}} with real values and {{ph:…}} with placeholders under `salt`. With `seen`
        (value -> the placeholder the model was shown for it, or None), a script copies what the
        model saw, which is not always the value's own placeholder: keepmum may have masked it
        together with the text around it."""
        vault = Vault(salt) if salt is not None else None

        def sub(m):
            kind, name, how = m.groups()
            if name not in self.values:
                raise KeyError(f"{self.id}: no value {name!r}")
            v = self.values[name]
            if kind == "v":
                return v.text
            if real_only or vault is None:
                raise ValueError(f"{self.id}: {{{{ph:{name}}}}} needs the salt")
            return mangle((seen and seen(v.text)) or vault.placeholder(v.kind, v.text), how)

        text = _TEMPLATE.sub(sub, text)
        if seen is not None and hasattr(seen, "mask"):
            text = _SEEN.sub(lambda m: seen.mask(m.group(1)), text)
        return text

    def gap(self, agent: str) -> str | None:
        if isinstance(self.known_gap, dict):
            return self.known_gap.get(agent)
        return self.known_gap

    def placeholder(self, name: str, salt: bytes) -> str:
        v = self.values[name]
        return Vault(salt).placeholder(v.kind, v.text)

    def shown(self, text: str) -> str:
        """text with every real value replaced by its template name: safe to write to a result."""
        for v in sorted(self.values.values(), key=lambda v: len(v.text), reverse=True):
            text = text.replace(v.text, f"{{{{v:{v.name}}}}}")
        return text


def load_case(path: Path) -> Case:
    raw = tomllib.loads(path.read_text())
    values = {}
    for name, spec in (raw.get("values") or {}).items():
        if isinstance(spec, str):
            spec = {"text": spec}
        text = _gen(spec["gen"]) if "gen" in spec else spec["text"]
        values[name] = Value(name, text, spec.get("kind", "SECRET"), spec.get("must_mask", True))
    config = raw.get("config") or "[detect]\nmodel = false\n"
    # TOML puts a key written after a [table] into that table: a prompt below [files] is a file.
    if not str(raw.get("prompt", "")).strip():
        raise ValueError(f"{path.name}: no top-level prompt (is it below a [table]?)")
    return Case(
        id=raw["id"], title=raw["title"], why=raw.get("why", "").strip(), path=path,
        agents=raw.get("agents", ["codex", "claude", "agy"]), modes=raw.get("modes", ["scripted", "live"]),
        config=config, model=bool(tomllib.loads(config).get("detect", {}).get("model", False)),
        prompt=raw.get("prompt", "").strip(), values=values, plain=raw.get("plain", []),
        files=raw.get("files", {}), workspace_dir=raw.get("workspace_dir", "ws"), script=raw.get("script", []),
        expect=raw.get("expect", {}), known_gap=raw.get("known_gap"),
        mcp=bool(raw.get("mcp", False)), timeout=raw.get("timeout", 300), raw=raw,
    )


def all_cases(selected: list[str] | None = None) -> list[Case]:
    cases = [load_case(p) for p in sorted(CASES.glob("*.toml"))]
    if selected:
        cases = [c for c in cases if any(c.id == s or c.id.startswith(s) for s in selected)]
    return cases
