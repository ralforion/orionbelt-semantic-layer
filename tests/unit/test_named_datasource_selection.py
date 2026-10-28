"""A model picks the data source its queries run on: settings.defaultDataSource.

Two DuckDB files, shop-eu and shop-us, stand in for two databases of one
dialect. A model naming one of them must be answered from it on every
surface; a model naming none runs on the sole source of its dialect, and is
refused when there are several.
"""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("ob_flight")
duckdb = pytest.importorskip("duckdb")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from orionbelt.api.app import create_app  # noqa: E402
from orionbelt.api.deps import init_session_manager, reset_session_manager  # noqa: E402
from orionbelt.cache import datasource_key_for  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402
from orionbelt.service.datasource_selection import (  # noqa: E402
    DataSourceUnavailableError,
    describe_datasources,
    resolve_datasource,
)
from orionbelt.service.db_executor import ExecutionUnavailableError  # noqa: E402
from orionbelt.service.model_store import ModelStore, ModelValidationError  # noqa: E402
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from orionbelt.settings import Settings  # noqa: E402


def shop_model(source: str | None = None) -> str:
    """One data object, ``shop``, whose single row names the database it is in."""
    named = f"  defaultDataSource: {source}\n" if source else ""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
{named}dataObjects:
  Shop:
    code: shop
    columns:
      Region:
        code: region
        abstractType: string
dimensions:
  Region:
    dataObject: Shop
    column: Region
    resultType: string
"""


REGION = QueryObject.model_validate({"select": {"dimensions": ["Region"]}})


@pytest.fixture
def shops(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Configure shop-eu and shop-us, two DuckDB files, in the environment."""
    for name in ("eu", "us"):
        con = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
        con.execute(f"CREATE TABLE shop AS SELECT '{name}' AS region")
        con.close()
    for variable in ("DUCKDB_DATABASE", "DUCKDB_DATABASE_PATH", "MOTHERDUCK_ACCESS_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OBSL_DATABASES", "shop-eu,shop-us")
    monkeypatch.setenv("DB_SHOP_EU_TYPE", "duckdb")
    monkeypatch.setenv("DB_SHOP_EU_DESCRIPTION", "EU web shop")
    monkeypatch.setenv("DB_SHOP_EU_DUCKDB_DATABASE", str(tmp_path / "eu.duckdb"))
    monkeypatch.setenv("DB_SHOP_US_TYPE", "duckdb")
    monkeypatch.setenv("DB_SHOP_US_DUCKDB_DATABASE", str(tmp_path / "us.duckdb"))
    return tmp_path


def _model(yaml: str) -> Any:
    raw, source_map = TrackedLoader().load_string(yaml)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


class TestSetting:
    def test_the_name_is_read(self) -> None:
        settings = _model(shop_model("shop-eu")).settings

        assert settings is not None and settings.default_data_source == "shop-eu"

    def test_an_empty_name_is_a_wrong_value(self) -> None:
        raw, source_map = TrackedLoader().load_string(shop_model('""'))
        _, result = ReferenceResolver().resolve(raw, source_map)

        assert [e.code for e in result.errors] == ["INVALID_SETTING"]
        assert "defaultDataSource" in result.errors[0].message


class TestResolution:
    def test_the_named_source(self, shops: Any) -> None:
        source = resolve_datasource(_model(shop_model("Shop US")), "duckdb")

        assert source is not None and source.name == "shop-us"

    def test_without_named_sources_the_dialects_variables_apply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OBSL_DATABASES", raising=False)
        monkeypatch.setenv("DUCKDB_DATABASE", ":memory:")

        assert resolve_datasource(_model(shop_model()), "duckdb") is None

    def test_the_sole_named_source_of_the_dialect(
        self, shops: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OBSL_DATABASES", "shop-eu")

        source = resolve_datasource(_model(shop_model()), "duckdb")

        assert source is not None and source.name == "shop-eu"

    def test_several_sources_and_no_name_is_refused(self, shops: Any) -> None:
        with pytest.raises(ExecutionUnavailableError, match=r"\(shop-eu, shop-us\)"):
            resolve_datasource(_model(shop_model()), "duckdb")

    def test_an_unknown_name_lists_the_configured_ones(self, shops: Any) -> None:
        with pytest.raises(ExecutionUnavailableError, match="shop-asia.*shop-eu, shop-us"):
            resolve_datasource(_model(shop_model("shop-asia")), "duckdb")

    def test_a_source_of_another_dialect_is_refused(self, shops: Any) -> None:
        with pytest.raises(ExecutionUnavailableError, match="which is duckdb.*for postgres"):
            resolve_datasource(_model(shop_model("shop-eu")), "postgres")

    def test_an_older_driver_runs_a_model_that_names_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ob-flight-extension 2.36.0 on PyPI has no ob_flight.datasources.
        monkeypatch.setitem(sys.modules, "ob_flight.datasources", None)

        assert resolve_datasource(_model(shop_model()), "duckdb") is None
        with pytest.raises(ExecutionUnavailableError, match="upgrade it"):
            resolve_datasource(_model(shop_model("shop-eu")), "duckdb")

    def test_the_listing_shows_no_secret(self, shops: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MOTHERDUCK_ACCESS_TOKEN", "s3cret")

        listed = describe_datasources()

        assert [(d["name"], d["description"]) for d in listed] == [
            ("shop-eu", "EU web shop"),
            ("shop-us", None),
        ]
        # A DuckDB path is not shown: a MotherDuck one can carry its token.
        assert listed[0]["target"] == {}
        assert "s3cret" not in repr(listed)


class TestLoad:
    def test_a_source_given_at_load_replaces_the_models(self, shops: Any) -> None:
        store = ModelStore()
        model_id = store.load_model(shop_model("shop-eu"), datasource="shop-us").model_id

        settings = store.get_model(model_id).settings
        assert settings is not None and settings.default_data_source == "shop-us"

    def test_the_same_yaml_on_another_source_is_another_model(self, shops: Any) -> None:
        store = ModelStore()
        eu = store.load_model(shop_model(), datasource="shop-eu").model_id
        us = store.load_model(shop_model(), datasource="shop-us").model_id

        assert eu != us
        assert store.load_model(shop_model(), datasource="shop-us").model_load == "reused"

    def test_where_queries_execute_an_unknown_name_fails_the_load(self, shops: Any) -> None:
        with pytest.raises(ModelValidationError) as caught:
            ModelStore().load_model(shop_model("shop-asia"), execution_dialect="duckdb")

        (error,) = caught.value.errors
        assert error.code == "DATA_SOURCE_UNAVAILABLE"
        assert error.path == "settings.defaultDataSource"

    def test_without_execution_any_name_loads(self, shops: Any) -> None:
        assert ModelStore().load_model(shop_model("shop-asia")).model_id

    def test_a_reused_model_is_checked_too(self, shops: Any) -> None:
        """Loaded once where nothing executes, the same bytes are not let
        through unchecked where queries do."""
        store = ModelStore()
        store.load_model(shop_model("shop-asia"))

        with pytest.raises(ModelValidationError) as caught:
            store.load_model(shop_model("shop-asia"), execution_dialect="duckdb")

        assert caught.value.errors[0].code == "DATA_SOURCE_UNAVAILABLE"

    def test_a_model_shared_by_another_session_is_checked_too(self, shops: Any) -> None:
        from orionbelt.service.model_cache import ModelCache

        shared = ModelCache()
        ModelStore(shared_cache=shared).load_model(shop_model("shop-asia"))

        with pytest.raises(ModelValidationError):
            ModelStore(shared_cache=shared).load_model(
                shop_model("shop-asia"), execution_dialect="duckdb"
            )


class TestOnlineValidation:
    """The probe opens the connection the model's queries open: its source."""

    def test_the_probe_follows_the_models_source(
        self, shops: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orionbelt.cli import _local

        monkeypatch.setenv("DB_VENDOR", "postgres")

        summary = _local.validate(shop_model("shop-us"), online=True)

        assert summary.valid, summary.errors

    def test_an_explicit_dialect_still_decides(self, shops: Any) -> None:
        from orionbelt.cli import _local

        summary = _local.validate(shop_model("shop-us"), online=True, dialect="postgres")

        assert [e.code for e in summary.errors] == ["DATASOURCE_UNAVAILABLE"]
        assert "which is duckdb" in summary.errors[0].message


@pytest.fixture
async def client(shops: Any) -> AsyncIterator[AsyncClient]:
    app = create_app(settings=Settings(session_ttl_seconds=3600, session_cleanup_interval=9999))
    mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    init_session_manager(mgr, query_execute_enabled=True, db_vendor="duckdb")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        reset_session_manager()
        mgr.stop()


async def _regions(client: AsyncClient, yaml: str, **load: Any) -> Any:
    session = (await client.post("/v1/sessions")).json()["session_id"]
    loaded = await client.post(f"/v1/sessions/{session}/models", json={"model_yaml": yaml, **load})
    if loaded.status_code != 201:
        return loaded
    model_id = loaded.json()["model_id"]
    response = await client.post(
        f"/v1/sessions/{session}/query/execute",
        json={"model_id": model_id, "query": REGION.model_dump(by_alias=True, exclude_none=True)},
    )
    assert response.status_code == 200, response.text
    return response.json()["rows"]


class TestRest:
    async def test_each_model_is_answered_from_its_source(self, client: AsyncClient) -> None:
        assert await _regions(client, shop_model("shop-eu")) == [["eu"]]
        assert await _regions(client, shop_model("shop-us")) == [["us"]]

    async def test_the_load_override_picks_the_source(self, client: AsyncClient) -> None:
        assert await _regions(client, shop_model("shop-eu"), datasource="shop-us") == [["us"]]

    async def test_an_unknown_source_is_a_422_at_load(self, client: AsyncClient) -> None:
        response = await _regions(client, shop_model("shop-asia"))

        assert response.status_code == 422
        assert response.json()["detail"]["errors"][0]["code"] == "DATA_SOURCE_UNAVAILABLE"

    async def test_an_ambiguous_model_is_a_503_at_execution(self, client: AsyncClient) -> None:
        session = (await client.post("/v1/sessions")).json()["session_id"]
        model_id = (
            await client.post(f"/v1/sessions/{session}/models", json={"model_yaml": shop_model()})
        ).json()["model_id"]

        response = await client.post(
            f"/v1/sessions/{session}/query/execute",
            json={
                "model_id": model_id,
                "query": REGION.model_dump(by_alias=True, exclude_none=True),
            },
        )

        assert response.status_code == 503
        assert "shop-eu, shop-us" in response.json()["detail"]

    async def test_the_list(self, client: AsyncClient) -> None:
        response = await client.get("/v1/datasources")

        assert response.status_code == 200
        assert [d["name"] for d in response.json()["datasources"]] == ["shop-eu", "shop-us"]

    async def test_online_validation_probes_the_models_source(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("orionbelt.api.deps.get_db_vendor", lambda: "postgres")
        session = (await client.post("/v1/sessions")).json()["session_id"]

        for path in (f"/v1/sessions/{session}/validate", "/v1/validate"):
            response = await client.post(
                path, params={"online": "true"}, json={"model_yaml": shop_model("shop-us")}
            )
            assert response.json()["valid"], (path, response.json())

    async def test_the_settings_mirror_reports_it(self, client: AsyncClient) -> None:
        session = (await client.post("/v1/sessions")).json()["session_id"]
        model_id = (
            await client.post(
                f"/v1/sessions/{session}/models", json={"model_yaml": shop_model("shop-us")}
            )
        ).json()["model_id"]

        response = await client.get(
            "/v1/settings", params={"session_id": session, "model_id": model_id}
        )

        assert response.json()["model_settings"]["defaultDataSource"] == "shop-us"


class TestCli:
    def test_execute_runs_on_the_models_source(self, shops: Any) -> None:
        from orionbelt.cli import _local

        _, executed = _local.execute_query(shop_model("shop-us"), REGION, None)

        assert executed.rows == [["us"]]

    def test_the_datasource_option_replaces_it(self, shops: Any) -> None:
        from orionbelt.cli import _local

        _, executed = _local.execute_query(
            shop_model("shop-us"), REGION, None, datasource="shop-eu"
        )

        assert executed.rows == [["eu"]]

    def test_datasources_lists_them(self, shops: Any) -> None:
        from typer.testing import CliRunner

        from orionbelt.cli.main import app

        result = CliRunner().invoke(app, ["datasources", "-f", "json"])

        assert result.exit_code == 0, result.output
        assert '"shop-us"' in result.output


class TestFlight:
    def test_an_older_orionbelt_runs_on_the_dialects_variables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """orionbelt 2.36.0 has no resolver, and none of its models a source."""
        from ob_flight import server_execution

        monkeypatch.setitem(sys.modules, "orionbelt.service.datasource_selection", None)

        assert server_execution.source_for(_model(shop_model()), "duckdb") is None

    def _server(self, model: Any) -> Any:
        from unittest.mock import MagicMock

        server = MagicMock()
        server._get_model.return_value = (model, "duckdb")
        server._cache = None
        server._cache_config = None
        server._detect_virtual_table.return_value = None
        server._batch_size = 100
        server._compile_obml.side_effect = lambda obml, m, d: (
            REGION,
            types.SimpleNamespace(sql='SELECT region AS "Region" FROM shop', physical_tables=[]),
        )
        server._rewrite_table_names.side_effect = lambda sql, m: sql
        server._source_for.side_effect = lambda m, d, compiled=None: resolve_datasource(m, d)
        return server

    def test_an_uncached_ticket_runs_on_the_source_planned(self, shops: Any) -> None:
        """No cache: the source still travels from planning to execution,
        since the call fetching the ticket need not name the model."""
        from ob_flight import server_execution

        server = self._server(_model(shop_model("shop-us")))
        server._build_cache_meta.side_effect = lambda **kw: server_execution.build_cache_meta(
            server, **kw
        )

        from unittest.mock import patch

        from ob_flight import server as flight_server

        sql, dialect, *_, meta = server_execution.prepare_sql(
            server, "select:\n  dimensions: [Region]\n"
        )
        with patch.object(flight_server, "db_connect", wraps=flight_server.db_connect) as connect:
            server_execution.execute_sql(server, sql, dialect, cache_meta=meta)

        assert meta is not None and "key" not in meta
        (target,), _ = connect.call_args
        assert target.name == "shop-us"
        assert server._cache_put_table.call_count == 0

    def test_the_cache_scope_is_the_models_source(self, shops: Any) -> None:
        from unittest.mock import patch

        from ob_flight import server_execution

        model = _model(shop_model("shop-us"))
        server = self._server(model)
        server._cache = types.SimpleNamespace(backend_name="file")
        server._cache_config = object()
        server._selector_from_context.return_value = "sales"
        server._session_manager.get_store.return_value.list_models.return_value = [
            types.SimpleNamespace(model_id="m")
        ]
        server._build_cache_meta.side_effect = lambda **kw: server_execution.build_cache_meta(
            server, **kw
        )

        def plan(**kwargs: Any) -> Any:
            return types.SimpleNamespace(
                datasource=kwargs["datasource"],
                cache_key="k",
                ttl_outcome=types.SimpleNamespace(ttl=types.SimpleNamespace(seconds=60)),
            )

        with patch("orionbelt.service.query_execution.resolve_cache_plan", side_effect=plan):
            *_, meta = server_execution.prepare_sql(server, "select:\n  dimensions: [Region]\n")

        us = resolve_datasource(model, "duckdb")
        assert us is not None and meta is not None
        assert meta["datasource"] == datasource_key_for(us)


def two_object_model(orders: str | None, customers: str | None, default: str | None = None) -> str:
    """Orders and Customers, each optionally naming its own data source."""

    def source(name: str | None) -> str:
        return f"    dataSource: {name}\n" if name else ""

    named = f"  defaultDataSource: {default}\n" if default else ""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
{named}dataObjects:
  Orders:
    code: shop
{source(orders)}    columns:
      Region:
        code: region
        abstractType: string
  Customers:
    code: shop
{source(customers)}    columns:
      Customer Region:
        code: region
        abstractType: string
dimensions:
  Region:
    dataObject: Orders
    column: Region
    resultType: string
"""


class TestPerObjectSource:
    """A data object may name its own source."""

    def test_the_name_is_read(self) -> None:
        model = _model(two_object_model("shop-us", "shop-us"))

        assert model.data_objects["Orders"].data_source == "shop-us"

    def test_an_empty_name_is_refused(self) -> None:
        raw, source_map = TrackedLoader().load_string(two_object_model('""', None))
        _, result = ReferenceResolver().resolve(raw, source_map)

        (parse_error,) = [e for e in result.errors if e.code == "DATA_OBJECT_PARSE_ERROR"]
        assert parse_error.path == "dataObjects.Orders"
        assert "dataSource must be a data source name" in parse_error.message

    def test_objects_naming_one_source_run_on_it(self, shops: Any) -> None:
        source = resolve_datasource(_model(two_object_model("shop-us", "shop-us")), "duckdb")

        assert source is not None and source.name == "shop-us"

    def test_an_objects_source_wins_over_the_models(self, shops: Any) -> None:
        model = _model(two_object_model("shop-us", "shop-us", default="shop-eu"))

        source = resolve_datasource(model, "duckdb")

        assert source is not None and source.name == "shop-us"

    def test_objects_of_two_sources_are_refused_naming_each(self, shops: Any) -> None:
        from orionbelt.service.datasource_selection import FederationNotEnabledError

        model = _model(two_object_model("shop-us", None, default="shop-eu"))

        with pytest.raises(FederationNotEnabledError) as caught:
            resolve_datasource(model, "duckdb")

        assert "FEDERATION_NOT_ENABLED" in str(caught.value)
        assert "shop-us: Orders; shop-eu: Customers" in str(caught.value)

    def test_an_object_naming_none_beside_one_naming_a_source_is_two_sources(
        self, shops: Any
    ) -> None:
        from orionbelt.service.datasource_selection import FederationNotEnabledError

        with pytest.raises(FederationNotEnabledError, match="no data source: Customers"):
            resolve_datasource(_model(two_object_model("shop-us", None)), "duckdb")

    def test_a_nested_object_is_read_from_its_parents_source(self, shops: Any) -> None:
        from orionbelt.models.semantic import NestedSource

        model = _model(two_object_model("shop-us", None))
        nested = model.data_objects["Customers"].model_copy(
            update={"nested_in": NestedSource(data_object="Orders", column="Region")}
        )
        model = model.model_copy(
            update={"data_objects": {**model.data_objects, "Customers": nested}}
        )

        source = resolve_datasource(model, "duckdb")

        assert source is not None and source.name == "shop-us"

    def test_an_unknown_object_source_fails_the_load_at_its_path(self, shops: Any) -> None:
        with pytest.raises(ModelValidationError) as caught:
            ModelStore().load_model(
                two_object_model("shop-asia", "shop-eu"), execution_dialect="duckdb"
            )

        assert [(e.code, e.path) for e in caught.value.errors] == [
            ("DATA_SOURCE_UNAVAILABLE", "dataObjects.Orders.dataSource")
        ]

    def test_a_model_of_two_known_sources_loads(self, shops: Any) -> None:
        loaded = ModelStore().load_model(
            two_object_model("shop-us", "shop-eu"), execution_dialect="duckdb"
        )

        assert loaded.model_id

    def test_inheritance_keeps_it(self) -> None:
        store = ModelStore()
        parent = store.load_model(two_object_model("shop-us", "shop-us")).model_id

        child = store.load_model("version: 1.0\n", inherits_model_id=parent).model_id

        assert store.get_model(child).data_objects["Orders"].data_source == "shop-us"

    def test_the_graph_carries_it(self) -> None:
        store = ModelStore()
        model_id = store.load_model(two_object_model("shop-us", "shop-us")).model_id

        assert '"shop-us"' in store.get_graph(model_id).turtle
        assert "obsl:dataSource" in store.get_graph(model_id).turtle

    def test_the_graph_keeps_the_models_default_apart_from_the_objects_own(self) -> None:
        """Declared on the model node; an object that names none carries none."""
        from rdflib import Literal

        from orionbelt.obsl.exporter import OBSL, export_obsl

        store = ModelStore()
        model_id = store.load_model(two_object_model("shop-us", None, default="shop-eu")).model_id
        graph = export_obsl(store.get_model(model_id), model_id)

        assert list(graph.subject_objects(OBSL.defaultDataSource)) == [
            (next(graph.subjects(OBSL.hasDataObject, None)), Literal("shop-eu"))
        ]
        assert sorted(str(o) for o in graph.objects(None, OBSL.dataSource)) == ["shop-us"]

    @staticmethod
    def _guide_query() -> str:
        """The SPARQL query docs/guide/obsl.md gives for each object's source."""
        from pathlib import Path

        guide = Path(__file__).resolve().parents[2] / "docs" / "guide" / "obsl.md"
        blocks = guide.read_text().split("```sparql\n")[1:]
        (query,) = [b.split("```")[0] for b in blocks if "obsl:defaultDataSource" in b]
        return query

    def test_sparql_reads_each_objects_source(self) -> None:
        from orionbelt.cli._local import sparql

        result = sparql(two_object_model("shop-us", None, default="shop-eu"), self._guide_query())

        assert [(r["object"], r["source"]) for r in result.results] == [
            ("Customers", "shop-eu"),
            ("Orders", "shop-us"),
        ]

    def test_sparql_gives_a_nested_object_its_root_parents_source(self) -> None:
        """As the compiler resolves it, whatever the nested object names itself.

        Reading the model's default there would call a model federated that
        reads one source.
        """
        from orionbelt.cli._local import sparql
        from orionbelt.compiler.federation import object_sources
        from orionbelt.service.datasource_selection import source_key

        yaml = """\
version: 1.0
settings:
  defaultDialect: duckdb
  defaultDataSource: shop-eu
dataObjects:
  Charges:
    code: charges
    dataSource: shop-us
    columns:
      Charge Key: {code: charge_key, abstractType: string}
  Charge Labels:
    nestedIn: {dataObject: Charges, column: Labels}
    dataSource: shop-eu
    columns:
      Label Key: {code: Key, abstractType: string}
  Label Parts:
    nestedIn: {dataObject: Charge Labels, column: Parts}
    columns:
      Part Name: {code: Name, abstractType: string}
  Accounts:
    code: accounts
    columns:
      Account Id: {code: account_id, abstractType: string}
dimensions:
  Label Key: {dataObject: Charge Labels, column: Label Key}
"""
        store = ModelStore()
        compiled = object_sources(store.get_model(store.load_model(yaml).model_id))

        result = sparql(yaml, self._guide_query())

        graph = {r["object"]: r["source"] for r in result.results}
        assert graph == {
            "Accounts": "shop-eu",
            "Charges": "shop-us",
            "Charge Labels": "shop-us",
            "Label Parts": "shop-us",
        }
        assert {name: source_key(source) for name, source in graph.items()} == compiled

    def test_without_a_default_the_model_carries_none(self) -> None:
        from orionbelt.obsl.exporter import OBSL, export_obsl

        store = ModelStore()
        model_id = store.load_model(two_object_model("shop-us", "shop-eu")).model_id

        graph = export_obsl(store.get_model(model_id), model_id)

        assert list(graph.objects(None, OBSL.defaultDataSource)) == []

    def test_the_shapes_allow_one_default_on_a_model(self) -> None:
        from pathlib import Path

        from rdflib import Graph, Namespace
        from rdflib.namespace import XSD

        sh = Namespace("http://www.w3.org/ns/shacl#")
        obsl = Namespace("https://ralforion.com/ns/obsl#")
        shapes = Graph().parse(Path(__file__).resolve().parents[2] / "ontology" / "obsl.shacl.ttl")

        (prop,) = [
            p
            for p in shapes.objects(obsl.SemanticModelShape, sh.property)
            if shapes.value(p, sh.path) == obsl.defaultDataSource
        ]
        assert shapes.value(prop, sh.maxCount).toPython() == 1
        assert shapes.value(prop, sh.datatype) == XSD.string

    def test_osi_round_trips_it(self) -> None:
        import yaml
        from osi_orionbelt.obml_to_osi import OBMLtoOSI
        from osi_orionbelt.osi_to_obml import OSItoOBML

        obml = yaml.safe_load(two_object_model("shop-us", None))

        back = OSItoOBML(OBMLtoOSI(obml).convert()).convert()

        assert back["dataObjects"]["Orders"]["dataSource"] == "shop-us"
        assert "dataSource" not in back["dataObjects"]["Customers"]


class TestPerObjectSourceRest:
    async def test_a_query_on_one_source_of_two_runs_on_that_source(
        self, client: AsyncClient
    ) -> None:
        assert await _regions(client, two_object_model("shop-us", "shop-eu")) == [["us"]]
        assert await _regions(client, two_object_model("shop-eu", "shop-us")) == [["eu"]]

    async def test_objects_naming_one_source_are_answered_from_it(
        self, client: AsyncClient
    ) -> None:
        assert await _regions(client, two_object_model("shop-us", "shop-us")) == [["us"]]

    async def test_the_schema_api_shows_it(self, client: AsyncClient) -> None:
        session = (await client.post("/v1/sessions")).json()["session_id"]
        model_id = (
            await client.post(
                f"/v1/sessions/{session}/models",
                json={"model_yaml": two_object_model("shop-us", "shop-us")},
            )
        ).json()["model_id"]

        schema = (await client.get(f"/v1/sessions/{session}/models/{model_id}/schema")).json()

        assert {o["name"]: o["data_source"] for o in schema["data_objects"]} == {
            "Orders": "shop-us",
            "Customers": "shop-us",
        }


class TestPerObjectSourceNames:
    """Source names are compared as the registry compares them."""

    def test_spellings_of_one_source_are_one_source(self, shops: Any) -> None:
        model = _model(two_object_model("Shop EU", None, default="shop-eu"))

        source = resolve_datasource(model, "duckdb")

        assert source is not None and source.name == "shop-eu"

    def test_a_single_source_model_reports_at_its_path_whatever_the_spelling(
        self, shops: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_SHOP_EU_TYPE", "postgres")

        with pytest.raises(ModelValidationError) as caught:
            ModelStore().load_model(
                two_object_model("Shop EU", "shop_eu"), execution_dialect="duckdb"
            )

        assert [e.path for e in caught.value.errors] == ["dataObjects.Orders.dataSource"]

    def test_the_rule_is_the_registrys(self) -> None:
        from ob_flight.datasources import _normalized

        from orionbelt.service.datasource_selection import source_key

        for name in ("Shop EU", "shop_eu", "shop-eu", " SHOP\t-_eu ", "crm"):
            assert source_key(name) == _normalized(name)

    def test_a_broken_registry_fails_a_multi_source_load_cleanly(
        self, shops: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DB_SHOP_US_TYPE")

        with pytest.raises(ModelValidationError) as caught:
            ModelStore().load_model(
                two_object_model("shop-us", "shop-eu"), execution_dialect="duckdb"
            )

        (error,) = caught.value.errors
        assert error.code == "DATA_SOURCE_UNAVAILABLE"
        assert "DB_SHOP_US_TYPE" in error.message


class TestBrokenRegistryRest:
    async def test_a_multi_source_upload_is_a_422(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DB_SHOP_US_TYPE")
        session = (await client.post("/v1/sessions")).json()["session_id"]

        response = await client.post(
            f"/v1/sessions/{session}/models",
            json={"model_yaml": two_object_model("shop-us", "shop-eu")},
        )

        assert response.status_code == 422
        assert response.json()["detail"]["errors"][0]["code"] == "DATA_SOURCE_UNAVAILABLE"


class TestRepairDetail:
    """A data-source error says which case it is and how to repair it."""

    def _load_error(self, model_yaml: str, dialect: str = "duckdb") -> Any:
        with pytest.raises(ModelValidationError) as caught:
            ModelStore().load_model(model_yaml, execution_dialect=dialect)
        (error,) = caught.value.errors
        return error

    def test_a_misspelt_source_suggests_the_configured_one(self, shops: Any) -> None:
        error = self._load_error(shop_model("shp-eu"))

        assert error.suggestions == ["shop-eu", "shop-us"]
        assert error.hint is not None and "OBSL_DATABASES" in error.hint
        assert error.context == {
            "reason": "unknownSource",
            "dataSource": "shp-eu",
            "dataSources": ["shop-eu", "shop-us"],
        }
        assert "Did you mean 'shop-eu' or 'shop-us'?" in error.as_text()

    def test_an_unrelated_name_suggests_nothing(self, shops: Any) -> None:
        assert self._load_error(shop_model("warehouse")).suggestions == []

    def test_an_object_source_is_reported_the_same_way(self, shops: Any) -> None:
        error = self._load_error(two_object_model("shp-eu", "shop-eu"))

        assert error.path == "dataObjects.Orders.dataSource"
        assert error.suggestions[0] == "shop-eu"
        assert error.context["reason"] == "unknownSource"

    def test_another_dialect_names_both(self, shops: Any) -> None:
        with pytest.raises(DataSourceUnavailableError) as caught:
            resolve_datasource(_model(shop_model("shop-eu")), "postgres")

        assert caught.value.context == {
            "reason": "dialectMismatch",
            "dataSource": "shop-eu",
            "sourceDialect": "duckdb",
            "dialect": "postgres",
        }
        assert "Hint: Compile for duckdb" in str(caught.value)

    def test_an_ambiguous_model_lists_the_candidates(self, shops: Any) -> None:
        with pytest.raises(DataSourceUnavailableError) as caught:
            resolve_datasource(_model(shop_model()), "duckdb")

        assert caught.value.context == {
            "reason": "ambiguousSource",
            "dialect": "duckdb",
            "dataSources": ["shop-eu", "shop-us"],
        }

    def test_a_broken_registry_says_so(self, shops: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DB_SHOP_US_TYPE")

        error = self._load_error(two_object_model("shop-us", "shop-eu"))

        assert error.context == {"reason": "invalidConfiguration"}

    async def test_the_422_carries_it(self, client: AsyncClient) -> None:
        response = await _regions(client, shop_model("shp-eu"))

        (error,) = response.json()["detail"]["errors"]
        assert error["suggestions"][0] == "shop-eu"
        assert error["context"]["reason"] == "unknownSource"
        assert "Did you mean 'shop-eu'" in response.json()["detail"]["message"]
