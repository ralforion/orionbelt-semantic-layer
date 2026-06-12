"""Command-line entry points for the OBML <-> OSI converter.

Two format-named commands, mirroring the OSI converter convention
(``<vendor>-to-osi`` / ``osi-to-<vendor>``):

    obml-to-osi  [--ontology]  IN  [-o OUT]    OBML  -> OSI core-spec (or ontology)
    osi-to-obml                IN  [-o OUT]    OSI core-spec -> OBML

Both print conversion warnings and a validation summary to stderr, and exit
non-zero when the produced document fails schema validation (unless
``--no-validate`` is given).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from osi_orionbelt.converter import (
    OBMLtoOSI,
    OBMLtoOSIOntology,
    OSItoOBML,
    validate_obml,
    validate_osi,
    validate_osi_ontology,
)


def _load(input_path: str | None, parser: argparse.ArgumentParser) -> dict[str, Any]:
    if not input_path:
        parser.error("Input file is required")
    with open(input_path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        parser.error("Input YAML must be a mapping (dict)")
    return data


def _emit(result: dict[str, Any], output: str | None) -> None:
    output_yaml = yaml.dump(
        result, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
    )
    if output:
        Path(output).write_text(output_yaml)
        print(f"Converted to {output}", file=sys.stderr)
    else:
        sys.stdout.write(output_yaml)


def _print_warnings(warnings: list[str]) -> None:
    if warnings:
        print("\nConversion warnings:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)


def _report_validation(label: str, result: dict[str, Any], validate_fn: Any) -> bool:
    """Validate ``result`` and print a summary. Return True if there are errors."""
    print(f"\nValidating {label}...", file=sys.stderr)
    vr = validate_fn(result)
    for line in vr.summary_lines():
        print(line, file=sys.stderr)
    if vr.valid:
        print(f"{label} is valid", file=sys.stderr)
        return False
    print(f"{label} has validation errors", file=sys.stderr)
    return True


def obml_to_osi(argv: list[str] | None = None) -> int:
    """OBML -> OSI core-spec (or, with --ontology, OSI ontology)."""
    parser = argparse.ArgumentParser(
        prog="obml-to-osi", description="Convert an OBML model to OSI."
    )
    parser.add_argument("input", nargs="?", help="Input OBML YAML file")
    parser.add_argument("-o", "--output", help="Output YAML file (default: stdout)")
    parser.add_argument(
        "--ontology", action="store_true", help="Emit an OSI ontology document instead of core-spec"
    )
    parser.add_argument("--name", default="semantic_model", help="OSI model name")
    parser.add_argument("--description", default="", help="OSI model description")
    parser.add_argument("--ai-instructions", default="", help="OSI ai_context instructions")
    parser.add_argument("--no-validate", action="store_true", help="Skip output validation")
    args = parser.parse_args(argv)

    data = _load(args.input, parser)

    validate_fn: Any
    if args.ontology:
        converter: Any = OBMLtoOSIOntology(data, args.name, args.description, args.ai_instructions)
        result = converter.convert()
        validate_fn, label = validate_osi_ontology, "OSI ontology output"
    else:
        converter = OBMLtoOSI(data, args.name, args.description, args.ai_instructions)
        result = converter.convert()
        validate_fn, label = validate_osi, "OSI output"

    _emit(result, args.output)
    _print_warnings(converter.warnings)

    if args.no_validate:
        return 0
    return 1 if _report_validation(label, result, validate_fn) else 0


def osi_to_obml(argv: list[str] | None = None) -> int:
    """OSI core-spec -> OBML."""
    parser = argparse.ArgumentParser(
        prog="osi-to-obml", description="Convert an OSI model to OBML."
    )
    parser.add_argument("input", nargs="?", help="Input OSI YAML file")
    parser.add_argument("-o", "--output", help="Output YAML file (default: stdout)")
    parser.add_argument("--database", default="ANALYTICS", help="Default database for OBML output")
    parser.add_argument("--schema", default="PUBLIC", help="Default schema for OBML output")
    parser.add_argument("--no-validate", action="store_true", help="Skip output validation")
    args = parser.parse_args(argv)

    data = _load(args.input, parser)

    converter = OSItoOBML(data, args.database, args.schema)
    result = converter.convert()

    _emit(result, args.output)
    _print_warnings(converter.warnings)

    if args.no_validate:
        return 0
    return 1 if _report_validation("OBML output", result, validate_obml) else 0


if __name__ == "__main__":
    raise SystemExit(obml_to_osi())
