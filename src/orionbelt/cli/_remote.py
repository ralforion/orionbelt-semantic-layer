"""HTTP client for the ``obsl --server`` remote path.

Targets a deployed OrionBelt REST API.

``compile`` / ``execute`` run against the server's **curated** model via the
top-level ``/v1/query/sql``, ``/v1/query/execute`` and
``/v1/query/semantic-ql[/compile]`` shortcuts — the deployed model is
auto-resolved and **no model is uploaded**, so governed single-model
deployments (where ad-hoc model upload is disabled) are respected. ``validate``
and ``convert`` post the model / file you pass to their dedicated stateless
endpoints.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from orionbelt import __version__
from orionbelt.cli._local import CliError
from orionbelt.models.query import QueryObject

# Generous default: a remote ``execute`` may hit a cold warehouse connection.
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class RemoteClient:
    """Thin wrapper over the OrionBelt REST API for the CLI's remote path."""

    def __init__(self, server: str, api_key: str | None = None) -> None:
        self.base = server.rstrip("/")
        # Identify as obsl rather than the default "python-httpx/..." — some
        # WAFs (e.g. Cloud Armor in front of the demo deployment) deny the
        # generic httpx agent.
        self._headers: dict[str, str] = {"User-Agent": f"obsl/{__version__}"}
        if api_key:
            # The API accepts the key via X-API-Key (default) or Bearer; send
            # both so a server configured with a custom header name still works
            # through the Authorization fallback.
            self._headers["X-API-Key"] = api_key
            self._headers["Authorization"] = f"Bearer {api_key}"

    # -- low-level ----------------------------------------------------------

    def _post(self, path: str, json: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=json, params=params)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base}/v1{path}"
        try:
            resp = httpx.request(
                method, url, json=json, params=params, headers=self._headers, timeout=_TIMEOUT
            )
        except httpx.RequestError as exc:
            raise CliError(f"Could not reach server {self.base}: {exc}") from None
        if resp.status_code >= 400:
            raise CliError(f"Server returned {resp.status_code}: {_detail(resp)}")
        return resp.json()

    # -- operations ---------------------------------------------------------

    def validate(self, model_yaml: str) -> dict[str, Any]:
        return cast("dict[str, Any]", self._post("/validate", {"model_yaml": model_yaml}))

    def _query_body(self, query: QueryObject) -> dict[str, Any]:
        return query.model_dump(by_alias=True, mode="json", exclude_none=True)

    def compile(self, query: QueryObject, dialect: str | None) -> dict[str, Any]:
        """Compile a query against the server's curated model (no upload).

        Uses the top-level ``/query/sql`` shortcut, which auto-resolves the
        single deployed model — so this respects governed, single-model
        deployments where ad-hoc model upload is disabled.
        """
        params = {"dialect": dialect} if dialect else None
        return cast(
            "dict[str, Any]", self._post("/query/sql", self._query_body(query), params=params)
        )

    def execute(self, query: QueryObject, dialect: str | None) -> dict[str, Any]:
        """Execute a query against the server's curated model (no upload)."""
        params = {"dialect": dialect} if dialect else None
        return cast(
            "dict[str, Any]", self._post("/query/execute", self._query_body(query), params=params)
        )

    def _obsql_body(self, sql: str, dialect: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"sql": sql}
        if dialect:
            body["dialect"] = dialect
        return body

    def compile_obsql(self, sql: str, dialect: str | None) -> dict[str, Any]:
        """Compile an OBSQL string against the server's curated model."""
        return cast(
            "dict[str, Any]",
            self._post("/query/semantic-ql/compile", self._obsql_body(sql, dialect)),
        )

    def execute_obsql(self, sql: str, dialect: str | None) -> dict[str, Any]:
        """Execute an OBSQL string against the server's curated model."""
        return cast(
            "dict[str, Any]", self._post("/query/semantic-ql", self._obsql_body(sql, dialect))
        )

    def convert_osi_to_obml(self, input_yaml: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]", self._post("/convert/osi-to-obml", {"input_yaml": input_yaml})
        )

    def convert_obml_to_osi(
        self,
        input_yaml: str,
        *,
        model_name: str = "semantic_model",
        model_description: str = "",
        ai_instructions: str = "",
        include_ontology: bool = False,
    ) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            self._post(
                "/convert/obml-to-osi",
                {
                    "input_yaml": input_yaml,
                    "model_name": model_name,
                    "model_description": model_description,
                    "ai_instructions": ai_instructions,
                    "include_ontology": include_ontology,
                },
            ),
        )

    def dialects(self) -> list[str]:
        data = self._get("/dialects")
        return [d["name"] for d in data.get("dialects", [])]


def _detail(resp: httpx.Response) -> str:
    """Best-effort extraction of an error message from a JSON error body."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:500]
