"""Every shipped query payload must resolve against the bundled model.

Two sources, same failure mode. The ``integrations/`` examples are what a user
copies first, and they referenced ``Country`` and ``Revenue``, names no bundled
model has ever exposed, so the first request of each quickstart answered
``UNKNOWN_SELECT_ITEM``. The ``tests/docker`` and ``tests/cloudrun`` suites hold
payloads too, and those are worse off: they need a running container or a live
deployment, so CI never runs them and a stale name there sits green until
somebody runs the suite by hand.

Nothing compiled any of it, because it lives in an OpenAPI spec, an n8n
workflow's JSON string body, fenced Markdown blocks, and curl bodies inside
shell scripts. Three rounds of correcting these by grep each missed instances,
which is the argument for extracting them mechanically.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_REPO = Path(__file__).resolve().parents[2]
_INTEGRATIONS = _REPO / "integrations"
_SHELL_SUITES = (_REPO / "tests" / "docker", _REPO / "tests" / "cloudrun")
_MODEL = _REPO / "examples" / "orionbelt_1_commerce.yaml"


def _looks_like_a_query(obj: Any) -> bool:
    """A query object is a mapping with a ``select`` carrying dims or measures."""
    if not isinstance(obj, dict):
        return False
    select = obj.get("select")
    return isinstance(select, dict) and bool(
        select.get("dimensions") or select.get("measures") or select.get("fields")
    )


def _walk(node: Any) -> list[dict]:
    """Every query-shaped mapping anywhere inside a parsed document."""
    found: list[dict] = []
    if _looks_like_a_query(node):
        found.append(node)
    if isinstance(node, dict):
        for value in node.values():
            found.extend(_walk(value))
        # n8n embeds the request body as a JSON *string*.
        body = node.get("jsonBody")
        if isinstance(body, str):
            with contextlib.suppress(json.JSONDecodeError):
                found.extend(_walk(json.loads(body)))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk(value))
    return found


def _json_objects_in_shell(text: str) -> list[dict]:
    """Every JSON object containing a ``select`` embedded in a shell script.

    The payloads sit in curl bodies, sometimes escaped (``\\"measures\\"``) where
    the script nests them inside a larger JSON string. Brace-matching from each
    ``{`` that opens an object mentioning ``"select"`` is enough, and anything
    that does not parse is skipped rather than guessed at.
    """
    plain = text.replace('\\"', '"')
    found: list[dict] = []
    for start in (m.start() for m in re.finditer(r"\{", plain)):
        depth = 0
        for end, ch in enumerate(plain[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blob = plain[start : end + 1]
                    if '"select"' in blob:
                        with contextlib.suppress(json.JSONDecodeError):
                            parsed = json.loads(blob)
                            if _looks_like_a_query(parsed):
                                found.append(parsed)
                    break
    return found


def _collect() -> list[tuple[str, dict]]:
    """(source label, query) for every example under integrations/."""
    out: list[tuple[str, dict]] = []
    yaml = YAML(typ="safe")
    for path in sorted(_INTEGRATIONS.rglob("*")):
        if path.suffix in {".json", ".yaml", ".yml"}:
            try:
                doc = (
                    json.loads(path.read_text())
                    if path.suffix == ".json"
                    else yaml.load(path.read_text())
                )
            except Exception:  # noqa: BLE001 - a malformed doc is another test's problem
                continue
            for i, q in enumerate(_walk(doc)):
                out.append((f"{path.relative_to(_REPO)}#{i}", q))
        elif path.suffix == ".md":
            for i, block in enumerate(
                re.findall(r"```(?:json)?\n(.*?)```", path.read_text(), re.S)
            ):
                try:
                    doc = json.loads(block)
                except json.JSONDecodeError:
                    continue
                for j, q in enumerate(_walk(doc)):
                    out.append((f"{path.relative_to(_REPO)}#{i}.{j}", q))
    for suite in _SHELL_SUITES:
        for path in sorted(suite.glob("*.sh")):
            for i, q in enumerate(_json_objects_in_shell(path.read_text())):
                out.append((f"{path.relative_to(_REPO)}#{i}", q))
    return out


_EXAMPLES = _collect()

# The shell suites deliberately send unresolvable names to assert a 4xx. Those
# payloads are held to the opposite expectation rather than skipped.
_SENTINELS = ("NonExistent", "Fake Measure")


def _is_deliberately_invalid(query: dict) -> bool:
    return any(s in json.dumps(query) for s in _SENTINELS)


_POSITIVE = [(label, q) for label, q in _EXAMPLES if not _is_deliberately_invalid(q)]
_NEGATIVE = [(label, q) for label, q in _EXAMPLES if _is_deliberately_invalid(q)]


@pytest.fixture(scope="module")
def model():
    raw, src = TrackedLoader().load(_MODEL)
    resolved, result = ReferenceResolver().resolve(raw, src)
    assert resolved is not None, result.errors
    return resolved


def test_examples_were_actually_found() -> None:
    """Guard the guard: a broken extractor would make every case below vacuous."""
    assert len(_EXAMPLES) >= 3, f"expected to find query examples, got {len(_EXAMPLES)}"
    assert _NEGATIVE, "the deliberately-invalid payloads disappeared from the shell suites"


@pytest.mark.parametrize(("label", "query"), _POSITIVE, ids=[label for label, _ in _POSITIVE])
def test_a_shipped_example_resolves(model, label: str, query: dict) -> None:
    """The names in it must exist in the model the quickstart tells you to load."""
    CompilationPipeline().compile(QueryObject(**query), model, "postgres")


@pytest.mark.parametrize(("label", "query"), _NEGATIVE, ids=[label for label, _ in _NEGATIVE])
def test_a_deliberately_invalid_payload_still_fails(model, label: str, query: dict) -> None:
    """The shell suites assert a 4xx for these, so they must stay unresolvable.

    Checked rather than merely excluded: if one of these names ever became real,
    the suite's error-handling case would pass for the wrong reason.
    """
    with pytest.raises(ResolutionError):
        CompilationPipeline().compile(QueryObject(**query), model, "postgres")
