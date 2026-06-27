"""``obsl`` — the OrionBelt Semantic Layer command-line interface.

A local-first CLI: ``validate``, ``compile``, ``execute``, ``describe``,
``convert``, ``diagram``, ``graph`` and ``dialects`` all run in-process by
calling the same compiler / parser / converter internals the REST API uses,
so model authors can lint and preview SQL with zero infrastructure.

Pass ``--server URL`` (with optional ``--api-key``) to ``validate``,
``compile``, ``execute`` or ``convert`` to run against a deployed OrionBelt
REST API instead — handy for executing queries against a warehouse the local
machine can't reach. The remaining commands are pure functions of the model
file and always run locally.
"""

from __future__ import annotations

__all__ = ["app"]


def __getattr__(name: str) -> object:
    # Lazy re-export so ``from orionbelt.cli import app`` works without paying
    # the Typer import cost for callers that only want the subpackage.
    if name == "app":
        from orionbelt.cli.main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
