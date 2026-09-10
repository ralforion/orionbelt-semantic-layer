"""Reading a listener's TLS material off disk.

Shared by the two surfaces that listen on their own port: Arrow Flight SQL
and the Postgres wire protocol. They differ in everything except this -
Flight is TLS from the first byte and hands pyarrow raw PEM, while pgwire
negotiates with an ``SSLRequest`` and needs an ``ssl.SSLContext`` - so what
they share is the reading and the refusing, not the serving.

The failures here are configuration failures, and each one says what the
operator got wrong rather than surfacing as an ``OSError`` on a path that
looks correct. The most likely of them is not a typo: the published image
runs as a non-root user (``Dockerfile`` creates ``app`` and switches to it),
so a private key bind-mounted from the host as ``root:root 0600`` is
present, correctly named, and unreadable - which reads as a broken path
until someone thinks to check the mode.

The setting *names* are passed in rather than hard-coded, so a pgwire
misconfiguration says ``PGWIRE_TLS_KEY`` and a Flight one says
``FLIGHT_TLS_KEY``. An error naming the wrong surface's setting would be
worse than none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("orionbelt.service.tls")


class TLSConfigError(ValueError):
    """The TLS configuration cannot be used as given.

    Its own type so a caller can refuse to start with the operator's message
    rather than a stack trace from deep inside pyarrow or ``ssl``.
    """


@dataclass(frozen=True)
class ListenerTLS:
    """Loaded TLS material, plus the paths it came from.

    Flight wants the PEM bytes (pyarrow takes ``(cert, key)`` pairs); pgwire
    wants an ``ssl.SSLContext``, which ``ssl`` builds from paths rather than
    from bytes. Carrying both avoids writing the material back to a temporary
    file just to hand it to the other API.
    """

    certificates: list[tuple[bytes, bytes]]
    cert_path: str
    key_path: str
    verify_client: bool = False
    root_certificates: bytes | None = None
    client_ca_path: str | None = None

    @property
    def scheme(self) -> str:
        return "grpc+tls"

    def ssl_context(self) -> Any:
        """An ``ssl.SSLContext`` for a socket-level server (pgwire).

        Built from the paths: ``load_cert_chain`` reads files, and handing it
        the bytes we already hold would mean writing them back out.
        """
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
        if self.client_ca_path is not None:
            ctx.load_verify_locations(cafile=self.client_ca_path)
            ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx


def _read(path: str, *, setting: str) -> bytes:
    """Read one PEM file, or explain which way it went wrong."""
    p = Path(path)
    if not p.exists():
        raise TLSConfigError(f"{setting} points at {path}, which does not exist")
    if p.is_dir():
        raise TLSConfigError(f"{setting} points at {path}, which is a directory")
    try:
        data = p.read_bytes()
    except PermissionError as exc:
        raise TLSConfigError(
            f"{setting} at {path} cannot be read: {exc.strerror}. "
            "In the published image the server runs as a non-root user, so a "
            "file mounted as root-owned 0600 is unreadable to it - make it "
            "readable by the container's user (Kubernetes: defaultMode on the "
            "secret volume, or fsGroup)."
        ) from None
    if not data.strip():
        raise TLSConfigError(f"{setting} at {path} is empty")
    if b"-----BEGIN" not in data:
        raise TLSConfigError(
            f"{setting} at {path} does not look like PEM: no '-----BEGIN' header. "
            "DER and PKCS#12 are not read here; convert to PEM first."
        )
    return data


def load_listener_tls(
    cert_path: str | None,
    key_path: str | None,
    client_ca_path: str | None = None,
    *,
    prefix: str = "FLIGHT",
) -> ListenerTLS | None:
    """Build the TLS config, or ``None`` when TLS is not configured.

    Both the certificate and the key, or neither. Half a pair is refused
    rather than ignored: a deployment that set one and got plaintext would
    believe it was encrypted, which is worse than not starting.

    ``prefix`` names the surface in every message - ``FLIGHT`` or ``PGWIRE``.
    """
    if cert_path is None and key_path is None:
        if client_ca_path is not None:
            raise TLSConfigError(
                f"{prefix}_TLS_CLIENT_CA is set without {prefix}_TLS_CERT and "
                f"{prefix}_TLS_KEY. Mutual TLS needs the server's own certificate "
                "as well as the CA its clients are checked against."
            )
        return None

    if cert_path is None or key_path is None:
        missing = f"{prefix}_TLS_CERT" if cert_path is None else f"{prefix}_TLS_KEY"
        present = f"{prefix}_TLS_KEY" if cert_path is None else f"{prefix}_TLS_CERT"
        raise TLSConfigError(
            f"{present} is set but {missing} is not. TLS needs both; "
            "starting with one would serve plaintext under a configuration "
            "that reads as encrypted."
        )

    cert = _read(cert_path, setting=f"{prefix}_TLS_CERT")
    key = _read(key_path, setting=f"{prefix}_TLS_KEY")

    root: bytes | None = None
    if client_ca_path is not None:
        root = _read(client_ca_path, setting=f"{prefix}_TLS_CLIENT_CA")

    return ListenerTLS(
        certificates=[(cert, key)],
        cert_path=cert_path,
        key_path=key_path,
        verify_client=root is not None,
        root_certificates=root,
        client_ca_path=client_ca_path,
    )
