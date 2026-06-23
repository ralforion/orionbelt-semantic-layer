"""Every tracked example / demo / fixture model and query validates against
the published JSON schema (drift guard for shipped OBML).

The model-load and query endpoints now JSON-schema-validate their input, so
anything we ship as an example, demo, or fixture must itself satisfy that
contract -- otherwise a user copying it would hit a 422. This walks the
git-tracked YAML, classifies each document as a model (has ``dataObjects``)
or a query (has ``select``), validates it, and fails on any unexpected
violation.

Documents that are neither (extends *fragments*, OSI files, other YAML) are
skipped. Tracked files that are intentionally schema-invalid (negative-test
fixtures) go in ``ALLOWLIST`` with a reason; it is empty today because every
tracked model/query is schema-valid.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from orionbelt.parser.schema_validation import validate_obml_document, validate_query_document

REPO_ROOT = Path(__file__).resolve().parents[2]

# Tracked YAML that is intentionally schema-invalid (path -> reason). Empty:
# every tracked model/query currently validates. Add an entry (with a reason)
# only for a deliberate negative-test fixture.
ALLOWLIST: dict[str, str] = {}


def _tracked_yaml() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.yml", "*.yaml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def test_tracked_example_models_and_queries_validate() -> None:
    checked = 0
    failures: list[str] = []
    for rel in _tracked_yaml():
        if rel.startswith(("schema/", ".github/")):
            continue
        path = REPO_ROOT / rel
        if "docker-compose" in path.name or ".minio" in rel:
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue

        if "dataObjects" in doc or "data_objects" in doc:
            errors = validate_obml_document(doc)
        elif "select" in doc:
            errors = validate_query_document(doc)
        else:
            continue  # extends fragment / OSI / unrelated YAML

        checked += 1
        if errors and rel not in ALLOWLIST:
            msg = "; ".join(getattr(e, "message", str(e)) for e in errors[:3])
            failures.append(f"{rel}: {msg}")

    assert checked > 0, "no tracked models/queries discovered (git ls-files issue?)"
    assert not failures, (
        "Tracked example/demo/fixture OBML fails JSON-schema validation "
        "(a user copying it would get a 422). Fix the file, or add it to "
        "ALLOWLIST with a reason if it is an intentional negative-test fixture:\n"
        + "\n".join(failures)
    )
