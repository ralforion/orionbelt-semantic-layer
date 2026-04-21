"""Extends & inherits merger — merges analytical fragments and parent models."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("orionbelt.parser.merger")

MAX_EXTENDS_DEPTH = 5

ANALYTICAL_KEYS = {"dimensions", "measures", "metrics", "customExtensions"}


class MergeError(Exception):
    """Raised when merging extends or inherits fails."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class ExtendsMerger:
    """Merges analytical fragments (extends) and parent models (inherits) into a raw YAML dict.

    Operates on raw dicts (pre-resolution). Returns the merged dict ready for
    ``ReferenceResolver.resolve()``.
    """

    def merge_from_files(
        self,
        raw: dict[str, Any],
        base_path: Path,
        *,
        _depth: int = 0,
        _seen: set[str] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Merge extends/inherits from file paths relative to *base_path*.

        Returns ``(merged_dict, warnings)``.
        """
        warnings: list[str] = []
        seen = _seen if _seen is not None else set()

        self._validate_combination(raw)

        if "extends" in raw and raw["extends"]:
            raw, ext_warnings = self._merge_extends_files(raw, base_path, depth=_depth, seen=seen)
            warnings.extend(ext_warnings)

        if "inherits" in raw and raw["inherits"]:
            raw, inh_warnings = self._merge_inherits_file(raw, base_path)
            warnings.extend(inh_warnings)

        return raw, warnings

    def merge_from_strings(
        self,
        raw: dict[str, Any],
        extend_yamls: list[str] | None = None,
        inherits_raw: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Merge extends from inline YAML strings and/or inherits from parent raw dict.

        Returns ``(merged_dict, warnings)``.
        """
        warnings: list[str] = []

        self._validate_combination(raw)

        if extend_yamls:
            raw, ext_warnings = self._merge_extends_strings(raw, extend_yamls)
            warnings.extend(ext_warnings)

        if inherits_raw is not None:
            raw, inh_warnings = self._merge_inherits_raw(raw, inherits_raw)
            warnings.extend(inh_warnings)

        return raw, warnings

    # -- validation ----------------------------------------------------------

    @staticmethod
    def _validate_combination(raw: dict[str, Any]) -> None:
        has_extends = bool(raw.get("extends"))
        has_inherits = bool(raw.get("inherits"))
        if has_extends and has_inherits:
            raise MergeError(
                "INVALID_EXTENDS_INHERITS_COMBINATION",
                "A model cannot have both 'extends' and 'inherits'",
            )

    @staticmethod
    def _validate_extend_dict(ext: dict[str, Any], origin: str) -> None:
        if ext.get("dataObjects"):
            raise MergeError(
                "EXTENDS_CONTAINS_DATA_OBJECTS",
                f"Extend fragment '{origin}' must not contain 'dataObjects'",
            )

    @staticmethod
    def _validate_parent(parent: dict[str, Any], origin: str) -> None:
        if parent.get("extends"):
            raise MergeError(
                "PARENT_HAS_EXTENDS",
                f"Parent model '{origin}' must not use 'extends'",
            )
        if parent.get("inherits"):
            raise MergeError(
                "PARENT_HAS_INHERITS",
                f"Parent model '{origin}' must not use 'inherits'",
            )

    @staticmethod
    def _validate_inherits_child(raw: dict[str, Any]) -> None:
        if raw.get("dataObjects"):
            raise MergeError(
                "INHERITS_CONTAINS_DATA_OBJECTS",
                "An inheriting model must not define 'dataObjects'",
            )

    # -- extends (files) -----------------------------------------------------

    def _merge_extends_files(
        self,
        raw: dict[str, Any],
        base_path: Path,
        *,
        depth: int,
        seen: set[str],
    ) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        extend_paths: list[str] = raw.pop("extends", [])
        extend_sources: list[str] = list(extend_paths)

        if depth >= MAX_EXTENDS_DEPTH:
            raise MergeError(
                "EXTENDS_MAX_DEPTH_EXCEEDED",
                f"Extends nesting exceeds maximum depth of {MAX_EXTENDS_DEPTH}",
            )

        merged = copy.deepcopy(raw)

        for ext_rel in extend_paths:
            ext_file = (base_path / ext_rel).resolve()
            ext_key = str(ext_file)

            if ext_key in seen:
                raise MergeError(
                    "CIRCULAR_EXTENDS",
                    f"Circular reference detected: '{ext_rel}' was already loaded",
                )
            seen.add(ext_key)

            if not ext_file.is_file():
                raise MergeError(
                    "EXTENDS_FILE_NOT_FOUND",
                    f"Extend file not found: {ext_rel} (resolved to {ext_file})",
                )

            ext_yaml = ext_file.read_text(encoding="utf-8")
            ext_dict = yaml.safe_load(ext_yaml) or {}

            self._validate_extend_dict(ext_dict, ext_rel)

            if ext_dict.get("extends"):
                ext_dict, nested_warnings = self._merge_extends_files(
                    ext_dict,
                    ext_file.parent,
                    depth=depth + 1,
                    seen=seen,
                )
                warnings.extend(nested_warnings)

            merge_warnings = self._deep_merge_analytical(merged, ext_dict, ext_rel)
            warnings.extend(merge_warnings)

        merged["_extends_sources"] = extend_sources
        return merged, warnings

    # -- extends (strings) ---------------------------------------------------

    def _merge_extends_strings(
        self,
        raw: dict[str, Any],
        extend_yamls: list[str],
    ) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        raw.pop("extends", None)
        merged = copy.deepcopy(raw)

        for i, ext_yaml in enumerate(extend_yamls):
            ext_dict = yaml.safe_load(ext_yaml) or {}
            origin = f"extends[{i}]"

            self._validate_extend_dict(ext_dict, origin)

            merge_warnings = self._deep_merge_analytical(merged, ext_dict, origin)
            warnings.extend(merge_warnings)

        merged["_extends_sources"] = [f"inline:{i}" for i in range(len(extend_yamls))]
        return merged, warnings

    # -- inherits (file) -----------------------------------------------------

    def _merge_inherits_file(
        self,
        raw: dict[str, Any],
        base_path: Path,
    ) -> tuple[dict[str, Any], list[str]]:
        inherits_rel: str = raw.pop("inherits", "")

        self._validate_inherits_child(raw)

        parent_file = (base_path / inherits_rel).resolve()
        if not parent_file.is_file():
            raise MergeError(
                "PARENT_MODEL_NOT_FOUND",
                f"Parent model file not found: {inherits_rel} (resolved to {parent_file})",
            )

        parent_yaml = parent_file.read_text(encoding="utf-8")
        parent_dict = yaml.safe_load(parent_yaml) or {}

        self._validate_parent(parent_dict, inherits_rel)

        return self._do_inherits_merge(raw, parent_dict, inherits_rel)

    # -- inherits (raw dict from session) ------------------------------------

    def _merge_inherits_raw(
        self,
        raw: dict[str, Any],
        parent_raw: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        self._validate_inherits_child(raw)
        self._validate_parent(parent_raw, "parent")

        return self._do_inherits_merge(raw, parent_raw, "parent")

    # -- inherits merge logic ------------------------------------------------

    def _do_inherits_merge(
        self,
        child: dict[str, Any],
        parent: dict[str, Any],
        origin: str,
    ) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        merged = copy.deepcopy(parent)

        # Child version wins
        if "version" in child:
            merged["version"] = child["version"]

        # Child description wins if present
        if child.get("description"):
            merged["description"] = child["description"]

        # Override/add analytical definitions — child wins
        for key in ("dimensions", "measures", "metrics"):
            parent_section = merged.get(key) or {}
            child_section = child.get(key) or {}
            for name, defn in child_section.items():
                if name in parent_section:
                    warnings.append(
                        f"{key[:-1].title()} '{name}' from child overrides parent '{origin}'"
                    )
                parent_section[name] = defn
            if parent_section:
                merged[key] = parent_section

        # Filters accumulate (AND logic)
        parent_filters = list(merged.get("filters") or [])
        child_filters = list(child.get("filters") or [])
        if parent_filters or child_filters:
            merged["filters"] = parent_filters + child_filters

        # customExtensions: concatenate
        parent_exts = list(merged.get("customExtensions") or [])
        child_exts = list(child.get("customExtensions") or [])
        if parent_exts or child_exts:
            merged["customExtensions"] = parent_exts + child_exts

        # Owner: child wins if present
        if child.get("owner"):
            merged["owner"] = child["owner"]

        merged["_inherits_source"] = origin
        return merged, warnings

    # -- analytical merge ----------------------------------------------------

    @staticmethod
    def _deep_merge_analytical(
        target: dict[str, Any],
        source: dict[str, Any],
        origin: str,
    ) -> list[str]:
        """Merge analytical keys from *source* into *target*. Source wins on conflict."""
        warnings: list[str] = []

        for key in ("dimensions", "measures", "metrics"):
            src_section = source.get(key)
            if not src_section:
                continue
            tgt_section = target.setdefault(key, {})
            for name, defn in src_section.items():
                if name in tgt_section:
                    warnings.append(f"{key[:-1].title()} '{name}' overridden by '{origin}'")
                tgt_section[name] = defn

        # customExtensions: concatenate lists
        src_exts = source.get("customExtensions")
        if src_exts:
            target.setdefault("customExtensions", []).extend(src_exts)

        # description: last non-None wins
        if source.get("description"):
            target["description"] = source["description"]

        return warnings
