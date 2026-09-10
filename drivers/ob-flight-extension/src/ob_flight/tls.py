"""Flight's view of the shared listener TLS loader.

The loader itself lives in ``orionbelt.service.tls`` because pgwire needs it
too, and pgwire is core while this package is an optional extra - core cannot
depend on it. This module keeps the Flight-facing names and pins the prefix,
so a misconfiguration here says ``FLIGHT_TLS_KEY`` rather than the other
surface's setting.
"""

from __future__ import annotations

from orionbelt.service.tls import ListenerTLS, TLSConfigError, load_listener_tls

#: Kept as the Flight-facing name; the type is shared with pgwire.
FlightTLS = ListenerTLS
FlightTLSConfigError = TLSConfigError


def load_flight_tls(
    cert_path: str | None,
    key_path: str | None,
    client_ca_path: str | None = None,
) -> ListenerTLS | None:
    """Load the Flight listener's TLS material, or ``None`` when unconfigured."""
    return load_listener_tls(cert_path, key_path, client_ca_path, prefix="FLIGHT")


__all__ = ["FlightTLS", "FlightTLSConfigError", "load_flight_tls"]
