"""Reading the Flight listener's TLS material off disk.

Separate from ``startup`` because the failures here are configuration
failures, and each one deserves to say what the operator got wrong rather
than surfacing as an ``OSError`` on a path that looks correct.

The most likely of those is not a typo. The published image runs as a
non-root user (``Dockerfile`` creates ``app`` and switches to it), so a
private key bind-mounted from the host as ``root:root 0600`` is present,
correctly named, and unreadable - which reads as a broken path until
someone thinks to check the mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("ob_flight.tls")


class FlightTLSConfigError(ValueError):
    """The TLS configuration cannot be used as given.

    Its own type so the API layer can refuse to start with the operator's
    message rather than a stack trace from deep inside pyarrow.
    """


@dataclass(frozen=True)
class FlightTLS:
    """Loaded TLS material, ready to hand to ``FlightServerBase``."""

    certificates: list[tuple[bytes, bytes]]
    verify_client: bool = False
    root_certificates: bytes | None = None

    @property
    def scheme(self) -> str:
        return "grpc+tls"


def _read(path: str, *, setting: str) -> bytes:
    """Read one PEM file, or explain which way it went wrong."""
    p = Path(path)
    if not p.exists():
        raise FlightTLSConfigError(f"{setting} points at {path}, which does not exist")
    if p.is_dir():
        raise FlightTLSConfigError(f"{setting} points at {path}, which is a directory")
    try:
        data = p.read_bytes()
    except PermissionError as exc:
        raise FlightTLSConfigError(
            f"{setting} at {path} cannot be read: {exc.strerror}. "
            "In the published image the server runs as a non-root user, so a "
            "file mounted as root-owned 0600 is unreadable to it - make it "
            "readable by the container's user (Kubernetes: defaultMode on the "
            "secret volume, or fsGroup)."
        ) from None
    if not data.strip():
        raise FlightTLSConfigError(f"{setting} at {path} is empty")
    if b"-----BEGIN" not in data:
        raise FlightTLSConfigError(
            f"{setting} at {path} does not look like PEM: no '-----BEGIN' header. "
            "DER and PKCS#12 are not read here; convert to PEM first."
        )
    return data


def load_flight_tls(
    cert_path: str | None,
    key_path: str | None,
    client_ca_path: str | None = None,
) -> FlightTLS | None:
    """Build the TLS config, or ``None`` when TLS is not configured.

    Both the certificate and the key, or neither. Half a pair is refused
    rather than ignored: a deployment that set one and got plaintext would
    believe it was encrypted, which is worse than not starting.
    """
    if cert_path is None and key_path is None:
        if client_ca_path is not None:
            raise FlightTLSConfigError(
                "FLIGHT_TLS_CLIENT_CA is set without FLIGHT_TLS_CERT and "
                "FLIGHT_TLS_KEY. Mutual TLS needs the server's own certificate "
                "as well as the CA its clients are checked against."
            )
        return None

    if cert_path is None or key_path is None:
        missing = "FLIGHT_TLS_CERT" if cert_path is None else "FLIGHT_TLS_KEY"
        present = "FLIGHT_TLS_KEY" if cert_path is None else "FLIGHT_TLS_CERT"
        raise FlightTLSConfigError(
            f"{present} is set but {missing} is not. Flight TLS needs both; "
            "starting with one would serve plaintext under a configuration "
            "that reads as encrypted."
        )

    cert = _read(cert_path, setting="FLIGHT_TLS_CERT")
    key = _read(key_path, setting="FLIGHT_TLS_KEY")

    root: bytes | None = None
    if client_ca_path is not None:
        root = _read(client_ca_path, setting="FLIGHT_TLS_CLIENT_CA")

    return FlightTLS(
        certificates=[(cert, key)],
        verify_client=root is not None,
        root_certificates=root,
    )
