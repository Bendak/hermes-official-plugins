"""Test bootstrap: import the plugin directory as the package ``elevenlabs_sfx_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``hermes_cli``, ``tools``, ``agent``) must be importable: run with a hermes-agent checkout on
``PYTHONPATH``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "elevenlabs_sfx_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the plugin dir puts it on sys.path, where its modules could shadow core.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_load_plugin_package()


@pytest.fixture
def plugin_root() -> Path:
    return _ROOT


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    """Every test gets its own HOME and profile home; nothing touches the real ones."""
    home = tmp_path / "os-home" / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    return home


class _RepoRootIsNotATestPackage:
    """The plugin dir *is* a package (its ``__init__.py`` has relative imports), so pytest would
    collect it as a test ``Package`` and import that ``__init__.py`` standalone. Collect it as a plain
    directory instead (registered globally: the root's collector comes from its parent's hook proxy)."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("elevenlabs-sfx-root-as-dir"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "elevenlabs-sfx-root-as-dir")
