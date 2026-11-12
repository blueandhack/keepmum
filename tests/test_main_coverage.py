"""`python -m keepmum` runs the CLI under keepmum's own name."""

import runpy

from keepmum import cli


def test_python_m_keepmum_runs_the_cli_as_keepmum(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "app", lambda **kw: calls.append(kw))
    runpy.run_module("keepmum", run_name="__main__")
    assert calls == [{"prog_name": "keepmum"}]
