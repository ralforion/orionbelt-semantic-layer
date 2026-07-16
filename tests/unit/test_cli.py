"""Tests for the ``obsl`` command-line interface.

These exercise the local (in-process) command paths via Typer's ``CliRunner``.
The remote (``--server``) paths are covered by monkeypatching ``RemoteClient``
so no live server is required.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from orionbelt.cli.main import app
from tests.conftest import SAMPLE_MODEL_YAML

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Keep the CLI tests hermetic.

    ``--server`` / ``--api-key`` are env-backed (OBSL_SERVER / OBSL_API_KEY).
    A developer with either exported would otherwise flip the local command
    paths into remote mode. Clear them for every test.
    """
    monkeypatch.delenv("OBSL_SERVER", raising=False)
    monkeypatch.delenv("OBSL_API_KEY", raising=False)


@pytest.fixture
def model_file(tmp_path):
    """Write the shared sample model to a temp file and return its path."""
    p = tmp_path / "model.yaml"
    p.write_text(SAMPLE_MODEL_YAML, encoding="utf-8")
    return str(p)


@pytest.fixture
def query_file(tmp_path):
    """A simple, valid query against the sample model."""
    p = tmp_path / "query.json"
    p.write_text(
        json.dumps(
            {
                "select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]},
                "limit": 10,
            }
        ),
        encoding="utf-8",
    )
    return str(p)


# -- validate ---------------------------------------------------------------


def test_validate_valid_model(model_file):
    result = runner.invoke(app, ["validate", model_file])
    assert result.exit_code == 0


def test_validate_json_output(model_file):
    result = runner.invoke(app, ["validate", model_file, "-f", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["errors"] == []


def test_validate_invalid_model_exits_nonzero(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1.0\ndimensions:\n  X:\n    column: Nope\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(bad), "-f", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert payload["errors"]


def test_validate_missing_file():
    result = runner.invoke(app, ["validate", "/no/such/file.yaml"])
    assert result.exit_code != 0


# -- compile ----------------------------------------------------------------


def test_compile_emits_sql(model_file, query_file):
    result = runner.invoke(app, ["compile", model_file, "-q", query_file, "-d", "snowflake"])
    assert result.exit_code == 0
    assert "SELECT" in result.stdout
    assert "Customer Country" in result.stdout


def test_compile_json_output(model_file, query_file):
    result = runner.invoke(
        app, ["compile", model_file, "-q", query_file, "-d", "duckdb", "-f", "json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dialect"] == "duckdb"
    assert "SELECT" in payload["sql"]


def test_compile_unknown_measure_clean_error(model_file, tmp_path):
    q = tmp_path / "q.json"
    q.write_text(json.dumps({"select": {"dimensions": [], "measures": ["Nope"]}}), encoding="utf-8")
    result = runner.invoke(app, ["compile", model_file, "-q", str(q)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_compile_rejects_authored_label(tmp_path, query_file):
    """The CLI is an external boundary: an authored ``label:`` on a dimension
    fails schema validation with a clean error (no traceback), matching the
    REST API's 422 guard rather than being silently coerced. See #221.
    """
    import yaml

    raw = yaml.safe_load(SAMPLE_MODEL_YAML)
    dim_key = next(iter(raw["dimensions"]))
    raw["dimensions"][dim_key]["label"] = "Authored"
    bad = tmp_path / "model.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    result = runner.invoke(app, ["compile", str(bad), "-q", query_file])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "schema validation" in result.output.lower()


def test_compile_explain(model_file, query_file):
    result = runner.invoke(
        app, ["compile", model_file, "-q", query_file, "-d", "postgres", "--explain"]
    )
    assert result.exit_code == 0
    assert "planner" in result.output


# -- OBSQL (--sql) ----------------------------------------------------------


def test_compile_obsql_local(model_file):
    result = runner.invoke(
        app,
        ["compile", model_file, "--sql", 'SELECT "Customer Country", "Total Revenue" FROM model'],
    )
    assert result.exit_code == 0
    assert "SELECT" in result.stdout
    assert "Customer Country" in result.stdout


def test_compile_requires_exactly_one_query_input(model_file, query_file):
    # both -q and --sql → error
    both = runner.invoke(
        app, ["compile", model_file, "-q", query_file, "--sql", "SELECT x FROM model"]
    )
    assert both.exit_code != 0
    # neither → error
    neither = runner.invoke(app, ["compile", model_file])
    assert neither.exit_code != 0


def test_compile_obsql_remote(monkeypatch, model_file):
    from orionbelt.cli import _remote

    def fake_compile_obsql(self, sql, dialect):
        return {"sql": f"-- {sql}", "dialect": dialect or "postgres", "sql_valid": True}

    monkeypatch.setattr(_remote.RemoteClient, "compile_obsql", fake_compile_obsql)
    result = runner.invoke(
        app, ["compile", "--sql", "SELECT a FROM m", "-s", "http://example", "-d", "mysql"]
    )
    assert result.exit_code == 0
    assert "SELECT a FROM m" in result.stdout


# -- describe / diagram / graph --------------------------------------------


def test_describe(model_file):
    result = runner.invoke(app, ["describe", model_file])
    assert result.exit_code == 0
    assert "Customers" in result.output


def test_describe_json(model_file):
    result = runner.invoke(app, ["describe", model_file, "-f", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert {d["name"] for d in payload["dimensions"]} >= {"Customer Country"}


def test_diagram(model_file):
    result = runner.invoke(app, ["diagram", model_file])
    assert result.exit_code == 0
    assert "erDiagram" in result.stdout


def test_graph(model_file):
    result = runner.invoke(app, ["graph", model_file])
    assert result.exit_code == 0
    assert "obsl:" in result.stdout


# -- convert ----------------------------------------------------------------


def test_convert_obml_to_osi(model_file):
    result = runner.invoke(app, ["convert", "obml-to-osi", model_file])
    assert result.exit_code == 0
    assert "semantic_model" in result.stdout


def test_convert_roundtrip_osi_to_obml(model_file, tmp_path):
    osi = runner.invoke(app, ["convert", "obml-to-osi", model_file])
    assert osi.exit_code == 0
    osi_file = tmp_path / "model.osi.yaml"
    osi_file.write_text(osi.stdout, encoding="utf-8")
    back = runner.invoke(app, ["convert", "osi-to-obml", str(osi_file)])
    assert back.exit_code == 0
    assert "dataObjects" in back.stdout


# -- dialects ---------------------------------------------------------------


def test_dialects(model_file):
    result = runner.invoke(app, ["dialects", "-f", "json"])
    assert result.exit_code == 0
    names = json.loads(result.stdout)
    assert "snowflake" in names
    assert "duckdb" in names


# -- version ----------------------------------------------------------------


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "obsl" in result.stdout


# -- stdin ------------------------------------------------------------------


def test_validate_from_stdin():
    result = runner.invoke(app, ["validate", "-"], input=SAMPLE_MODEL_YAML)
    assert result.exit_code == 0


# -- remote path (mocked) ---------------------------------------------------


def test_compile_remote_curated(monkeypatch, query_file):
    """Remote compile queries the server's curated model — no MODEL needed."""
    from orionbelt.cli import _remote

    def fake_compile(self, query, dialect):
        return {"sql": "SELECT 1", "dialect": dialect or "postgres", "sql_valid": True}

    monkeypatch.setattr(_remote.RemoteClient, "compile", fake_compile)
    result = runner.invoke(
        app, ["compile", "-q", query_file, "-s", "http://example", "-d", "mysql"]
    )
    assert result.exit_code == 0
    assert "SELECT 1" in result.stdout


def test_execute_remote_curated(monkeypatch, query_file):
    from orionbelt.cli import _remote

    def fake_execute(self, query, dialect):
        return {
            "columns": [{"name": "Customer Country"}, {"name": "Revenue"}],
            "rows": [["US", 100]],
            "row_count": 1,
            "execution_time_ms": 1.0,
            "dialect": dialect or "postgres",
        }

    monkeypatch.setattr(_remote.RemoteClient, "execute", fake_execute)
    result = runner.invoke(app, ["execute", "-q", query_file, "-s", "http://example", "-f", "csv"])
    assert result.exit_code == 0
    assert "US" in result.stdout


def test_compile_local_requires_model(query_file):
    """Without --server, MODEL is required."""
    result = runner.invoke(app, ["compile", "-q", query_file])
    assert result.exit_code != 0


def test_explicit_model_overrides_env_server(monkeypatch, model_file, query_file):
    """An ambient OBSL_SERVER must not silently redirect an explicit local compile.

    No RemoteClient is mocked here: if the command went remote it would attempt
    a real HTTP call to the bogus URL and fail. A successful local compile proves
    the provided MODEL takes precedence.
    """
    monkeypatch.setenv("OBSL_SERVER", "http://should-not-be-used.invalid")
    result = runner.invoke(app, ["compile", model_file, "-q", query_file, "-d", "duckdb"])
    assert result.exit_code == 0
    assert "SELECT" in result.stdout


def test_execute_remote_sql_limit_warns(monkeypatch, query_file):
    """--limit can't be honored for remote --sql; the CLI warns instead of lying."""
    from orionbelt.cli import _remote

    def fake_execute_obsql(self, sql, dialect):
        return {
            "columns": [{"name": "x"}],
            "rows": [[1]],
            "row_count": 1,
            "execution_time_ms": 1.0,
            "dialect": "duckdb",
        }

    monkeypatch.setattr(_remote.RemoteClient, "execute_obsql", fake_execute_obsql)
    result = runner.invoke(
        app, ["execute", "--sql", "SELECT x FROM m", "-s", "http://example", "--limit", "5"]
    )
    assert result.exit_code == 0
    assert "limit" in result.output.lower()


def test_validate_remote(monkeypatch, model_file):
    from orionbelt.cli import _remote

    def fake_validate(self, model_yaml):
        return {"valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(_remote.RemoteClient, "validate", fake_validate)
    result = runner.invoke(app, ["validate", model_file, "-s", "http://example", "-f", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["valid"] is True
