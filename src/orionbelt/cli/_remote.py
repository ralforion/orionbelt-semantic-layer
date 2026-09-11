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

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from orionbelt import __version__
from orionbelt.cli._local import CliError
from orionbelt.models.query import QueryObject

# Generous default: a remote ``execute`` may hit a cold warehouse connection.
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


@dataclass(frozen=True)
class ClientTLS:
    """Certificate material for the ``--server`` connection.

    Only the ``--server`` path uses this. Local mode opens no HTTP connection at
    all, and a local ``execute`` reaches the warehouse through its vendor
    driver, whose TLS is that driver's business.

    Holds the *built* context rather than the paths: httpx 0.28 deprecates
    ``cert=`` in favour of ``verify=<ssl_context>``, and building once means the
    material validated at flag-resolution time is the same object that goes on
    the wire.
    """

    context: ssl.SSLContext | None = None

    @property
    def verify(self) -> Any:
        """The value for httpx's ``verify``.

        ``True`` keeps the default trust store and full verification. A context
        carries whatever was configured; it is never ``False``, because there is
        deliberately no flag for that - a CLI that can be told to trust anything
        gets told to trust anything.
        """
        return self.context if self.context is not None else True


def resolve_tls(client_cert: str | None, client_key: str | None, ca_cert: str | None) -> ClientTLS:
    """Validate the three settings and build a :class:`ClientTLS`.

    Every failure here is a configuration failure and says which setting is
    wrong, for the same reason the listener loaders do: a path that is missing,
    unreadable, or present but not what it claims otherwise surfaces as an SSL
    error from inside the HTTP client, naming none of them.
    """
    if client_key and not client_cert:
        raise CliError(
            "--client-key was given without --client-cert. A private key alone "
            "cannot identify a client; supply the certificate too."
        )

    # Expand here and use the expanded form everywhere below. Checking
    # ``~/ca.pem`` and then handing the literal string to OpenSSL would pass
    # this validation and fail in the handshake, which is the opposite of what
    # validating early is for.
    expanded: dict[str, str | None] = {"cert": None, "key": None, "ca": None}
    for slot, label, value in (
        ("cert", "--client-cert", client_cert),
        ("key", "--client-key", client_key),
        ("ca", "--ca-cert", ca_cert),
    ):
        if value is None:
            continue
        path = Path(value).expanduser()
        if not path.is_file():
            raise CliError(f"{label}: no such file: {path}")
        if not os.access(path, os.R_OK):
            raise CliError(f"{label}: not readable: {path}")
        expanded[slot] = str(path)

    if expanded["cert"] is None and expanded["ca"] is None:
        return ClientTLS()

    # ``httpx.create_ssl_context``, not ``ssl.create_default_context``. They do
    # not trust the same things: httpx falls back to the certifi bundle (and
    # honours SSL_CERT_FILE / SSL_CERT_DIR before it), while ssl's default is
    # OpenSSL's own store, which on some platforms is close to empty. Building
    # our own would have meant that adding --client-cert silently changed which
    # authorities the *server* is checked against, so a server that verified
    # before could start failing for an unrelated reason.
    try:
        context = httpx.create_ssl_context(verify=expanded["ca"] or True)
    except (ssl.SSLError, OSError) as exc:
        raise CliError(
            f"--ca-cert: not a usable PEM CA bundle ({path_reason(exc)}): {expanded['ca']}"
        ) from None
    if expanded["cert"]:
        try:
            context.load_cert_chain(expanded["cert"], expanded["key"])
        except (ssl.SSLError, OSError) as exc:
            flag = "--client-cert/--client-key" if expanded["key"] else "--client-cert"
            raise CliError(f"{flag}: could not load the certificate ({path_reason(exc)})") from None
    return ClientTLS(context=context)


def path_reason(exc: BaseException) -> str:
    """The useful half of an ssl/OS error, without the file path repeated."""
    reason = getattr(exc, "reason", None) or getattr(exc, "strerror", None)
    return str(reason or exc).strip() or exc.__class__.__name__


class RemoteClient:
    """Thin wrapper over the OrionBelt REST API for the CLI's remote path."""

    def __init__(
        self, server: str, api_key: str | None = None, tls: ClientTLS | None = None
    ) -> None:
        self.base = server.rstrip("/")
        self._tls = tls if tls is not None else ClientTLS()
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
            # A Client rather than ``httpx.request``: the module-level helper
            # cannot take a prepared SSL context, and the client certificate
            # lives on that context.
            with httpx.Client(
                headers=self._headers,
                timeout=_TIMEOUT,
                verify=self._tls.verify,
            ) as client:
                resp = client.request(method, url, json=json, params=params)
        except httpx.RequestError as exc:
            raise CliError(f"Could not reach server {self.base}: {exc}") from None
        if resp.status_code >= 400:
            raise CliError(f"Server returned {resp.status_code}: {_detail(resp)}")
        return resp.json()

    # -- operations ---------------------------------------------------------

    def validate(
        self, model_yaml: str, *, online: bool = False, dialect: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"online": "true"} if online else {}
        if online and dialect:
            params["dialect"] = dialect
        return cast(
            "dict[str, Any]",
            self._post("/validate", {"model_yaml": model_yaml}, params=params or None),
        )

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
