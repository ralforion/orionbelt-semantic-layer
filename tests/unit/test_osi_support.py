"""Unit tests for the OSI converter import seam.

``osi-orionbelt`` is an optional dependency, so the import helper must raise a
clear 503 (not a 500) when the converter package is not installed.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException

from orionbelt.api import osi_support


def test_get_converter_module_returns_package() -> None:
    mod = osi_support.get_converter_module()
    for sym in (
        "OSItoOBML",
        "OBMLtoOSI",
        "OBMLtoOSIOntology",
        "validate_obml",
        "validate_osi",
        "validate_osi_ontology",
    ):
        assert hasattr(mod, sym)


def test_get_converter_module_503_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = importlib.import_module

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "osi_orionbelt":
            raise ModuleNotFoundError("No module named 'osi_orionbelt'", name="osi_orionbelt")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib, "import_module", fake_import)

    with pytest.raises(HTTPException) as excinfo:
        osi_support.get_converter_module()
    assert excinfo.value.status_code == 503
    assert "osi-orionbelt" in excinfo.value.detail


def test_get_converter_module_reraises_inner_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # The converter package IS installed but has a broken transitive import:
    # that must propagate as-is, not be masked as a 503 "not installed".
    real_import = importlib.import_module

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "osi_orionbelt":
            raise ModuleNotFoundError("No module named 'some_missing_dep'", name="some_missing_dep")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib, "import_module", fake_import)

    with pytest.raises(ModuleNotFoundError) as excinfo:
        osi_support.get_converter_module()
    assert excinfo.value.name == "some_missing_dep"
