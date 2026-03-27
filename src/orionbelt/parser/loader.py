"""YAML loader with position tracking for rich error reporting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from orionbelt.models.errors import SourceSpan

# ---------------------------------------------------------------------------
# Safety limits
# ---------------------------------------------------------------------------

_MAX_DOCUMENT_SIZE = 5_000_000  # 5M characters
_MAX_NODE_COUNT = 50_000
_MAX_DEPTH = 20

# Regex to detect YAML anchor definitions (&name) on non-comment lines.
# Matches & at line start or after whitespace/sequence indicators, followed by
# an anchor name.
_ANCHOR_RE = re.compile(r"(?:^|[\s\-:])&(\w+)", re.MULTILINE)
# Lines starting with optional whitespace + # are full-line comments.
_COMMENT_LINE_RE = re.compile(r"^\s*#.*$", re.MULTILINE)


class YAMLSafetyError(Exception):
    """Raised when YAML input violates safety constraints.

    Distinct from parse errors — these indicate potentially malicious input
    (e.g., billion-laughs anchors, excessive nesting, oversized documents).
    """


@dataclass
class SourceMap:
    """Maps YAML key paths to their source positions for error reporting."""

    _positions: dict[str, SourceSpan] = field(default_factory=dict)

    def add(self, path: str, span: SourceSpan) -> None:
        self._positions[path] = span

    def get(self, path: str) -> SourceSpan | None:
        return self._positions.get(path)

    def merge(self, other: SourceMap) -> None:
        self._positions.update(other._positions)

    @property
    def paths(self) -> list[str]:
        return list(self._positions.keys())


class TrackedLoader:
    """YAML loader that tracks source positions for error reporting.

    Uses ruamel.yaml which preserves line/column info on every parsed node.
    """

    def __init__(self) -> None:
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        # Reject duplicate YAML keys (e.g. two columns with the same name).
        # Without this, ruamel.yaml silently keeps only the last value.
        self._yaml.allow_duplicate_keys = False
        # Reject deeply nested structures (mitigates stack-based DoS).
        # ruamel.yaml raises an error when nesting exceeds this limit.
        self._yaml.max_depth = _MAX_DEPTH

    # -- safety checks -------------------------------------------------------

    @staticmethod
    def _check_yaml_safety(content: str) -> None:
        """Pre-parse safety checks on raw YAML text.

        Raises ``YAMLSafetyError`` if the content contains anchors/aliases
        (not used in OBML) or exceeds the maximum document size.
        """
        if len(content) > _MAX_DOCUMENT_SIZE:
            raise YAMLSafetyError(
                f"YAML document exceeds maximum size "
                f"({len(content):,} chars > {_MAX_DOCUMENT_SIZE:,} limit)"
            )
        # Strip full-line comments before scanning so that &name inside
        # comments (e.g. "# see R&D notes") does not cause a false positive.
        stripped = _COMMENT_LINE_RE.sub("", content)
        if _ANCHOR_RE.search(stripped):
            raise YAMLSafetyError("YAML anchors/aliases are not supported in OBML")

    @staticmethod
    def _check_node_count(data: Any, limit: int = _MAX_NODE_COUNT) -> None:
        """Post-parse defense-in-depth: reject documents with too many nodes."""
        count = 0
        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            count += 1
            if count > limit:
                raise YAMLSafetyError(f"YAML document exceeds maximum node count ({limit:,})")
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    # -- public loading API --------------------------------------------------

    def load(self, path: Path) -> tuple[dict[str, Any], SourceMap]:
        """Load a YAML file and return parsed dict + source position map."""
        with path.open("r", encoding="utf-8") as handle:
            content = handle.read()
        self._check_yaml_safety(content)
        data = self._yaml.load(content)
        if data is None:
            return {}, SourceMap()
        self._check_node_count(data)
        source_map = SourceMap()
        self._extract_positions(data, str(path), "", source_map)
        return self._to_plain_dict(data), source_map

    def load_string(
        self, content: str, filename: str = "<string>"
    ) -> tuple[dict[str, Any], SourceMap]:
        """Load YAML from a string."""
        self._check_yaml_safety(content)
        data = self._yaml.load(content)
        if data is None:
            return {}, SourceMap()
        self._check_node_count(data)
        source_map = SourceMap()
        self._extract_positions(data, filename, "", source_map)
        return self._to_plain_dict(data), source_map

    def load_model_directory(self, root: Path) -> tuple[dict[str, Any], SourceMap]:
        """Load a model directory: model.yaml + facts/*.yaml + dimensions/*.yaml + measures/*.yaml.

        Returns a merged dict with all artifacts and a combined source map.
        """
        merged: dict[str, Any] = {}
        combined_map = SourceMap()

        # Load model.yaml (root file)
        model_file = root / "model.yaml"
        if model_file.exists():
            data, smap = self.load(model_file)
            merged.update(data)
            combined_map.merge(smap)

        # Load subdirectory YAML files
        for subdir in ("facts", "dimensions", "measures", "macros", "policies"):
            subdir_path = root / subdir
            if subdir_path.is_dir():
                section: dict[str, Any] = merged.get(subdir, {})
                for yaml_file in sorted(subdir_path.glob("*.yaml")):
                    data, smap = self.load(yaml_file)
                    if isinstance(data, dict):
                        # Use the filename stem as key if the file is a single artifact
                        if "name" in data:
                            section[data["name"]] = data
                        else:
                            section.update(data)
                    combined_map.merge(smap)
                if section:
                    merged[subdir] = section

        return merged, combined_map

    def _extract_positions(
        self,
        data: Any,
        filename: str,
        prefix: str,
        source_map: SourceMap,
    ) -> None:
        """Recursively extract source positions from ruamel.yaml nodes."""
        if isinstance(data, CommentedMap):
            for key in data:
                key_path = f"{prefix}.{key}" if prefix else str(key)
                # Try to get position for this key from ruamel.yaml's lc object
                try:
                    lc = data.lc
                    # lc.key() returns a callable in newer ruamel.yaml
                    key_positions = lc.key(key)
                    if key_positions:
                        line, col = key_positions
                        source_map.add(
                            key_path,
                            SourceSpan(file=filename, line=line + 1, column=col + 1),
                        )
                except (AttributeError, KeyError, TypeError):
                    # Fallback: use the map's own position
                    try:
                        lc = data.lc
                        source_map.add(
                            key_path,
                            SourceSpan(file=filename, line=lc.line + 1, column=lc.col + 1),
                        )
                    except (AttributeError, TypeError):
                        pass
                self._extract_positions(data[key], filename, key_path, source_map)
        elif isinstance(data, CommentedSeq):
            for i, item in enumerate(data):
                item_path = f"{prefix}[{i}]"
                try:
                    lc = data.lc
                    item_pos = lc.item(i)
                    if item_pos:
                        line, col = item_pos
                        source_map.add(
                            item_path,
                            SourceSpan(file=filename, line=line + 1, column=col + 1),
                        )
                except (AttributeError, KeyError, TypeError):
                    pass
                self._extract_positions(item, filename, item_path, source_map)

    def _to_plain_dict(self, data: Any) -> dict[str, Any]:
        """Convert ruamel.yaml CommentedMap/Seq to plain Python dict/list."""
        if isinstance(data, CommentedMap):
            return {str(k): self._to_plain_value(v) for k, v in data.items()}
        if isinstance(data, dict):
            return {str(k): self._to_plain_value(v) for k, v in data.items()}
        return {}

    def _to_plain_value(self, data: Any) -> Any:
        if isinstance(data, CommentedMap):
            return {str(k): self._to_plain_value(v) for k, v in data.items()}
        if isinstance(data, CommentedSeq):
            return [self._to_plain_value(item) for item in data]
        if isinstance(data, dict):
            return {str(k): self._to_plain_value(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._to_plain_value(item) for item in data]
        return data
