"""Tests run in a HOME of their own, so they never write the developer's salt, history or
settings. pf-serve and the model are still found where this Mac keeps them, read only."""

import os
import tempfile
from pathlib import Path

_real = Path.home()
_pf_serve = _real / ".local" / "share" / "keepmum" / "bin" / "pf-serve"
if _pf_serve.exists():
    os.environ.setdefault("KEEPMUM_PF_SERVE", str(_pf_serve))
os.environ.setdefault("HF_HOME", str(_real / ".cache" / "huggingface"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="keepmum-tests-")
