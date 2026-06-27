"""HTTP client for the ``obsl --server`` remote path.

Targets a deployed OrionBelt REST API. Stateless model+query work goes
through ``POST /v1/oneshot/batch`` (which accepts a model and runs queries in
one round trip); validation and conversion use their dedicated stateless
endpoints. The local file is always the source of truth — it is uploaded with
each call, so no session has to be created on the server.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from orionbelt.cli._local import CliError
from orionbelt.models.query import QueryObject

# Generous default: a remote ``execute`` may hit a cold warehouse connection.
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class RemoteClient:
    """Thin wrapper over the OrionBelt REST API for the CLI's remote path."""

    def __init__(self, server: str, api_key: str | None = None) -> None:
        self.base = server.rstrip("/")
        self._headers: dict[str, str] = {}
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

    def _batch_one(
        self, model_yaml: str, query: QueryObject, dialect: str | None, *, execute: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model_yaml": model_yaml,
            "queries": [{"id": "q0", "query": query.model_dump(by_alias=True, mode="json")}],
            "execute": execute,
        }
        if dialect:
            body["dialect"] = dialect
        data = self._post("/oneshot/batch", body)
        results = data.get("results") or []
        if not results:
            raise CliError("Server returned no results for the query")
        item = results[0]
        if item.get("status") == "error":
            err = item.get("error") or {}
            raise CliError(f"{err.get('code', 'ERROR')}: {err.get('message', 'query failed')}")
        return cast("dict[str, Any]", item)

    def compile(self, model_yaml: str, query: QueryObject, dialect: str | None) -> dict[str, Any]:
        return self._batch_one(model_yaml, query, dialect, execute=False)

    def execute(self, model_yaml: str, query: QueryObject, dialect: str | None) -> dict[str, Any]:
        return self._batch_one(model_yaml, query, dialect, execute=True)

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
