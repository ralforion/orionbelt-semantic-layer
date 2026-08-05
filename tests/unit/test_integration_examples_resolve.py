"""Every query example shipped in ``integrations/`` must resolve.

These examples are what a user copies first. They referenced ``Country`` and
``Revenue``, names no bundled model has ever exposed, so the very first request
of each quickstart answered ``UNKNOWN_SELECT_ITEM``. Nothing noticed because
nothing compiled them: they live in an OpenAPI spec, an n8n workflow's JSON
string body, and fenced blocks in Markdown.

Two rounds of fixing these by grep each missed instances, which is the argument
for extracting them mechanically instead.
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
from orionbelt.models.query import QueryObject
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_REPO = Path(__file__).resolve().parents[2]
_INTEGRATIONS = _REPO / "integrations"
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
    return out


_EXAMPLES = _collect()


@pytest.fixture(scope="module")
def model():
    raw, src = TrackedLoader().load(_MODEL)
    resolved, result = ReferenceResolver().resolve(raw, src)
    assert resolved is not None, result.errors
    return resolved


def test_examples_were_actually_found() -> None:
    """Guard the guard: a broken extractor would make every case below vacuous."""
    assert len(_EXAMPLES) >= 3, f"expected to find query examples, got {len(_EXAMPLES)}"


@pytest.mark.parametrize(("label", "query"), _EXAMPLES, ids=[label for label, _ in _EXAMPLES])
def test_a_shipped_example_resolves(model, label: str, query: dict) -> None:
    """The names in it must exist in the model the quickstart tells you to load."""
    CompilationPipeline().compile(QueryObject(**query), model, "postgres")
