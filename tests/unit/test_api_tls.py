"""TLS on the REST listener.

The two wire surfaces gained TLS in 2.28.0 and REST did not, which left it as
the only surface that could not encrypt itself. The reasoning was presumably
that REST is always behind something that terminates TLS - but that applies to
pgwire too, and pgwire got it anyway, precisely because there is not always a
proxy.

What is asserted here is the *wiring*: that the settings reach uvicorn in the
form it expects, and that a misconfiguration stops startup rather than serving
plaintext under a configuration that reads as encrypted. The handshake itself is
``ssl``'s and is already covered where the shared loader is tested.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import pytest

from orionbelt.api.app import _rest_tls_kwargs
from orionbelt.service.tls import TLSConfigError


class _Settings:
    """Only the attributes ``_rest_tls_kwargs`` reads."""

    def __init__(self, cert: str | None, key: str | None, client_ca: str | None = None) -> None:
        self.api_tls_cert = cert
        self.api_tls_key = key
        self.api_tls_client_ca = client_ca
        self.api_server_host = "127.0.0.1"
        self.effective_port = 8000


@pytest.fixture
def pki(tmp_path: Path) -> dict[str, str]:
    """A CA, and a server certificate signed by it."""
    pytest.importorskip("cryptography", reason="cryptography required to mint test certs")
    import datetime as dt

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
        .sign(ca_key, hashes.SHA256())
    )
    paths = {
        "ca": tmp_path / "ca.crt",
        "cert": tmp_path / "server.crt",
        "key": tmp_path / "server.key",
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
    return {k: str(v) for k, v in paths.items()}


class TestUnconfigured:
    def test_no_settings_means_no_uvicorn_arguments(self) -> None:
        """Plaintext stays the default, so every existing deployment is unchanged."""
        assert _rest_tls_kwargs(_Settings(None, None)) == {}


class TestConfigured:
    def test_the_certificate_and_key_reach_uvicorn_as_paths(self, pki: dict[str, str]) -> None:
        """uvicorn takes paths, not PEM bytes - which is why the loader keeps both."""
        kwargs = _rest_tls_kwargs(_Settings(pki["cert"], pki["key"]))
        assert kwargs == {"ssl_certfile": pki["cert"], "ssl_keyfile": pki["key"]}

    def test_a_client_ca_turns_on_mutual_tls(self, pki: dict[str, str]) -> None:
        """The point of the setting: without CERT_REQUIRED uvicorn would accept
        any client, and a CA that is loaded but not enforced is worse than none,
        because the configuration reads as mutual TLS."""
        kwargs = _rest_tls_kwargs(_Settings(pki["cert"], pki["key"], pki["ca"]))
        assert kwargs["ssl_ca_certs"] == pki["ca"]
        assert kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED

    def test_the_material_actually_loads(self, pki: dict[str, str]) -> None:
        """Paths that uvicorn will hand to ``ssl``, proven usable here rather
        than at bind time in production."""
        kwargs = _rest_tls_kwargs(_Settings(pki["cert"], pki["key"], pki["ca"]))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(kwargs["ssl_certfile"], kwargs["ssl_keyfile"])
        ctx.load_verify_locations(cafile=kwargs["ssl_ca_certs"])


class TestMisconfiguredStopsStartup:
    """Half a configuration must not serve plaintext.

    A deployment that set one of the pair and got HTTP would believe it was
    encrypted. Refusing to start is the louder and safer failure, and is what
    both wire surfaces already do.
    """

    def test_a_certificate_without_a_key(self, pki: dict[str, str]) -> None:
        with pytest.raises(RuntimeError, match="API_TLS_KEY"):
            _rest_tls_kwargs(_Settings(pki["cert"], None))

    def test_a_key_without_a_certificate(self, pki: dict[str, str]) -> None:
        with pytest.raises(RuntimeError, match="API_TLS_CERT"):
            _rest_tls_kwargs(_Settings(None, pki["key"]))

    def test_a_client_ca_alone(self, pki: dict[str, str]) -> None:
        """Mutual TLS needs the server's own certificate as well."""
        with pytest.raises(RuntimeError, match="API_TLS_CLIENT_CA"):
            _rest_tls_kwargs(_Settings(None, None, pki["ca"]))

    def test_a_missing_file_names_the_setting(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nope.crt")
        with pytest.raises(RuntimeError, match="API_TLS_CERT"):
            _rest_tls_kwargs(_Settings(missing, missing))

    def test_the_error_names_this_surface_not_another(self, pki: dict[str, str]) -> None:
        """``prefix='API'`` matters: an error naming FLIGHT_TLS_KEY on the REST
        listener would send an operator to the wrong setting entirely."""
        with pytest.raises(RuntimeError) as exc:
            _rest_tls_kwargs(_Settings(pki["cert"], None))
        assert "FLIGHT" not in str(exc.value)
        assert "PGWIRE" not in str(exc.value)


def test_the_loader_is_shared_with_the_wire_surfaces() -> None:
    """Not a reimplementation: the same loader, so the same error messages and
    the same refusal to start on half a pair."""
    import inspect

    from orionbelt.api import app

    assert "load_listener_tls" in inspect.getsource(app._rest_tls_kwargs)
    assert issubclass(TLSConfigError, ValueError)
