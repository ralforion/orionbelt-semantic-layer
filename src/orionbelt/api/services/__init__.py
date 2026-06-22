"""Service layer for the session router.

Pure helper and core-logic functions extracted from the (formerly fat)
``orionbelt.api.routers.sessions`` module. These contain no FastAPI route
decorators; the thin handlers in ``sessions.py`` resolve dependencies, call
these services, and translate domain exceptions to ``HTTPException``.

Services may depend on the cache, model store, compiler, dialect, and model
layers, but must NOT import from ``orionbelt.api.routers.sessions`` (that
would create an import cycle).
"""

from __future__ import annotations
