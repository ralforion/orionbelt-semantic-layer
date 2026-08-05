"""The release guard that would have caught the 2.24.0 converter publish gap.

``scripts/check_osi_version_bump.py`` fails a PR that edits the packaged
converter source while leaving its version on something PyPI already has,
which is the state that makes ``skip-existing`` upload nothing and still
report success. These cover the decision logic with PyPI and git stubbed;
the network and git plumbing themselves are exercised by running it in CI.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_osi_version_bump.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_osi_version_bump", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load()
    monkeypatch.setattr(sys, "argv", ["check_osi_version_bump.py"])
    return module


def _configure(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    changed: bool,
    version: str,
    released: set[str] | None,
    base_version: str = "0.1.2",
    dunder: str | None = None,
) -> None:
    monkeypatch.setattr(guard, "source_changed", lambda _base: changed)
    monkeypatch.setattr(
        guard, "declared_version", lambda ref=None: base_version if ref else version
    )
    monkeypatch.setattr(guard, "dunder_version", lambda: dunder if dunder is not None else version)
    monkeypatch.setattr(guard, "published_versions", lambda: released)


def test_the_2_24_0_shape_fails(guard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Source edited, version left on one PyPI already has: the actual defect."""
    _configure(guard, monkeypatch, changed=True, version="0.1.2", released={"0.1.1", "0.1.2"})
    assert guard.main() == 1


def test_a_bump_to_an_unpublished_version_passes(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(guard, monkeypatch, changed=True, version="0.2.0", released={"0.1.1", "0.1.2"})
    assert guard.main() == 0


def test_further_edits_to_an_already_bumped_version_pass(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once 0.2.0 is landed but unpublished, more work on it needs no second bump.

    This is why the rule is "not already on PyPI" rather than "changed in this
    PR": the latter would demand a bump per PR between releases.
    """
    _configure(
        guard,
        monkeypatch,
        changed=True,
        version="0.2.0",
        released={"0.1.2"},
        base_version="0.2.0",
    )
    assert guard.main() == 0


def test_untouched_source_needs_no_bump(guard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(guard, monkeypatch, changed=False, version="0.1.2", released={"0.1.2"})
    assert guard.main() == 0


def test_disagreeing_version_files_fail_even_without_a_source_change(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pyproject and __init__ out of step: the runtime version lies about the code."""
    _configure(
        guard, monkeypatch, changed=False, version="0.2.0", released={"0.1.2"}, dunder="0.1.2"
    )
    assert guard.main() == 1


def test_an_unreachable_pypi_still_fails_an_unbumped_version(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline fallback: no PyPI, so an unchanged version cannot be cleared."""
    _configure(
        guard,
        monkeypatch,
        changed=True,
        version="0.1.2",
        released=None,
        base_version="0.1.2",
    )
    assert guard.main() == 1


def test_an_unreachable_pypi_accepts_a_version_that_moved(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(
        guard,
        monkeypatch,
        changed=True,
        version="0.2.0",
        released=None,
        base_version="0.1.2",
    )
    assert guard.main() == 0


def test_the_real_repo_state_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped pyproject and __init__ agree - the check's own precondition."""
    guard = _load()
    assert guard.declared_version() == guard.dunder_version()
