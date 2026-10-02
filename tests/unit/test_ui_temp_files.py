"""The UI does not keep a copy of every result in the container's memory.

Each run writes its result to a temp dir for the download button. Nothing
removed those dirs, and on Cloud Run ``/tmp`` is in memory, so the UI grew
until it hit its limit (the "Memory limit of 512 MiB exceeded" restarts).
Old result dirs are now pruned on every run, and Gradio sweeps its own cache.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("gradio", reason="gradio required by the UI handlers")

from orionbelt.ui import handlers  # noqa: E402

_TSV = 7  # tsv_path slot in the execute_query tuple


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(handlers.tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _make_dir(root: Path, name: str, age_seconds: float) -> Path:
    path = root / name
    path.mkdir()
    (path / "query_results.tsv").write_text("a\n1\n")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_old_result_dirs_are_removed(tmp_root: Path) -> None:
    old = _make_dir(tmp_root, "obsl_tsv_old", 20 * 60)
    handlers._prune_result_tsv_dirs()
    assert not old.exists()


def test_recent_result_dirs_are_kept(tmp_root: Path) -> None:
    """A dir younger than ten minutes may still be waiting for Gradio to copy it."""
    recent = _make_dir(tmp_root, "obsl_tsv_recent", 60)
    handlers._prune_result_tsv_dirs()
    assert recent.exists()


def test_other_temp_files_are_untouched(tmp_root: Path) -> None:
    other = _make_dir(tmp_root, "gradio_or_anything_else", 20 * 60)
    handlers._prune_result_tsv_dirs()
    assert other.exists()


def test_a_run_prunes_old_results_and_keeps_its_own(tmp_root: Path) -> None:
    old = _make_dir(tmp_root, "obsl_tsv_old", 20 * 60)
    response = MagicMock(status_code=200)
    data = {
        "sql": "SELECT 1 AS a",
        "columns": [{"name": "a", "type": "integer"}],
        "rows": [[1]],
        "row_count": 1,
        "execution_time_ms": 1.0,
    }
    client = MagicMock()
    client.post.return_value = response
    with (
        patch.object(
            handlers,
            "_ensure_session_and_model",
            return_value=(client, "sid", "mid", {"s": "1"}, {"m": "1"}),
        ),
        patch.object(handlers, "_decode_arrow_execute_response", return_value=data),
    ):
        result = handlers.execute_query(
            "version: 1.0", "select: {fields: [a]}", "duckdb", "http://unused", None, None
        )
    tsv = Path(result[_TSV])
    assert tsv.exists() and tsv.read_text().startswith("a")
    assert tsv.parent.parent == tmp_root
    assert not old.exists()
