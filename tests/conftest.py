"""Tests run in a HOME of their own, so they never write the developer's salt, history or
settings. pf-serve and the model are still found where this Mac keeps them, read only."""

import os
import tempfile
from pathlib import Path

import pytest

_real = Path.home()
_pf_serve = _real / ".local" / "share" / "keepmum" / "bin" / "pf-serve"
if _pf_serve.exists():
    os.environ.setdefault("KEEPMUM_PF_SERVE", str(_pf_serve))
os.environ.setdefault("HF_HOME", str(_real / ".cache" / "huggingface"))
# The checks' models, for the end-to-end tests that run them (read only, linked into their HOME).
os.environ.setdefault("KEEPMUM_TEST_GUARD_DIR", str(_real / ".local" / "share" / "keepmum" / "guard"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="keepmum-tests-")


@pytest.fixture(autouse=True)
def _no_codex_processes(monkeypatch):
    """Codex already running on this Mac must not turn up in a test's Codex status as a warning."""
    from keepmum import clients

    monkeypatch.setattr(clients, "_codex_processes", lambda: [])
