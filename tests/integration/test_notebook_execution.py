"""End-to-end notebook execution smoke test (v2.7.5+).

Static contract checks live in ``tests/unit/test_notebook_contracts.py``
— this file goes one step further and *actually runs* the shipped
Colab quickstart notebook headlessly via nbclient. Catches:

* Runtime errors that static checks can't see (e.g. an API endpoint
  the notebook calls but doesn't reference by its full name).
* Cell-ordering bugs (notebook depends on side effects from earlier
  cells that get reordered during edits).
* Display-only HTML/Mermaid cells that swallow errors silently.

Marker-gated because the notebook is heavy (installs deps, generates
the TPC-H DuckDB benchmark, runs ~50 cells, each compiling + executing
real SQL). Run with::

    uv run pytest -m notebook

Skips automatically when ``nbclient`` / ``duckdb`` / ``ipykernel``
aren't installed so the suite stays portable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.notebook

_ROOT = Path(__file__).resolve().parents[2]
_COLAB = _ROOT / "examples" / "quickstart_colab.ipynb"


@pytest.fixture(scope="module")
def _nbclient_or_skip():
    nbclient = pytest.importorskip("nbclient", reason="nbclient required for notebook execution")
    nbformat = pytest.importorskip("nbformat", reason="nbformat required for notebook execution")
    pytest.importorskip("ipykernel", reason="ipykernel required for notebook execution")
    pytest.importorskip("duckdb", reason="duckdb required by the Colab quickstart")
    return nbclient, nbformat


def test_quickstart_colab_executes_cleanly(_nbclient_or_skip, tmp_path) -> None:
    """Execute every cell of ``examples/quickstart_colab.ipynb``.

    This is the same notebook published on Colab — a green run here
    means agents / new users following the published quickstart will
    succeed end-to-end against the current API surface. Catches the
    class of regression reviewed in finding #6 + #86 (stale env var,
    removed endpoints, renamed QueryFilter keys) in the *real
    execution* path, not just static text matching.
    """
    nbclient, nbformat = _nbclient_or_skip
    if not _COLAB.exists():
        pytest.skip(f"{_COLAB} not present in tree")

    nb = nbformat.read(str(_COLAB), as_version=4)
    # Run in a clean working directory so the notebook's relative paths
    # (``tpch.duckdb``, ``tpch.obml.yml``, ``api.log``) don't collide
    # with the repo's checked-in files or pollute the workspace.
    client = nbclient.NotebookClient(
        nb,
        timeout=600,  # 10 min cap — Colab cells include API startup + SQL execution
        kernel_name="python3",
        resources={"metadata": {"path": str(tmp_path)}},
        # Don't fail the whole notebook if one cell raises — capture
        # every failure with its index so the user sees the full set.
        allow_errors=True,
    )
    client.execute()

    failures: list[tuple[int, str]] = []
    for idx, cell in enumerate(nb.cells):
        if cell.get("cell_type") != "code":
            continue
        for out in cell.get("outputs", []):
            if out.get("output_type") == "error":
                tb = "\n".join(out.get("traceback", []))
                failures.append((idx, f"{out.get('ename')}: {out.get('evalue')}\n{tb[-1500:]}"))
                break
    assert not failures, "Colab notebook had errors in:\n" + "\n---\n".join(
        f"cell #{i}:\n{msg}" for i, msg in failures
    )
