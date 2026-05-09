"""Corpus loader — reads the manifest and the per-query OBML YAML files.

The corpus is split across two locations to keep query files faithful to
the OBML schema:

* ``corpus.yaml`` — the manifest. Holds ``id``, ``description``,
  ``lastVerifiedBy``, and the optional ``handSql`` test-rig metadata.
* ``queries/<id>.yaml`` — the actual query body. Each file is exactly the
  payload a user would POST to ``/v1/query/sql``; the test framework
  itself adds no schema extensions on top of OBML.

This module exposes a single ``load_corpus()`` function that hydrates
each manifest entry into a ``CorpusEntry`` with the parsed
``QueryObject`` already attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from orionbelt.models.query import QueryObject

CORRECTNESS_DIR = Path(__file__).resolve().parent
QUERIES_DIR = CORRECTNESS_DIR / "queries"
REF_SQL_DIR = CORRECTNESS_DIR / "reference_sql"
MANIFEST_PATH = CORRECTNESS_DIR / "corpus.yaml"


@dataclass(frozen=True)
class HandSqlSpec:
    """§3.3 ratifier metadata: which SQL file to compare against and how to sort."""

    ref_file: str  # filename under ``reference_sql/``
    sort_keys: list[str]

    @property
    def ref_path(self) -> Path:
        return REF_SQL_DIR / self.ref_file


@dataclass(frozen=True)
class CorpusEntry:
    """One v0 corpus query — manifest metadata + parsed OBML query."""

    id: str
    description: str
    last_verified_by: str
    query: QueryObject
    hand_sql: HandSqlSpec | None = None

    @property
    def query_path(self) -> Path:
        return QUERIES_DIR / f"{self.id}.yaml"


def _load_query(query_id: str) -> QueryObject:
    path = QUERIES_DIR / f"{query_id}.yaml"
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping, got {type(data).__name__}")
    return QueryObject.model_validate(data)


def load_corpus() -> list[CorpusEntry]:
    """Read ``corpus.yaml`` and hydrate each entry's query from disk.

    Order of returned entries matches manifest order so drift snapshot
    filenames stay stable.
    """
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh) or []
    if not isinstance(manifest, list):
        raise ValueError(
            f"{MANIFEST_PATH}: expected a top-level list, got {type(manifest).__name__}"
        )

    entries: list[CorpusEntry] = []
    seen_ids: set[str] = set()
    for raw in manifest:
        if not isinstance(raw, dict):
            raise ValueError(f"{MANIFEST_PATH}: entries must be mappings; got {raw!r}")
        try:
            entry_id = raw["id"]
            description = raw["description"]
            last_verified_by = raw["lastVerifiedBy"]
        except KeyError as exc:
            raise ValueError(
                f"{MANIFEST_PATH}: entry is missing required key {exc.args[0]!r}: {raw!r}"
            ) from None
        if entry_id in seen_ids:
            raise ValueError(f"{MANIFEST_PATH}: duplicate id {entry_id!r}")
        seen_ids.add(entry_id)

        hand_sql_raw = raw.get("handSql")
        hand_sql: HandSqlSpec | None = None
        if hand_sql_raw is not None:
            hand_sql = HandSqlSpec(
                ref_file=hand_sql_raw["refFile"],
                sort_keys=list(hand_sql_raw.get("sortKeys") or []),
            )

        entries.append(
            CorpusEntry(
                id=entry_id,
                description=description,
                last_verified_by=last_verified_by,
                query=_load_query(entry_id),
                hand_sql=hand_sql,
            )
        )
    return entries


# Eagerly-loaded list — pytest parametrize needs a stable, hashable
# collection at import time. Cheap (15 small YAML files).
CORPUS: list[CorpusEntry] = load_corpus()
