import json
import os
import stat
import subprocess
import sys

import pytest

from keepmum import config as config_mod
from keepmum import launch, protect
from keepmum.protect import ProtectError


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    for k in launch.BYPASS_KEYS:
        monkeypatch.delenv(k, raising=False)
    return d


def settings(d):
    return json.loads((d / "settings.json").read_text())


def test_on_creates_the_setting_and_off_removes_it(claude_dir):
    st = protect.enable(7788)
    assert st.on and settings(claude_dir) == {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:7788"}}
    assert protect.enable(7788).on  # again: nothing changes
    st = protect.disable(7788)
    assert not st.on and settings(claude_dir) == {}


def test_other_keys_are_kept_and_a_backup_is_made(claude_dir):
    claude_dir.mkdir()
    original = {"model": "opus", "env": {"API_TIMEOUT_MS": "60000"}, "permissions": {"allow": ["Bash(ls)"]}}
    (claude_dir / "settings.json").write_text(json.dumps(original))
    os.chmod(claude_dir / "settings.json", 0o600)
    protect.enable(7788)
    s = settings(claude_dir)
    assert s["model"] == "opus" and s["permissions"] == original["permissions"]
    assert s["env"] == {"API_TIMEOUT_MS": "60000", "ANTHROPIC_BASE_URL": "http://127.0.0.1:7788"}
    assert json.loads((claude_dir / "settings.json.keepmum-backup").read_text()) == original
    assert stat.S_IMODE((claude_dir / "settings.json").stat().st_mode) == 0o600
    protect.disable(7788)
    assert settings(claude_dir) == original


def test_another_gateway_is_not_replaced(claude_dir):
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.corp.internal"}}))
    with pytest.raises(ProtectError, match="already sends Claude Code to https://gateway.corp.internal"):
        protect.enable(7788)
    assert not protect.disable(7788).on  # off leaves someone else's value alone
    assert settings(claude_dir)["env"]["ANTHROPIC_BASE_URL"] == "https://gateway.corp.internal"


def test_a_broken_file_is_never_rewritten(claude_dir):
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text('{"model": "opus",}')
    with pytest.raises(ProtectError, match="not valid JSON"):
        protect.enable(7788)
    assert (claude_dir / "settings.json").read_text() == '{"model": "opus",}'


@pytest.mark.parametrize("value, ours", [
    ("http://127.0.0.1:7788", True), ("http://localhost:7788/", True),
    ("http://127.0.0.1:7789", False), ("https://127.0.0.1:7788", False),
    ("http://127.0.0.1:7788/v1", False), ("http://evil.example:7788", False), ("not a url", False),
])
def test_is_keepmum_url(value, ours):
    assert protect.is_keepmum_url(value, 7788) == ours


def test_our_own_url_is_not_a_bypass(claude_dir, tmp_path):
    protect.enable(7788)
    project = tmp_path / "proj"
    project.mkdir()
    assert launch.bypass_risks(project, 7788) == []
    assert any("ANTHROPIC_BASE_URL" in r for r in launch.bypass_risks(project, 9999))  # another port is
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://elsewhere"}}))
    assert any("proj/.claude/settings.json" in r for r in launch.bypass_risks(project, 7788))


def test_upstream_pointing_at_ourselves_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = config_mod.load(tmp_path / "none.toml")
    proxy = launch.build_proxy(cfg, rules_only=True, upstream=f"http://127.0.0.1:{cfg.port}")
    assert proxy.upstream == cfg.upstream


def test_keepmum_claude_refuses_when_protected_and_the_service_is_down(claude_dir, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from keepmum.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    protect.enable(7788)
    monkeypatch.setattr(launch, "shared_serve_port", lambda cfg: None)
    r = CliRunner().invoke(app, ["claude", "-p", "hi"])
    assert r.exit_code == 2 and "not running" in r.output


def test_keepmum_claude_uses_the_service_when_protected(claude_dir, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from keepmum.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    protect.enable(7788)
    monkeypatch.setattr(launch, "shared_serve_port", lambda cfg: 7788)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!{sys.executable}\nimport os, sys\nopen(os.environ['OUT'], 'w').write(os.environ['ANTHROPIC_BASE_URL'] + ' ' + ' '.join(sys.argv[1:]))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OUT", str(tmp_path / "out"))
    r = CliRunner().invoke(app, ["claude", "-p", "hi"])
    assert r.exit_code == 0 and (tmp_path / "out").read_text() == "http://127.0.0.1:7788 -p hi"


def test_wrapper_refuses_when_protected_and_the_service_is_down(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "keepmum").mkdir(parents=True)
    (home / ".config" / "keepmum" / "config.toml").write_text("[server]\nport = 1\n")  # nothing listens on port 1
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"}}))
    env = {k: v for k, v in os.environ.items() if k not in launch.BYPASS_KEYS and k != "CLAUDE_CONFIG_DIR"}
    env["HOME"] = str(home)
    wrapper = os.path.join(os.path.dirname(sys.executable), "keepmum-claude-wrapper")
    r = subprocess.run([wrapper, sys.executable, "-c", "print('started')"], env=env, cwd=home,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "protection is on" in r.stderr and r.stdout == ""


# --- Excluded projects ------------------------------------------------------------------------

UP = "https://api.anthropic.com"


@pytest.fixture
def repo(tmp_path, monkeypatch, claude_dir):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    r = tmp_path / "work" / "app"
    (r / "src" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


def local(root):
    return json.loads((root / ".claude" / "settings.local.json").read_text())


def test_exclude_writes_the_repo_root_local_settings_and_keeps_it_out_of_git(repo):
    root = protect.exclude(repo / "src" / "deep", UP)
    assert root == repo.resolve()
    assert local(repo) == {"env": {"ANTHROPIC_BASE_URL": UP}}
    assert not (repo / ".claude" / "settings.local.json.keepmum-backup").exists()
    assert subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", ".claude/settings.local.json"]).returncode == 0
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True).stdout
    assert status == ""  # nothing to commit
    assert protect.is_excluded(repo / "src") == UP
    assert protect.excluded_projects() == [{"path": str(repo.resolve()), "url": UP, "active": True}]


def test_include_restores_the_file(repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    protect.exclude(repo, UP)
    protect.include(repo / "src")
    assert local(repo) == {"permissions": {"allow": ["Bash(ls)"]}}
    assert protect.is_excluded(repo) is None and protect.excluded_projects() == []
    with pytest.raises(ProtectError, match="not excluded"):
        protect.include(repo)


def test_exclude_refuses_a_tracked_file_and_another_gateway(repo):
    (repo / ".claude").mkdir()
    f = repo / ".claude" / "settings.local.json"
    f.write_text("{}")
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".claude/settings.local.json"], check=True)
    with pytest.raises(ProtectError, match="tracked by git"):
        protect.exclude(repo, UP)
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "--cached", ".claude/settings.local.json"], check=True)
    f.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.corp.internal"}}))
    with pytest.raises(ProtectError, match="already sends"):
        protect.exclude(repo, UP)


def test_a_worktree_uses_the_main_checkout(repo):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    wt = repo.parent / "app-wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
    assert protect.project_root(wt) == repo.resolve()


def test_a_folder_outside_git_is_its_own_project(tmp_path, claude_dir, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = tmp_path / "plain"
    d.mkdir()
    assert protect.exclude(d, UP) == d.resolve()
    assert protect.is_excluded(d) == UP and protect.is_excluded(tmp_path) is None


def test_hand_removed_setting_means_not_excluded(repo):
    protect.exclude(repo, UP)
    (repo / ".claude" / "settings.local.json").write_text("{}")
    assert protect.is_excluded(repo) is None
    assert protect.excluded_projects()[0]["active"] is False


def test_an_excluded_project_is_not_a_bypass_risk_but_another_value_is(repo):
    protect.exclude(repo, UP)
    assert launch.bypass_risks(repo, 7788) == []
    (repo / "src" / ".claude").mkdir()
    (repo / "src" / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://x"}}))
    assert any("src/.claude" in r for r in launch.bypass_risks(repo / "src", 7788))


def test_keepmum_claude_refuses_in_an_excluded_project(repo, monkeypatch):
    from typer.testing import CliRunner

    from keepmum.cli import app

    protect.exclude(repo, UP)
    monkeypatch.chdir(repo)
    r = CliRunner().invoke(app, ["claude", "-p", "hi"])
    assert r.exit_code == 2 and "excluded" in r.output


def test_wrapper_runs_claude_directly_in_an_excluded_project(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in launch.BYPASS_KEYS and k != "CLAUDE_CONFIG_DIR"}
    env["HOME"] = str(home)
    # Exclude through the CLI, with the same HOME the wrapper sees.
    keepmum = os.path.join(os.path.dirname(sys.executable), "keepmum")
    subprocess.run([keepmum, "exclude", str(proj)], env=env, check=True, capture_output=True)
    wrapper = os.path.join(os.path.dirname(sys.executable), "keepmum-claude-wrapper")
    r = subprocess.run([wrapper, sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_BASE_URL'))"],
                       env=env, cwd=proj, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == "None\n" and r.stderr == ""  # no proxy started, nothing injected
