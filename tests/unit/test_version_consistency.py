"""Every package's exported ``__version__`` must match its pyproject version.

A published wheel self-reports through ``<pkg>.__version__``, so a version bump
that updates pyproject.toml but not the module constant ships a wheel whose
metadata and exported version disagree. This has happened: osi-orionbelt 0.1.1
was tagged while the module still exported 0.1.0.

Both packages are checked, because osi-orionbelt is versioned independently of
orionbelt-semantic-layer and is easy to miss when grepping for the main
package's version.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]

PACKAGES = [
    ("orionbelt", REPO_ROOT / "pyproject.toml"),
    ("osi_orionbelt", REPO_ROOT / "packages" / "osi-orionbelt" / "pyproject.toml"),
]


def _pyproject_version(pyproject: Path) -> str:
    with pyproject.open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


@pytest.mark.parametrize(("module_name", "pyproject"), PACKAGES)
def test_exported_version_matches_pyproject(module_name: str, pyproject: Path) -> None:
    module = pytest.importorskip(module_name)
    assert module.__version__ == _pyproject_version(pyproject), (
        f"{module_name}.__version__ is {module.__version__!r} but "
        f"{pyproject.relative_to(REPO_ROOT)} declares "
        f"{_pyproject_version(pyproject)!r}. Bump both."
    )
