"""osi-orionbelt: bidirectional OBML <-> OSI converter.

Converts between OrionBelt Markup Language (OBML) semantic models and Open
Semantic Interchange (OSI) models, in both directions. Validation helpers check
OBML and OSI documents against their JSON schemas.

Public API:
    OSItoOBML            - convert an OSI model dict to OBML
    OBMLtoOSI            - convert an OBML model dict to OSI core-spec
    validate_obml        - validate an OBML model dict
    validate_osi         - validate an OSI model dict
    ValidationResult     - structured validation result
"""

from __future__ import annotations

from osi_orionbelt.converter import (
    OBMLtoOSI,
    OSItoOBML,
    ValidationResult,
    validate_obml,
    validate_osi,
)

__version__ = "0.2.1"

__all__ = [
    "OBMLtoOSI",
    "OSItoOBML",
    "ValidationResult",
    "validate_obml",
    "validate_osi",
    "__version__",
]
