"""``.env`` resolution must agree between the two things that read it.

``Settings`` declares ``env_file=".env"``, which pydantic-settings resolves
from the working directory. ``api.app.main`` separately calls ``load_dotenv``
so that plain ``os.getenv`` callers (driver credentials, POSTGRES_SCHEMA) see
the same values - and a bare ``load_dotenv()`` searches upward from *its own
source file* instead. In a source checkout the two therefore disagreed: the
server, started anywhere, loaded the repo's .env into ``os.environ`` while
``Settings`` read whatever was beside the working directory.

Measured before the fix: started from a directory whose .env said
``API_SERVER_PORT=8020`` with no ``MODEL_FILES``, ``Settings()`` resolved 8020
and the process came up on the repo's 9003, against the repo's database.
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[2] / "src" / "orionbelt" / "api" / "app.py"


def _load_dotenv_call() -> ast.Call:
    tree = ast.parse(APP.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "load_dotenv"
        ):
            return node
    raise AssertionError("app.py no longer calls load_dotenv")


class TestDotenvIsResolvedFromTheWorkingDirectory:
    def test_it_does_not_search_upward_from_the_package(self) -> None:
        """A bare call resolves relative to app.py, not the caller's cwd."""
        call = _load_dotenv_call()
        assert call.args, (
            "load_dotenv() with no path searches upward from app.py, so a source "
            "checkout's .env wins over the working directory's"
        )

    def test_the_path_comes_from_find_dotenv_with_usecwd(self) -> None:
        call = _load_dotenv_call()
        path_arg = call.args[0]
        assert isinstance(path_arg, ast.Call)
        assert isinstance(path_arg.func, ast.Name)
        assert path_arg.func.id == "find_dotenv"
        assert any(
            kw.arg == "usecwd" and getattr(kw.value, "value", None) is True
            for kw in path_arg.keywords
        ), "find_dotenv must be told to start from the working directory"

    def test_real_environment_variables_still_win(self) -> None:
        """``override=False`` is what lets a container pass config without a file."""
        call = _load_dotenv_call()
        assert any(
            kw.arg == "override" and getattr(kw.value, "value", None) is False
            for kw in call.keywords
        )

    def test_a_missing_file_is_a_no_op(self) -> None:
        """``find_dotenv`` answers "" when there is none, and load_dotenv("")
        must not raise - a deployment configured purely by environment has no
        file on disk."""
        import tempfile

        from dotenv import find_dotenv, load_dotenv

        cwd = pathlib.Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as empty:
                import os

                os.chdir(empty)
                assert find_dotenv(usecwd=True) == ""
                assert load_dotenv("") is False
        finally:
            import os

            os.chdir(cwd)
