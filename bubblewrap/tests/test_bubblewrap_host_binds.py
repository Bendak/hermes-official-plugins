"""Host files named by configuration or the environment, bound into a real bwrap sandbox.

A CA bundle variable (SSL_CERT_FILE and the rest of CA_BUNDLE_VARS) and an
operator bind name a host path that bwrap mounts. Each one is resolved once
at construction and checked: it must be a regular file (a directory where a
directory is expected), it must not resolve into a path the sandbox hides,
and it must not lie in a directory a sandbox can write to, where a command
could swap it before the next spawn. A refused path is skipped with one
warning and the sandbox still runs. These tests run real commands against a
fake HOME that holds dummy credential files. Skipped where bwrap cannot run.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from bubblewrap_plugin import sandbox
from bubblewrap_plugin.sandbox import CA_BUNDLE_VARS, BubblewrapEnvironment, load_bubblewrap_config


def _bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    try:
        probe = subprocess.run(
            ["bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _bwrap_usable(), reason="bwrap missing or its namespace probe failed")

SECRET = "DUMMY-CREDENTIAL-MARKER"
ANCHORS = "DUMMY-TRUST-ANCHORS"


@pytest.fixture
def host_dir(tmp_path):
    """A directory the sandbox sees at its host path (every sandbox mounts a fresh /tmp)."""
    if not str(tmp_path.resolve()).startswith("/tmp/"):
        yield tmp_path
        return
    base = Path(tempfile.mkdtemp(prefix="hermes-bwrap-", dir="/var/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def home(host_dir, monkeypatch):
    """A fake HOME with dummy credentials, a fake Hermes home and the Python env Hermes runs from."""
    home = host_dir / "home"
    secrets = (".ssh/id_ed25519", ".unlisted-tool/token", ".hermes/.env", ".hermes/auth.json")
    for rel in secrets:
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{SECRET} {rel}\n")
    pyenv = home / ".hermes" / "hermes-agent" / "venv"
    bundle = pyenv / "lib" / "certifi" / "cacert.pem"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(f"{ANCHORS}\n")
    (home / "project").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)
    for name in CA_BUNDLE_VARS:
        monkeypatch.delenv(name, raising=False)
    # Inside the hidden Hermes home only the Python environment Hermes runs from may be bound back;
    # here that is the fake venv above, not the interpreter running the tests.
    monkeypatch.setattr(sandbox, "python_env_roots", lambda: (str(pyenv),), raising=False)
    return home


def _bundle(home: Path) -> Path:
    return home / ".hermes" / "hermes-agent" / "venv" / "lib" / "certifi" / "cacert.pem"


def _env(home: Path, settings: dict | None = None) -> BubblewrapEnvironment:
    return BubblewrapEnvironment(cwd=str(home / "project"), timeout=30, config=load_bubblewrap_config(settings or {}))


def _refusals(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "not binding" in r.getMessage()]


def test_ca_bundle_in_the_hermes_python_env_is_bound_and_readable(home, monkeypatch, caplog):
    """The trust anchors Hermes runs with come back read-only; the rest of the Hermes home stays hidden."""
    monkeypatch.setenv("SSL_CERT_FILE", str(_bundle(home)))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        out = env.execute('cat "$SSL_CERT_FILE"; cat "$HERMES_HOME/.env" 2>&1')["output"]
        written = env.execute('echo x >> "$SSL_CERT_FILE"')["returncode"]
    finally:
        env.cleanup()
    assert ANCHORS in out
    assert SECRET not in out
    assert written != 0
    assert _refusals(caplog) == []


@pytest.mark.parametrize("spelled, target", [
    ("certs/ca.pem", ".ssh/id_ed25519"),
    (".hermes/certs/ca.pem", ".unlisted-tool/token"),
    (".hermes/certs/ca.pem", ".hermes/.env"),
])
def test_ca_variable_resolving_into_a_hidden_path_is_skipped(home, monkeypatch, caplog, spelled, target):
    """A CA path that links into a credential store, a hidden dot entry or the Hermes home's secrets."""
    link = home.parent / spelled if not spelled.startswith(".hermes") else home / spelled
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(home / target)
    monkeypatch.setenv("SSL_CERT_FILE", str(link))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        out = env.execute('cat "$SSL_CERT_FILE" 2>&1; echo ran')["output"]
    finally:
        env.cleanup()
    assert SECRET not in out
    assert "ran" in out
    assert any("SSL_CERT_FILE" in message for message in _refusals(caplog))


def test_ca_directory_variable_naming_the_hermes_home_is_skipped(home, monkeypatch, caplog):
    monkeypatch.setenv("SSL_CERT_DIR", str(home / ".hermes"))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        out = env.execute('cat "$SSL_CERT_DIR/.env" "$SSL_CERT_DIR/auth.json" 2>&1')["output"]
    finally:
        env.cleanup()
    assert SECRET not in out
    assert any("SSL_CERT_DIR" in message for message in _refusals(caplog))


def test_ca_file_in_a_directory_the_sandbox_writes_is_skipped(home, monkeypatch, caplog):
    """The bundle resolves into the writable working directory, where a command could swap it."""
    in_cwd = home / "project" / "ca.pem"
    in_cwd.write_text(f"{ANCHORS}\n")
    link = home / ".hermes" / "certs" / "ca.pem"
    link.parent.mkdir(parents=True)
    link.symlink_to(in_cwd)
    monkeypatch.setenv("SSL_CERT_FILE", str(link))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        env.execute(f"rm -f {in_cwd} && ln -s {home / '.ssh' / 'id_ed25519'} {in_cwd}")
        out = env.execute('cat "$SSL_CERT_FILE" 2>&1; echo ran')["output"]
    finally:
        env.cleanup()
    assert SECRET not in out
    assert "ran" in out
    assert any("SSL_CERT_FILE" in message for message in _refusals(caplog))


def test_a_ca_link_the_sandbox_rewrites_cannot_steer_the_next_mount(home, monkeypatch):
    """The bundle is bound from the path it resolved to at construction, never through the link."""
    link = home / "project" / "ca.pem"
    link.symlink_to(_bundle(home))
    monkeypatch.setenv("SSL_CERT_FILE", str(link))
    env = _env(home)
    try:
        first = env.execute('cat "$SSL_CERT_FILE"')["output"]
        env.execute(f"rm -f {link} && ln -s {home / '.ssh' / 'id_ed25519'} {link}")
        second = env.execute(f'cat "$SSL_CERT_FILE" {_bundle(home)} 2>&1')["output"]
    finally:
        env.cleanup()
    assert ANCHORS in first
    assert SECRET not in second


def test_unset_and_missing_ca_variables_bind_nothing_and_warn_nothing(home, monkeypatch, caplog):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(home / ".hermes" / "missing.pem"))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        result = env.execute("echo ok")
    finally:
        env.cleanup()
    assert result["returncode"] == 0 and "ok" in result["output"]
    assert _refusals(caplog) == []


def test_read_only_bind_from_inside_the_writable_directory_cannot_be_steered(home, host_dir, caplog):
    """A source a command can replace would choose what the next spawn mounts; it is skipped."""
    data = home / "project" / "data"
    data.mkdir()
    (data / "readme").write_text("public data\n")
    mnt = host_dir / "mnt"
    mnt.mkdir()
    caplog.set_level(logging.WARNING)
    env = _env(home, {"binds": [{"src": str(data), "dest": str(mnt)}]})
    try:
        env.execute(f"rm -rf {data} && ln -s {home / '.unlisted-tool'} {data}")
        out = env.execute(f"cat {mnt}/token 2>&1; echo ran")["output"]
    finally:
        env.cleanup()
    assert SECRET not in out
    assert "ran" in out
    assert any(str(data) in message for message in _refusals(caplog))


def test_read_only_binds_from_a_fixed_place_or_at_their_own_path_still_work(home, host_dir, caplog):
    shared = host_dir / "shared"
    shared.mkdir()
    (shared / "readme").write_text("shared data\n")
    data = home / "project" / "data"
    data.mkdir()
    (data / "readme").write_text("project data\n")
    mnt = host_dir / "mnt"
    mnt.mkdir()
    caplog.set_level(logging.WARNING)
    env = _env(home, {"binds": [{"src": str(shared), "dest": str(mnt)}, {"src": str(data)}]})
    try:
        out = env.execute(f"cat {mnt}/readme {data}/readme")["output"]
        written = env.execute(f"echo x >> {data}/readme")["returncode"]
    finally:
        env.cleanup()
    assert "shared data" in out and "project data" in out
    assert written != 0
    assert _refusals(caplog) == []
