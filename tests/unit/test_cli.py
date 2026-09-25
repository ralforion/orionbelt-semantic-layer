"""Tests for the ``obsl`` command-line interface.

These exercise the local (in-process) command paths via Typer's ``CliRunner``.
The remote (``--server``) paths are covered by monkeypatching ``RemoteClient``
so no live server is required.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def test_convert_obml_to_osi_surfaces_authored_label(tmp_path):
    """An authored ``label:`` in the OBML input is surfaced as a schema warning
    (advisory) instead of being silently coerced away. Conversion still runs.
    See #221.
    """
    import yaml

    raw = yaml.safe_load(SAMPLE_MODEL_YAML)
    mkey = next(iter(raw["measures"]))
    raw["measures"][mkey]["label"] = "Authored"
    bad = tmp_path / "model.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    result = runner.invoke(app, ["convert", "obml-to-osi", str(bad)])
    assert result.exit_code == 0  # advisory: conversion still succeeds
    assert "semantic_model" in result.stdout  # output still produced
    assert "label" in result.output.lower()  # the violation is surfaced


def test_convert_roundtrip_osi_to_obml(model_file, tmp_path):
    osi = runner.invoke(app, ["convert", "obml-to-osi", model_file])
    assert osi.exit_code == 0
    osi_file = tmp_path / "model.osi.yaml"
    osi_file.write_text(osi.stdout, encoding="utf-8")
    back = runner.invoke(app, ["convert", "osi-to-obml", str(osi_file)])
    assert back.exit_code == 0
    assert "dataObjects" in back.stdout


def test_convert_osi_to_obml_remote_surfaces_input_schema(monkeypatch, tmp_path):
    """The --server path surfaces input schema issues the REST endpoint returns
    under input_validation.schema_errors (not warnings). See #225."""
    from orionbelt.cli import _remote

    def fake(self, input_yaml):
        return {
            "output_yaml": "dataObjects: {}\n",
            "warnings": [],
            "input_validation": {
                "schema_valid": False,
                "schema_errors": ["[semantic_model.0.datasets.0] bad OSI input"],
            },
        }

    monkeypatch.setattr(_remote.RemoteClient, "convert_osi_to_obml", fake)
    osi_file = tmp_path / "in.osi.yaml"
    osi_file.write_text("version: '0.2.0.dev0'\n", encoding="utf-8")
    result = runner.invoke(app, ["convert", "osi-to-obml", str(osi_file), "-s", "http://example"])
    assert result.exit_code == 0
    assert "dataObjects" in result.stdout
    assert "bad osi input" in result.output.lower()


def test_convert_obml_to_osi_remote_surfaces_input_schema(monkeypatch, tmp_path):
    """The --server obml-to-osi path likewise surfaces input_validation
    schema errors (latent gap from #223's REST change)."""
    from orionbelt.cli import _remote

    def fake(self, input_yaml, *, model_name="semantic_model"):
        return {
            "output_yaml": "semantic_model: []\n",
            "warnings": [],
            "input_validation": {
                "schema_valid": False,
                "schema_errors": ["[measures.Revenue] 'label' was unexpected"],
            },
        }

    monkeypatch.setattr(_remote.RemoteClient, "convert_obml_to_osi", fake)
    obml_file = tmp_path / "in.yaml"
    obml_file.write_text("version: 1.0\n", encoding="utf-8")
    result = runner.invoke(app, ["convert", "obml-to-osi", str(obml_file), "-s", "http://example"])
    assert result.exit_code == 0
    assert "semantic_model" in result.stdout
    assert "label" in result.output.lower()


def test_convert_osi_to_obml_surfaces_input_schema_issue(model_file, tmp_path):
    """A schema violation in the OSI input is surfaced as a warning (advisory),
    mirroring the REST endpoint and the obml-to-osi CLI path. Conversion still
    runs. See #225.
    """
    import yaml

    # Generate a genuinely valid OSI document, then inject an unexpected field
    # property the OSI schema forbids (but the converter tolerates).
    osi = runner.invoke(app, ["convert", "obml-to-osi", model_file])
    assert osi.exit_code == 0
    doc = yaml.safe_load(osi.stdout)
    doc["semantic_model"][0]["datasets"][0]["fields"][0]["bogusProp"] = "x"
    osi_file = tmp_path / "bad.osi.yaml"
    osi_file.write_text(yaml.safe_dump(doc), encoding="utf-8")

    back = runner.invoke(app, ["convert", "osi-to-obml", str(osi_file)])
    assert back.exit_code == 0  # advisory: conversion still succeeds
    assert "dataObjects" in back.stdout  # output still produced
    assert "bogusprop" in back.output.lower()  # the violation is surfaced


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

    def fake_validate(self, model_yaml, *, online=False, dialect=None):
        return {"valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(_remote.RemoteClient, "validate", fake_validate)
    result = runner.invoke(app, ["validate", model_file, "-s", "http://example", "-f", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["valid"] is True


def test_validate_remote_forwards_online_flags(monkeypatch, model_file):
    from orionbelt.cli import _remote

    seen = {}

    def fake_validate(self, model_yaml, *, online=False, dialect=None):
        seen["online"] = online
        seen["dialect"] = dialect
        return {"valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(_remote.RemoteClient, "validate", fake_validate)
    result = runner.invoke(
        app,
        ["validate", model_file, "-s", "http://example", "-f", "json", "--online", "-d", "duckdb"],
    )
    assert result.exit_code == 0
    assert seen == {"online": True, "dialect": "duckdb"}


def test_validate_online_query_params(monkeypatch):
    """``online`` and ``dialect`` travel as query params, not in the body."""
    from orionbelt.cli._remote import RemoteClient

    seen = {}

    def fake_request(self, method, path, *, json=None, params=None, text=False):
        seen["params"] = params
        return {"valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(RemoteClient, "_request", fake_request)
    RemoteClient("http://example", None).validate("version: 1.0", online=True, dialect="postgres")
    assert seen["params"] == {"online": "true", "dialect": "postgres"}


def test_validate_offline_sends_no_params(monkeypatch):
    from orionbelt.cli._remote import RemoteClient

    seen = {}

    def fake_request(self, method, path, *, json=None, params=None, text=False):
        seen["params"] = params
        return {"valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(RemoteClient, "_request", fake_request)
    RemoteClient("http://example", None).validate("version: 1.0")
    assert seen["params"] is None


class TestClientCertificates:
    """``--client-cert`` / ``--client-key`` / ``--ca-cert`` on the remote path.

    These exist for one deployment shape: an ingress in front of the REST API
    that requires a client certificate. OBSL does not serve TLS on REST itself,
    so there is nothing here that makes the *server* do mutual TLS - what these
    do is let the CLI satisfy a gateway that already demands it, where before
    it could not connect at all.

    Every assertion is about the error, because that is the whole value: a path
    that is missing, unreadable or not what it claims otherwise surfaces as an
    SSL exception from inside the HTTP client, naming neither the flag nor the
    file.
    """

    @staticmethod
    def _pem(tmp_path: Path, *, ca: bool = False) -> tuple[str, str]:
        """A self-signed certificate and its key, as separate PEM files.

        ``ca=True`` adds the basic constraint that makes it usable as a trust
        anchor - without it ``SSLContext.get_ca_certs()`` reports nothing,
        because a leaf certificate loaded as a CA bundle anchors nothing.
        """
        pytest.importorskip("cryptography", reason="cryptography required to mint a test cert")
        import datetime as dt

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "obsl-cli-test")])
        now = dt.datetime.now(dt.UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
        )
        if ca:
            builder = builder.add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
        cert = builder.sign(key, hashes.SHA256())
        cert_path = tmp_path / "client.crt"
        key_path = tmp_path / "client.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        return str(cert_path), str(key_path)

    def test_a_key_without_a_certificate_is_refused(self, tmp_path: Path) -> None:
        _, key = self._pem(tmp_path)
        result = runner.invoke(
            app, ["dialects", "--server", "https://example.invalid", "--client-key", key]
        )
        assert result.exit_code != 0
        assert "--client-cert" in result.output

    def test_a_missing_path_names_the_flag_and_the_file(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nope.crt")
        result = runner.invoke(
            app, ["dialects", "--server", "https://example.invalid", "--ca-cert", missing]
        )
        assert result.exit_code != 0
        assert "--ca-cert" in result.output
        assert "no such file" in result.output.lower()

    def test_a_file_that_is_not_a_ca_bundle_is_caught_before_the_request(
        self, tmp_path: Path
    ) -> None:
        """Readable and present is not the same as usable.

        Without this check the failure came out of the HTTP client as an
        unhandled SSL exception mentioning neither the flag nor the path.
        """
        junk = tmp_path / "not-a-ca.pem"
        junk.write_text("this is not a certificate\n")
        result = runner.invoke(
            app, ["dialects", "--server", "https://example.invalid", "--ca-cert", str(junk)]
        )
        assert result.exit_code != 0
        assert "--ca-cert" in result.output
        assert "not a usable" in result.output.lower()

    def test_a_mismatched_certificate_and_key_are_caught(self, tmp_path: Path) -> None:
        cert, _ = self._pem(tmp_path)
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        _, other_key = self._pem(other_dir)
        result = runner.invoke(
            app,
            [
                "dialects",
                "--server",
                "https://example.invalid",
                "--client-cert",
                cert,
                "--client-key",
                other_key,
            ],
        )
        assert result.exit_code != 0
        assert "--client-cert" in result.output

    def test_valid_material_reaches_the_request(self, tmp_path: Path) -> None:
        """The material loads, so the command fails on the *connection* instead.

        ``example.invalid`` cannot resolve, so reaching a connection error is
        proof the certificate checks passed rather than short-circuited.
        """
        cert, key = self._pem(tmp_path)
        result = runner.invoke(
            app,
            [
                "dialects",
                "--server",
                "https://example.invalid",
                "--client-cert",
                cert,
                "--client-key",
                key,
            ],
        )
        assert result.exit_code != 0
        assert "could not reach server" in result.output.lower()

    def test_the_flags_are_inert_without_a_server(self, tmp_path: Path) -> None:
        """Local mode opens no HTTP connection, so there is nothing to apply them to."""
        result = runner.invoke(app, ["dialects", "--ca-cert", str(tmp_path / "nope.crt")])
        assert result.exit_code == 0, result.output
        assert "duckdb" in result.output

    def test_a_client_certificate_alone_keeps_httpxs_default_trust(self, tmp_path: Path) -> None:
        """Adding --client-cert must not change who the *server* is checked against.

        The two defaults are not the same: httpx falls back to the certifi
        bundle, while ``ssl.create_default_context()`` uses OpenSSL's own store,
        which on some platforms is close to empty. Building our own context
        therefore made ``--client-cert`` silently swap the trust anchors, so a
        server that verified before could start failing for a reason having
        nothing to do with the client certificate.

        Compared by the actual CA set rather than by construction, because the
        defect was that two plausible-looking constructions disagree.
        """
        import httpx

        from orionbelt.cli._remote import resolve_tls

        cert, key = self._pem(tmp_path)
        ours = resolve_tls(cert, key, None).context
        assert ours is not None
        httpx_default = httpx.create_ssl_context(verify=True)
        assert ours.get_ca_certs() == httpx_default.get_ca_certs()

    def test_a_ca_bundle_replaces_that_trust_rather_than_adding_to_it(self, tmp_path: Path) -> None:
        """The other half: --ca-cert is an override, and must actually override."""
        import httpx

        from orionbelt.cli._remote import resolve_tls

        ca_cert, _ = self._pem(tmp_path, ca=True)
        ours = resolve_tls(None, None, ca_cert).context
        assert ours is not None
        assert ours.get_ca_certs() != httpx.create_ssl_context(verify=True).get_ca_certs()
        assert len(ours.get_ca_certs()) == 1, "only the bundle we named should be trusted"

    def test_no_path_uses_a_deprecated_httpx_api(self, tmp_path: Path) -> None:
        """Both context constructions must be current API.

        ``verify=<str>`` is deprecated in httpx 0.28 and would eventually stop
        working; ``cert=`` already did, which is how the first version of this
        code got caught. Pinned as an error so the next deprecation surfaces
        here rather than in a release.
        """
        import warnings

        from orionbelt.cli._remote import resolve_tls

        ca_cert, _ = self._pem(tmp_path, ca=True)
        cert, key = self._pem(tmp_path)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            resolve_tls(cert, key, None)
            resolve_tls(None, None, ca_cert)
            resolve_tls(cert, key, ca_cert)

    def test_a_tilde_path_is_expanded_before_openssl_sees_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``~/ca.pem`` passed the existence check and then failed in the handshake.

        The check expanded the path and the SSL call did not, so validating
        early achieved the opposite of what it is for.
        """
        from orionbelt.cli._remote import resolve_tls

        home = tmp_path / "home"
        home.mkdir()
        cert, key = self._pem(home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        tls = resolve_tls(f"~/{Path(cert).name}", f"~/{Path(key).name}", None)
        assert tls.context is not None


# -- sparql -----------------------------------------------------------------

_ASK = "ASK { ?s ?p ?o }"
_SELECT = "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"


def test_sparql_select_local(model_file):
    result = runner.invoke(app, ["sparql", model_file, "--sparql", _SELECT, "-f", "csv"])
    assert result.exit_code == 0, result.output
    header, value = result.stdout.strip().splitlines()[:2]
    assert header == "n"
    assert int(value) > 0


def test_sparql_ask_local(model_file):
    result = runner.invoke(app, ["sparql", model_file, "--sparql", _ASK])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "true"


def test_sparql_query_file_json(model_file, tmp_path):
    q = tmp_path / "q.rq"
    q.write_text(_ASK, encoding="utf-8")
    result = runner.invoke(app, ["sparql", model_file, "-q", str(q), "-f", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "type": "ask",
        "variables": [],
        "results": [],
        "boolean": True,
    }


def test_sparql_requires_exactly_one_query_input(model_file):
    result = runner.invoke(app, ["sparql", model_file])
    assert result.exit_code != 0


def test_sparql_update_is_refused(model_file):
    result = runner.invoke(
        app, ["sparql", model_file, "--sparql", "INSERT DATA { <a:x> <a:y> <a:z> }"]
    )
    assert result.exit_code == 1
    assert "error" in result.output.lower()


def test_sparql_remote(monkeypatch):
    from orionbelt.cli import _remote

    def fake_request(self, method, path, *, json=None, params=None, text=False):
        assert (method, path, json) == ("POST", "/sparql", {"query": _SELECT})
        return {"type": "select", "variables": ["n"], "results": [{"n": "7"}], "boolean": None}

    monkeypatch.setattr(_remote.RemoteClient, "_request", fake_request)
    result = runner.invoke(app, ["sparql", "--sparql", _SELECT, "-s", "http://x", "-f", "csv"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().splitlines() == ["n", "7"]


# -- rules ------------------------------------------------------------------

_COMMERCE = str(Path(__file__).resolve().parents[2] / "examples" / "orionbelt_1_commerce.yaml")


def _fake_execution(monkeypatch):
    """Replace the warehouse call: every rule query returns one row."""
    from orionbelt.cli import _local
    from orionbelt.service.db_executor import ColumnMeta, ExecutionResult

    def fake_run(model, compiled, dialect):
        return ExecutionResult([ColumnMeta("Name", "str")], raw_rows=[["x"]], row_count=1)

    monkeypatch.setattr(_local, "_run_compiled", fake_run)


def test_rules_list_local():
    result = runner.invoke(app, ["rules", "list", _COMMERCE, "-d", "duckdb", "-f", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["dialect"] == "duckdb"
    names = [r["name"] for r in data["rules"]]
    assert "High Value Client" in names and "Non-Negative Margin" in names
    assert all(r["compiles"] for r in data["rules"])


def test_rules_compile_one_prints_sql():
    result = runner.invoke(
        app, ["rules", "compile", _COMMERCE, "-r", "High Value Client", "-d", "duckdb"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("-- Rule: High Value Client (matches)")
    assert "HAVING" in result.stdout


def test_rules_unknown_rule_fails_cleanly():
    result = runner.invoke(app, ["rules", "compile", _COMMERCE, "-r", "Nope"])
    assert result.exit_code == 1
    assert "Unknown rule(s): Nope" in result.output


def test_rules_compile_failure_exits_nonzero(monkeypatch):
    from orionbelt.cli import _local

    real_compile = _local._compile

    def flaky(store, model_id, query, dialect):
        if "Total Sales" in query.select.measures:
            raise _local.CliError("boom")
        return real_compile(store, model_id, query, dialect)

    monkeypatch.setattr(_local, "_compile", flaky)
    result = runner.invoke(app, ["rules", "compile", _COMMERCE, "-d", "duckdb"])
    assert result.exit_code == 1
    assert "High Value Client: boom" in result.output
    assert "-- Rule: Electronics Sale" in result.stdout


def test_rules_evaluate_summary(monkeypatch):
    _fake_execution(monkeypatch)
    result = runner.invoke(
        app, ["rules", "evaluate", _COMMERCE, "--type", "validation", "-d", "duckdb", "-f", "json"]
    )
    assert result.exit_code == 0, result.output
    rules = json.loads(result.stdout)["rules"]
    assert {r["name"] for r in rules} == {"Non-Negative Margin", "Healthy Category"}
    assert all(r["status"] == "executed" and r["finding_count"] == 1 for r in rules)
    assert all("LIMIT 20" in r["sql"] for r in rules)


def test_rules_evaluate_one_prints_findings(monkeypatch):
    _fake_execution(monkeypatch)
    result = runner.invoke(
        app,
        ["rules", "evaluate", _COMMERCE, "-r", "Low Stock Product", "-d", "duckdb", "-f", "csv"],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().splitlines() == ["Name", "x"]


def test_rules_evaluate_limit_marks_truncated_count(monkeypatch):
    _fake_execution(monkeypatch)
    result = runner.invoke(
        app,
        ["rules", "evaluate", _COMMERCE, "--severity", "error", "--limit", "1", "-f", "csv"],
    )
    assert result.exit_code == 0, result.output
    assert "Non-Negative Margin,validation,error,executed,1+," in result.stdout


def test_rules_evaluate_remote_report(monkeypatch):
    from orionbelt.cli import _remote

    def fake_request(self, method, path, *, json=None, params=None, text=False):
        assert (method, path) == ("POST", "/rules/evaluate")
        assert json == {
            "limit": 5,
            "include_rows": True,
            "include_sql": True,
            "types": ["eligibility"],
        }
        return {
            "dialect": "duckdb",
            "results": [
                {
                    "name": "High Value Client",
                    "type": "eligibility",
                    "level": "aggregate",
                    "findings": "matches",
                    "status": "executed",
                    "finding_count": 2,
                    "columns": ["Client Name"],
                    "rows": [["A"], ["B"]],
                },
                {
                    "name": "Broken",
                    "type": "eligibility",
                    "level": "row",
                    "findings": "matches",
                    "status": "failed",
                    "error": "no such column",
                },
            ],
        }

    monkeypatch.setattr(_remote.RemoteClient, "_request", fake_request)
    result = runner.invoke(
        app,
        [
            "rules",
            "evaluate",
            "--type",
            "eligibility",
            "--limit",
            "5",
            "-s",
            "http://x",
            "-f",
            "csv",
        ],
    )
    assert result.exit_code == 1
    assert "High Value Client,eligibility,,executed,2," in result.stdout
    assert "Broken,eligibility,,failed,,no such column" in result.stdout


def test_rules_evaluate_remote_one_rule(monkeypatch):
    from orionbelt.cli import _remote

    def fake_request(self, method, path, *, json=None, params=None, text=False):
        assert (method, path, json) == (
            "POST",
            "/rules/Low%20Stock%20Product/evaluate",
            {"limit": 20},
        )
        return {
            "name": "Low Stock Product",
            "type": "classification",
            "level": "aggregate",
            "findings": "matches",
            "dialect": "duckdb",
            "columns": [{"name": "Product Name"}],
            "rows": [["Duo"]],
            "row_count": 1,
        }

    monkeypatch.setattr(_remote.RemoteClient, "_request", fake_request)
    result = runner.invoke(
        app, ["rules", "evaluate", "-r", "Low Stock Product", "-s", "http://x", "-f", "csv"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().splitlines() == ["Product Name", "Duo"]


def test_rules_list_remote(monkeypatch):
    from orionbelt.cli import _remote

    def fake_request(self, method, path, *, json=None, params=None, text=False):
        assert (method, path, params) == ("GET", "/rules", {"dialect": "postgres"})
        return {
            "dialect": "postgres",
            "rules": [
                {
                    "name": "A",
                    "type": "validation",
                    "level": "row",
                    "findings": "violations",
                    "severity": "error",
                    "executable": False,
                    "error": "bad",
                },
            ],
        }

    monkeypatch.setattr(_remote.RemoteClient, "_request", fake_request)
    result = runner.invoke(app, ["rules", "list", "-d", "postgres", "-s", "http://x", "-f", "csv"])
    assert result.exit_code == 0, result.output
    assert "A,validation,row,error,violations,no,bad" in result.stdout


# -- diagram / graph downloads ------------------------------------------------


def test_diagram_markdown_to_file(model_file, tmp_path):
    out = tmp_path / "er.md"
    result = runner.invoke(app, ["diagram", model_file, "-o", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    assert text.startswith("```mermaid\n")
    assert text.endswith("\n```\n")
    assert "erDiagram" in text


def test_diagram_markdown_flag_to_stdout(model_file):
    result = runner.invoke(app, ["diagram", model_file, "--md"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("```mermaid\n")


def test_diagram_plain_file_stays_raw(model_file, tmp_path):
    out = tmp_path / "er.mmd"
    result = runner.invoke(app, ["diagram", model_file, "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert not out.read_text(encoding="utf-8").startswith("```")


def test_diagram_remote(monkeypatch, tmp_path):
    from orionbelt.cli import _remote

    def fake_request(self, method, path, *, json=None, params=None, text=False):
        assert (method, path) == ("GET", "/diagram/er")
        assert params == {"show_columns": "false", "theme": "dark"}
        return {"mermaid": "erDiagram\n    A ||--o{ B : x"}

    monkeypatch.setattr(_remote.RemoteClient, "_request", fake_request)
    out = tmp_path / "er.md"
    result = runner.invoke(
        app, ["diagram", "--no-columns", "--theme", "dark", "-s", "http://x", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == "```mermaid\nerDiagram\n    A ||--o{ B : x\n```\n"


def test_graph_to_file(model_file, tmp_path):
    out = tmp_path / "model.ttl"
    result = runner.invoke(app, ["graph", model_file, "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "@prefix obsl:" in out.read_text(encoding="utf-8")


def test_graph_remote(monkeypatch):
    from orionbelt.cli import _remote

    def fake_request(self, method, path, *, json=None, params=None, text=False):
        assert (method, path, text) == ("GET", "/graph", True)
        return "@prefix obsl: <https://ralforion.com/ns/obsl#> .\n"

    monkeypatch.setattr(_remote.RemoteClient, "_request", fake_request)
    result = runner.invoke(app, ["graph", "-s", "http://x"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("@prefix obsl:")


def test_graph_local_requires_model():
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 1
    assert "MODEL is required" in result.output
