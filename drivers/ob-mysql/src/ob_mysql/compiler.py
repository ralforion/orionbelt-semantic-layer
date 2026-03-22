"""OrionBelt compilation bridge — delegates to ob-driver-core."""

from __future__ import annotations

from ob_driver_core.compiler import compile_obml
from ob_driver_core.detection import is_obml, parse_obml

__all__ = ["compile_obml", "is_obml", "parse_obml"]
