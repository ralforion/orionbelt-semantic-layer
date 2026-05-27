"""Static contract checks for shipped notebooks + integration examples.

v2.7.5 review surfaced ``examples/notebook_setup.py`` was still using
the legacy ``MODEL_FILE`` env var that was removed in v2.7.0 — the
notebook started the API, then immediately got ``HTTP 404 "No models
loaded in any session"`` from ``/v1/schema``. Same class of drift
showed up in integrations/* (LangChain, CrewAI, Vercel, ChatGPT GPT
Action), all of which document the removed env var.

These checks scan the shipped examples + integrations + Colab notebook
for known-stale patterns the runtime would reject today. They run in
the regular unit suite (no subprocess, no DuckDB) so a removed env
var or renamed query key gets caught on the same PR that removes it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

# Files whose contract has to track the live API surface. Add to this
# list when a new agent/integration example ships.
_EXAMPLE_FILES = (
    _ROOT / "examples" / "notebook_setup.py",
    _ROOT / "examples" / "quickstart_colab.ipynb",
    _ROOT / "integrations" / "langchain" / "agent_example.py",
    _ROOT / "integrations" / "langchain" / "orionbelt_tools.py",
    _ROOT / "integrations" / "crewai" / "crew_example.py",
    _ROOT / "integrations" / "crewai" / "orionbelt_tools.py",
)

# Pattern → reason (regex, what to say when it matches). All patterns
# describe surfaces that *runtime would reject today* — string-level
# match is enough to catch the regression even before someone runs the
# notebook.
_DEPRECATED_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # MODEL_FILE was removed in v2.7.0 — pydantic-settings silently
    # ignores unknown env vars so the API boots with no model loaded,
    # then every shortcut endpoint 404s.
    (
        r"\"MODEL_FILE\"\s*[:=]",
        "MODEL_FILE env var (removed in v2.7.0)",
        "Use MODEL_FILES (comma-separated). Single-entry list is the direct equivalent.",
    ),
    (
        r"^\s*env\[\"MODEL_FILE\"\]",
        "MODEL_FILE env var (removed in v2.7.0)",
        "Use MODEL_FILES (comma-separated). Single-entry list is the direct equivalent.",
    ),
)


def _file_text(path: Path) -> str:
    """Return notebook source as a single string.

    For ``.ipynb`` we extract the joined source of every code cell —
    string contracts are what matter, not Jupyter metadata.
    """
    raw = path.read_text(encoding="utf-8")
    if path.suffix != ".ipynb":
        return raw
    nb = json.loads(raw)
    parts: list[str] = []
    for cell in nb.get("cells", []):
        if cell.get("cell_type") in ("code", "markdown"):
            src = cell.get("source", [])
            if isinstance(src, list):
                parts.append("".join(src))
            else:
                parts.append(str(src))
    return "\n".join(parts)


@pytest.mark.parametrize("path", _EXAMPLE_FILES, ids=lambda p: str(p.relative_to(_ROOT)))
def test_no_deprecated_surface_references(path: Path) -> None:
    """Notebook / example must not reference any contract removed
    by a prior release. Failing pattern + suggested fix included so
    the next author knows what to do without reading three CHANGELOGs.
    """
    if not path.exists():
        pytest.skip(f"{path} not present in tree")
    text = _file_text(path)
    failures: list[str] = []
    for pattern, label, fix in _DEPRECATED_PATTERNS:
        match = re.search(pattern, text, flags=re.MULTILINE)
        if match is None:
            continue
        # Allow benign mentions where the file is explicitly *stripping*
        # the deprecated var or warning about it. Heuristic: the same
        # line also contains ``pop(`` (env scrub), ``removed in``, or
        # ``deprecated`` — likely intentional reference, not active use.
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start : (line_end if line_end != -1 else len(text))]
        if any(token in line for token in ("pop(", "removed in", "deprecated", "leaked")):
            continue
        failures.append(f"{path.name}: uses {label!r} — {fix}\n  matched line: {line.strip()!r}")
    assert not failures, "Deprecated API surface in shipped examples:\n  " + "\n  ".join(failures)


_QUERY_FILTER_DOC_FILES = (
    pytest.param(_ROOT / "examples" / "quickstart_colab.ipynb", id="quickstart_colab.ipynb"),
    pytest.param(
        _ROOT / "integrations" / "chatgpt-custom-gpt" / "openapi-gpt-action.yaml",
        id="openapi-gpt-action.yaml",
    ),
)


@pytest.mark.parametrize("path", _QUERY_FILTER_DOC_FILES)
def test_query_filter_uses_current_keys(path: Path) -> None:
    """Query examples must use the current ``QueryFilter`` field names:
    ``field`` + ``op``, not the removed ``dimension`` + ``operator``
    pair flagged in review finding #86.

    ``MeasureFilter.operator`` is a distinct model field with a leading
    ``column: {...}`` sibling — the regex below only matches the
    QueryFilter pattern where ``- field:``/``- dimension:`` heads a
    list item.
    """
    if not path.exists():
        pytest.skip(f"{path} not present")
    text = _file_text(path) if path.suffix == ".ipynb" else path.read_text(encoding="utf-8")
    # ``- dimension:`` heading a query filter list item
    bad_dim = re.findall(r"^\s*- dimension:\s+", text, flags=re.MULTILINE)
    # The query-filter ``operator:`` form is the one that follows a
    # ``- field:`` or ``- dimension:`` sibling at the same indent —
    # MeasureFilter's ``operator:`` sits under ``column: {...}`` (no
    # leading dash on the column line).
    bad_query_operator = re.findall(
        r"(?m)^(\s*)- (?:field|dimension):.*\n\1  operator:\s+",
        text,
    )
    assert not bad_dim, (
        f"{path.name}: uses removed query key ``- dimension:`` in {len(bad_dim)} place(s) — "
        "should be ``- field:`` (QueryFilter.field)."
    )
    assert not bad_query_operator, (
        f"{path.name}: uses removed query key ``operator:`` (alongside field/dimension) "
        f"in {len(bad_query_operator)} place(s) — should be ``op:`` (QueryFilter.op)."
    )


def test_notebook_setup_module_imports_cleanly() -> None:
    """``examples/notebook_setup.py`` must import without errors.

    The module gets imported by tutorial notebooks; a syntax error or
    a stale import would make every notebook exec fail at cell 1.
    """
    import importlib.util

    path = _ROOT / "examples" / "notebook_setup.py"
    if not path.exists():
        pytest.skip(f"{path} not present")
    spec = importlib.util.spec_from_file_location("_nb_setup_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Must expose the documented helpers.
    assert callable(getattr(mod, "start_api", None)), "start_api() missing"
    assert callable(getattr(mod, "api", None)), "api() helper missing"


def test_colab_install_cell_is_idempotent() -> None:
    """The Colab install cell must check what's already importable
    before running pip install (issue #87). Pre-fix used
    ``pip install -q ...`` unconditionally — that swallowed real
    errors AND conflicted with CI's pre-installed working tree,
    failing every PR run of the notebook smoke workflow.
    """
    path = _ROOT / "examples" / "quickstart_colab.ipynb"
    if not path.exists():
        pytest.skip(f"{path} not present")
    text = _file_text(path)
    assert "find_spec" in text, (
        "Colab install cell must check ``importlib.util.find_spec`` before "
        "running pip install — otherwise CI's pre-installed working tree "
        "collides with the published PyPI version. See #87."
    )
    # ``pip install -q`` swallows real errors; banned in shipped notebooks.
    assert 'pip", "install", "-q"' not in text, (
        "Colab install cell must not use ``pip install -q`` — real pip "
        "errors cascade into NameErrors on subsequent cells and waste a "
        "user's first 10 minutes. See #87."
    )


def test_colab_required_map_uses_correct_import_names() -> None:
    """The Colab install cell's ``_REQUIRED`` map keys must be IMPORT
    names (what ``importlib.util.find_spec`` resolves), not distribution
    names. ``ob-flight-extension`` ships its module as ``ob_flight``
    (not ``ob_flight_extension``); getting this wrong made the
    idempotent check always say "missing" on CI, forcing the pip
    fallback path that fails inside uv-managed venvs. See #94.
    """
    path = _ROOT / "examples" / "quickstart_colab.ipynb"
    if not path.exists():
        pytest.skip(f"{path} not present")
    text = _file_text(path)
    assert '"ob_flight":' in text, (
        "_REQUIRED map must use ``ob_flight`` (the actual module name "
        "shipped by ob-flight-extension), not ``ob_flight_extension`` "
        "(the distribution name). See #94."
    )
    assert '"ob_flight_extension":' not in text, (
        "_REQUIRED map still maps the distribution name "
        "``ob_flight_extension`` as an import key — the module name is "
        "``ob_flight``. See #94."
    )


def test_notebook_workflow_installs_pip_into_uv_venv() -> None:
    """The notebook workflow must ``uv pip install pip`` after
    ``uv sync`` so the notebook's ``sys.executable -m pip install``
    fallback path works inside the uv-managed venv (which omits pip
    by default). Without this, any cell hitting the install branch
    dies with ``No module named pip`` and cascades into NameErrors.
    See #94.
    """
    path = _ROOT / ".github" / "workflows" / "notebook.yml"
    if not path.exists():
        pytest.skip(f"{path} not present")
    text = path.read_text(encoding="utf-8")
    assert "uv pip install pip" in text, (
        "notebook.yml must install pip into the uv venv "
        "(``uv pip install pip``) so the notebook's pip fallback works. "
        "See #94."
    )


def test_notebook_setup_uses_model_files_env() -> None:
    """``start_api`` must set ``MODEL_FILES`` (not the removed
    ``MODEL_FILE``) so the API enters admin-curated mode and shortcut
    endpoints resolve. This is the exact bug from the v2.7.5 review.
    """
    path = _ROOT / "examples" / "notebook_setup.py"
    if not path.exists():
        pytest.skip(f"{path} not present")
    src = path.read_text(encoding="utf-8")
    # Find the env dict literal — must include MODEL_FILES, must not
    # include the removed MODEL_FILE as an active key.
    assert '"MODEL_FILES"' in src, "notebook_setup.py must set MODEL_FILES env var"
    # Active MODEL_FILE assignment regex — would catch
    # ``"MODEL_FILE": ...`` but not the comment about its removal.
    active_assignment = re.search(r'^\s*"MODEL_FILE"\s*:\s*', src, flags=re.MULTILINE)
    assert active_assignment is None, (
        "notebook_setup.py still assigns the removed MODEL_FILE env var — "
        "use MODEL_FILES (single-entry comma-separated list is the equivalent)."
    )
