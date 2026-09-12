"""Extends & inherits merger — merges analytical fragments and parent models."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("orionbelt.parser.merger")

MAX_EXTENDS_DEPTH = 5


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

        # customExtensions / externalConceptMappings: concatenate
        for list_key in ("customExtensions", "externalConceptMappings"):
            parent_items = list(merged.get(list_key) or [])
            child_items = list(child.get(list_key) or [])
            if parent_items or child_items:
                merged[list_key] = parent_items + child_items

        # ontology.prefixes: union; a rebinding is an error, see _merge_prefixes
        self._merge_prefixes(merged, child, origin)

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

        # customExtensions / externalConceptMappings: concatenate lists
        for list_key in ("customExtensions", "externalConceptMappings"):
            src_items = source.get(list_key)
            if src_items:
                target.setdefault(list_key, []).extend(src_items)

        ExtendsMerger._merge_prefixes(target, source, origin)

        # description: last non-None wins
        if source.get("description"):
            target["description"] = source["description"]

        return warnings

    @staticmethod
    def _merge_prefixes(target: dict[str, Any], source: dict[str, Any], origin: str) -> None:
        """Union ``source``'s ``ontology.prefixes`` into ``target``.

        A fragment's compact concept IRIs expand with the prefixes the
        fragment declares, so they have to survive the merge. But every
        mapping in the merged document expands against one prefix map, so
        rebinding a name that is already bound would silently rewrite the
        other fragment's mappings to a different namespace. That is an
        error, not a warning: ``corp:Base`` must not become
        ``https://child.example/Base`` because a later file reused ``corp``.
        """
        src_ontology = source.get("ontology")
        src_prefixes = src_ontology.get("prefixes") if isinstance(src_ontology, dict) else None
        if not isinstance(src_prefixes, dict) or not src_prefixes:
            return
        # A malformed block on either side (``ontology: []``, ``prefixes: [..]``)
        # is left exactly as authored: the resolver reports it as
        # ONTOLOGY_PARSE_ERROR with a source span, which is better than
        # anything the merger could say about it. Nothing is merged into it.
        tgt_ontology = target.get("ontology")
        if tgt_ontology is None:
            tgt_ontology = target["ontology"] = {}
        if not isinstance(tgt_ontology, dict):
            return
        tgt_prefixes = tgt_ontology.get("prefixes")
        if tgt_prefixes is None:
            tgt_prefixes = tgt_ontology["prefixes"] = {}
        if not isinstance(tgt_prefixes, dict):
            return
        for name, namespace in src_prefixes.items():
            bound = tgt_prefixes.get(name)
            if bound is not None and bound != namespace:
                raise MergeError(
                    "ONTOLOGY_PREFIX_CONFLICT",
                    f"Ontology prefix '{name}' is bound to <{bound}> and '{origin}' binds it "
                    f"to <{namespace}>; a prefix must expand to one namespace across all "
                    "merged fragments",
                )
            tgt_prefixes[name] = namespace
