"""Extends & inherits merger — merges analytical fragments and parent models."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

from orionbelt.models.semantic import OntologyConfig

logger = logging.getLogger("orionbelt.parser.merger")

MAX_EXTENDS_DEPTH = 5


# Keys a fragment's ``ontology`` block may carry, from the model it becomes.
_ONTOLOGY_KEYS = frozenset(OntologyConfig.model_fields)
# Origin label for the child side of an ``inherits`` merge in error messages.
_INHERITING_MODEL = "inheriting model"

# Name-keyed sections: the fragment's or child's definition replaces one of the
# same name, with a warning.
_NAMED_SECTIONS = ("dimensions", "measures", "metrics", "rules")
# Scalars whose value changes how every query is compiled or counted: the
# fragment's or child's value wins, with a warning when it changes one.
_BEHAVIOUR_SCALARS = ("exposeCounts", "countLabelPattern")
# Every top-level key a fragment or inheriting child may carry and the merge
# handles. Anything else is refused: a key the merge does not read would
# otherwise vanish, and a security filter or a setting vanished without a word.
_MERGED_KEYS = frozenset(
    {
        *_NAMED_SECTIONS,
        *_BEHAVIOUR_SCALARS,
        "examples",
        "filters",
        "settings",
        "customExtensions",
        "externalConceptMappings",
        "ontology",
        "description",
        "owner",
        "version",
    }
)


def _example_name(example: object) -> str | None:
    """An example's ``name`` when it is a string, the only form that identifies it."""
    name = example.get("name") if isinstance(example, dict) else None
    return name if isinstance(name, str) else None


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
        if "name" in ext:
            raise MergeError(
                "EXTENDS_CONTAINS_MODEL_NAME",
                f"Extend fragment '{origin}' must not contain 'name': a fragment adds to "
                "the model it extends and cannot rename it",
            )
        if ext.get("inherits"):
            raise MergeError(
                "EXTENDS_CONTAINS_INHERITS",
                f"Extend fragment '{origin}' must not use 'inherits'",
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
                ext_dict.pop("_extends_sources", None)

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
            if ext_dict.get("extends"):
                raise MergeError(
                    "EXTENDS_NESTED_INLINE",
                    f"Inline fragment '{origin}' must not use 'extends': an inline fragment "
                    "has no location to resolve file paths against",
                )

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
        merged = copy.deepcopy(parent)
        warnings = self._merge_model_keys(
            merged,
            {k: v for k, v in child.items() if k != "name"},
            _INHERITING_MODEL,
            override_label=f"from child overrides parent '{origin}'",
            whole_model=True,
        )
        # The child is a model of its own, so its name wins.
        if child.get("name"):
            merged["name"] = child["name"]
        merged["_inherits_source"] = origin
        return merged, warnings

    # -- analytical merge ----------------------------------------------------

    @staticmethod
    def _deep_merge_analytical(
        target: dict[str, Any],
        source: dict[str, Any],
        origin: str,
    ) -> list[str]:
        """Merge an extend fragment into *target*. Source wins on conflict."""
        return ExtendsMerger._merge_model_keys(
            target, source, origin, override_label=f"overridden by '{origin}'"
        )

    @staticmethod
    def _merge_model_keys(
        target: dict[str, Any],
        source: dict[str, Any],
        origin: str,
        *,
        override_label: str,
        whole_model: bool = False,
    ) -> list[str]:
        """Merge every top-level key of *source* into *target*; source wins.

        Shared by ``extends`` (source = fragment) and ``inherits`` (source =
        child, target = a copy of the parent). Each key is merged or refused;
        none is skipped. ``dataObjects``, ``name``, ``extends`` and ``inherits``
        are handled by the callers before this runs.
        """
        warnings: list[str] = []
        unknown = sorted(
            key
            for key in source
            if key not in _MERGED_KEYS and key not in {"dataObjects", "extends", "inherits"}
        )
        if unknown:
            raise MergeError(
                "UNKNOWN_PROPERTY",
                f"Unknown or unmergeable top-level propert{'y' if len(unknown) == 1 else 'ies'} "
                f"{', '.join(repr(k) for k in unknown)} in '{origin}'",
            )

        for key in _NAMED_SECTIONS:
            src_section = source.get(key)
            if not src_section:
                continue
            if not isinstance(src_section, dict):
                raise MergeError(
                    "INVALID_FRAGMENT_SECTION", f"'{key}' in '{origin}' must be a mapping"
                )
            tgt_section = target.get(key)
            if not isinstance(tgt_section, dict):
                tgt_section = target[key] = {}
            for name, defn in src_section.items():
                if name in tgt_section:
                    warnings.append(f"{key[:-1].title()} '{name}' {override_label}")
                tgt_section[name] = defn

        warnings.extend(ExtendsMerger._merge_examples(target, source, origin, override_label))

        # Static filters accumulate (AND): a fragment or child can narrow the
        # model, never widen it.
        src_filters = source.get("filters")
        if src_filters:
            if not isinstance(src_filters, list):
                raise MergeError(
                    "INVALID_FRAGMENT_SECTION", f"'filters' in '{origin}' must be a list"
                )
            target["filters"] = list(target.get("filters") or []) + src_filters

        warnings.extend(ExtendsMerger._merge_settings(target, source, origin, override_label))

        for key in _BEHAVIOUR_SCALARS:
            if key not in source:
                continue
            if key in target and target[key] != source[key]:
                warnings.append(f"Model property '{key}' {override_label}")
            target[key] = source[key]

        # customExtensions: concatenate lists
        src_exts = source.get("customExtensions")
        if src_exts:
            target["customExtensions"] = list(target.get("customExtensions") or []) + src_exts

        # externalConceptMappings: concatenate lists (type-checked first)
        ExtendsMerger._append_mappings(target, ExtendsMerger._fragment_mappings(source, origin))

        ExtendsMerger._merge_prefixes(target, source, origin)

        # description and owner: last non-empty wins
        for key in ("description", "owner"):
            if source.get(key):
                target[key] = source[key]

        # version: the source's only where it is a whole model (inherits); a
        # fragment's records the OBML version it was written for.
        if "version" in source and whole_model:
            target["version"] = source["version"]

        return warnings

    @staticmethod
    def _merge_examples(
        target: dict[str, Any], source: dict[str, Any], origin: str, override_label: str
    ) -> list[str]:
        """Append the source's examples; one with an existing name replaces it.

        Only a string ``name`` identifies an example. An entry without one, or
        with a name of another type, is appended as authored for the resolver
        to report with a source span; indexing it here crashed the load on an
        unhashable name. A malformed target section (not a list) is left
        exactly as authored for the same reason, as ``_append_mappings`` does.
        """
        src_examples = source.get("examples")
        if not src_examples:
            return []
        if not isinstance(src_examples, list):
            raise MergeError("INVALID_FRAGMENT_SECTION", f"'examples' in '{origin}' must be a list")
        existing = target.get("examples")
        if existing is None:
            existing = target["examples"] = []
        if not isinstance(existing, list):
            return []
        warnings: list[str] = []
        position = {
            name: i for i, ex in enumerate(existing) if (name := _example_name(ex)) is not None
        }
        for example in src_examples:
            name = _example_name(example)
            if name is not None and name in position:
                warnings.append(f"Example '{name}' {override_label}")
                existing[position[name]] = example
                continue
            if name is not None:
                position[name] = len(existing)
            existing.append(example)
        return warnings

    @staticmethod
    def _merge_settings(
        target: dict[str, Any], source: dict[str, Any], origin: str, override_label: str
    ) -> list[str]:
        """Merge the source's settings key by key; a changed value warns."""
        src_settings = source.get("settings")
        if not src_settings:
            return []
        if not isinstance(src_settings, dict):
            raise MergeError(
                "INVALID_FRAGMENT_SECTION", f"'settings' in '{origin}' must be a mapping"
            )
        tgt_settings = target.get("settings")
        if not isinstance(tgt_settings, dict):
            tgt_settings = target["settings"] = {}
        warnings: list[str] = []
        for key, value in src_settings.items():
            if key in tgt_settings and tgt_settings[key] != value:
                warnings.append(f"Setting '{key}' {override_label}")
            tgt_settings[key] = value
        return warnings

    @staticmethod
    def _fragment_mappings(source: dict[str, Any], origin: str) -> list[Any]:
        """A fragment's model-level ``externalConceptMappings``, or a MergeError.

        Only a list can be concatenated into the target; anything else
        (``externalConceptMappings: {}``) would otherwise be dropped here
        without the resolver ever seeing it.
        """
        items = source.get("externalConceptMappings")
        if items is None:
            return []
        if not isinstance(items, list):
            raise MergeError(
                "INVALID_CONCEPT_MAPPING",
                f"'externalConceptMappings' in '{origin}' must be a list of mapping entries",
            )
        return items

    @staticmethod
    def _append_mappings(target: dict[str, Any], items: list[Any]) -> None:
        """Append fragment mappings to the target's model-level list.

        A malformed target value (``externalConceptMappings: {}``) is left
        exactly as authored and nothing is appended, so the resolver reports
        it as INVALID_CONCEPT_MAPPING with a source span instead of the merge
        crashing on it or overwriting it.
        """
        if not items:
            return
        existing = target.get("externalConceptMappings")
        if existing is None:
            target["externalConceptMappings"] = list(items)
        elif isinstance(existing, list):
            existing.extend(items)

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
        if src_ontology is None:
            return
        src_prefixes = src_ontology.get("prefixes") if isinstance(src_ontology, dict) else None
        if not isinstance(src_ontology, dict) or (
            src_prefixes is not None and not isinstance(src_prefixes, dict)
        ):
            # The fragment's block is not copied into the merged document (only
            # its prefixes are), so a malformed one would vanish without a
            # trace if it were skipped here. Refuse the merge instead; the
            # resolver never sees fragment sources, so this is the only place
            # that can name the file.
            raise MergeError(
                "ONTOLOGY_PARSE_ERROR",
                f"'ontology' in '{origin}' must be a mapping with a 'prefixes' mapping",
            )
        # Same strictness as the resolver's UNKNOWN_PROPERTY check on a
        # top-level block: a typo in a fragment must not pass because only
        # ``prefixes`` is ever read from it.
        for key in src_ontology:
            if key not in _ONTOLOGY_KEYS:
                raise MergeError(
                    "UNKNOWN_PROPERTY", f"Unknown property '{key}' at ontology in '{origin}'"
                )
        if not src_prefixes:
            return
        # A malformed block on the target side (``ontology: []``,
        # ``prefixes: [..]``) is left exactly as authored: the resolver reports
        # it as ONTOLOGY_PARSE_ERROR with a source span, which is better than
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
