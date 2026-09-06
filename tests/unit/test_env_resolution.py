"""``.env`` resolution must agree between the two things that read it.

``Settings`` declares ``env_file=".env"``, which pydantic-settings reads as
exactly ``./.env`` - no search. ``api.app.main`` separately calls
``load_dotenv`` so that plain ``os.getenv`` callers (driver credentials,
POSTGRES_SCHEMA) see the same values, and every searching form of that call
disagrees with it:

* bare ``load_dotenv()`` walks up from *its own source file*, so a source
  checkout's .env was loaded whatever directory the server started in.
  Measured: started from a directory whose .env said ``API_SERVER_PORT=8020``
  with no ``MODEL_FILES``, ``Settings()`` resolved 8020 while the process came
  up on the repo's 9003, against the repo's database.
* ``find_dotenv(usecwd=True)`` walks up from the working directory, so a
  subdirectory with no .env of its own inherits a parent's while ``Settings``
  inherits nothing. Measured: parent .env ``API_SERVER_PORT=8123``, child with
  none - ``Settings()`` alone said 8000, and 8123 after the load.

Either way the disagreement injects a different deployment's credentials and
``MODEL_FILES`` into ``os.environ`` while ``Settings`` reads the intended ones.
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
    def test_it_does_not_search(self) -> None:
        """Neither upward from this file nor upward from the cwd."""
        call = _load_dotenv_call()
        assert call.args, "load_dotenv() with no path walks up from app.py"
        source = ast.unparse(call.args[0])
        assert "find_dotenv" not in source, (
            "find_dotenv searches upward; pydantic-settings reads only ./.env, "
            "so a subdirectory would inherit a parent's file while Settings does not"
        )

    def test_the_path_is_exactly_the_working_directory_s_env(self) -> None:
        call = _load_dotenv_call()
        source = ast.unparse(call.args[0])
        assert "Path.cwd()" in source and "'.env'" in source, source

    def test_real_environment_variables_still_win(self) -> None:
        """``override=False`` is what lets a container pass config without a file."""
        call = _load_dotenv_call()
        assert any(
            kw.arg == "override" and getattr(kw.value, "value", None) is False
            for kw in call.keywords
        )

    def test_a_parent_env_is_not_inherited(self) -> None:
        """The behavioural half: a child with no .env must see nothing."""
        import os
        import tempfile

        from dotenv import load_dotenv

        from orionbelt.settings import Settings

        cwd = pathlib.Path.cwd()
        with tempfile.TemporaryDirectory() as parent:
            (pathlib.Path(parent) / ".env").write_text("API_SERVER_PORT=8123\n")
            child = pathlib.Path(parent) / "child"
            child.mkdir()
            try:
                os.chdir(child)
                load_dotenv(pathlib.Path.cwd() / ".env", override=False)
                assert Settings().api_server_port != 8123
            finally:
                os.chdir(cwd)
                os.environ.pop("API_SERVER_PORT", None)

    def test_the_working_directory_s_own_env_is_read(self) -> None:
        import os
        import tempfile

        from dotenv import load_dotenv

        from orionbelt.settings import Settings

        cwd = pathlib.Path.cwd()
        with tempfile.TemporaryDirectory() as here:
            (pathlib.Path(here) / ".env").write_text("API_SERVER_PORT=8321\n")
            try:
                os.chdir(here)
                load_dotenv(pathlib.Path.cwd() / ".env", override=False)
                assert Settings().api_server_port == 8321
            finally:
                os.chdir(cwd)
                os.environ.pop("API_SERVER_PORT", None)

    def test_a_missing_file_is_a_no_op(self) -> None:
        """A deployment configured purely by environment has no file on disk."""
        import os
        import tempfile

        from dotenv import load_dotenv

        cwd = pathlib.Path.cwd()
        with tempfile.TemporaryDirectory() as empty:
            try:
                os.chdir(empty)
                assert load_dotenv(pathlib.Path.cwd() / ".env") is False
            finally:
                os.chdir(cwd)
