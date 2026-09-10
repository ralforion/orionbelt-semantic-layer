"""TLS on the pgwire surface, driven by a real Postgres client.

Postgres does not start encrypted. The client sends an ``SSLRequest``, the
server answers ``S`` or ``N``, and only then is the socket upgraded — so the
thing under test is a negotiation, not a listener, and only a real libpq
client exercises it the way a BI tool does.

The certificate material is read by the same loader the Flight surface uses
(``orionbelt.service.tls``); what differs is what each does with it, since
pyarrow wants PEM bytes and ``ssl`` wants paths.

See ``design/PLAN_flight_tls.md`` open question 4.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from orionbelt.pgwire.server import PgWireServer
from orionbelt.service.tls import load_listener_tls


def _pki(out: Path) -> dict[str, str]:
    """A CA, and a server certificate for localhost signed by it.

    A self-signed certificate would test three of the four cases but not
    ``verify-ca``, which is the mode an operator actually deploys.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = dt.datetime.now(dt.UTC)

    def name(cn: str) -> Any:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name("OBSL Test CA"))
        .issuer_name(name("OBSL Test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv = (
        x509.CertificateBuilder()
        .subject_name(name("localhost"))
        .issuer_name(ca.subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # An unrelated CA, so "wrong trust anchor" is distinguishable from "no
    # trust anchor" — they fail differently and only one is interesting.
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = (
        x509.CertificateBuilder()
        .subject_name(name("Unrelated CA"))
        .issuer_name(name("Unrelated CA"))
        .public_key(other_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(other_key, hashes.SHA256())
    )

    paths = {
        "ca": out / "ca.crt",
        "cert": out / "server.crt",
        "key": out / "server.key",
        "other_ca": out / "other.crt",
    }
    paths["ca"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths["cert"].write_bytes(srv.public_bytes(serialization.Encoding.PEM))
    paths["key"].write_bytes(
        srv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    paths["other_ca"].write_bytes(other.public_bytes(serialization.Encoding.PEM))
    return {k: str(v) for k, v in paths.items()}


@pytest.fixture(scope="module")
def tls_pgwire(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[int, dict[str, str]]]:
    """A TLS-enabled pgwire listener, on its own event-loop thread.

    The loop must not be shared with the tests: psycopg is blocking, so a
    client call on the loop's thread starves the accept and every connection
    times out.
    """
    pytest.importorskip("cryptography", reason="cryptography required to mint test certs")

    pki = _pki(tmp_path_factory.mktemp("pgwire-tls"))
    tls = load_listener_tls(pki["cert"], pki["key"], prefix="PGWIRE")
    server = PgWireServer(host="127.0.0.1", port=0, auth_mode="trust", tls=tls)

    ready = threading.Event()
    bound: dict[str, int] = {}
    loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(loop)
        bound["port"] = loop.run_until_complete(server.start())
        ready.set()
        # ``run_forever``, not ``serve_forever``: ``start()`` is already
        # accepting connections, and stopping the loop out from under
        # ``serve_forever`` leaves its future pending, which pytest reports as
        # an unraisable thread exception against an unrelated test.
        loop.run_forever()

    thread = threading.Thread(target=run, name="pgwire-tls-test", daemon=True)
    thread.start()
    assert ready.wait(30), "pgwire TLS listener did not start"

    try:
        yield bound["port"], pki
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def _encrypted(port: int, **kwargs: Any) -> bool:
    """Connect and report whether libpq negotiated TLS."""
    with psycopg.connect(
        host="localhost", port=port, user="obsl", dbname="x", connect_timeout=10, **kwargs
    ) as conn:
        pg = getattr(conn, "pgconn", None)
        return bool(getattr(pg, "ssl_in_use", False))


class TestSSLRequestNegotiation:
    """The four `sslmode` values a BI tool actually sends."""

    def test_disable_still_gets_plaintext(self, tls_pgwire: tuple[int, dict[str, str]]) -> None:
        """Turning TLS on does not lock out clients that do not ask for it.

        Postgres semantics: the server offers, the client decides. Refusing
        plaintext outright is a separate policy and not what this setting
        means.
        """
        port, _ = tls_pgwire
        assert _encrypted(port, sslmode="disable") is False

    def test_require_encrypts(self, tls_pgwire: tuple[int, dict[str, str]]) -> None:
        port, _ = tls_pgwire
        assert _encrypted(port, sslmode="require") is True

    def test_verify_ca_encrypts(self, tls_pgwire: tuple[int, dict[str, str]]) -> None:
        port, pki = tls_pgwire
        assert _encrypted(port, sslmode="verify-ca", sslrootcert=pki["ca"]) is True

    def test_verify_full_checks_the_hostname_too(
        self, tls_pgwire: tuple[int, dict[str, str]]
    ) -> None:
        """The certificate's CN is localhost, which is what we connect as."""
        port, pki = tls_pgwire
        assert _encrypted(port, sslmode="verify-full", sslrootcert=pki["ca"]) is True

    def test_the_wrong_trust_anchor_is_refused(
        self, tls_pgwire: tuple[int, dict[str, str]]
    ) -> None:
        """The assertion that makes the others mean something: verification
        is real, not a mode that accepts whatever it is given."""
        port, pki = tls_pgwire
        with pytest.raises(psycopg.OperationalError, match="(?i)certificate|ssl"):
            _encrypted(port, sslmode="verify-ca", sslrootcert=pki["other_ca"])


class TestWithoutTLS:
    def test_a_plaintext_listener_answers_n(self) -> None:
        """Unconfigured, the server declines the upgrade and carries on -
        which is what every deployment before this change did."""
        server = PgWireServer(host="127.0.0.1", port=0, auth_mode="trust")
        ready = threading.Event()
        bound: dict[str, int] = {}
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)
            bound["port"] = loop.run_until_complete(server.start())
            ready.set()
            loop.run_forever()  # see the module fixture

        thread = threading.Thread(target=run, name="pgwire-plain-test", daemon=True)
        thread.start()
        assert ready.wait(30)
        try:
            # ``prefer`` asks for TLS and accepts a refusal; that is the
            # negotiation working, not failing.
            assert _encrypted(bound["port"], sslmode="prefer") is False
        finally:
            asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
