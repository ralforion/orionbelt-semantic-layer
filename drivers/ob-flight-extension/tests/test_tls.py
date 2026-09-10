"""Loading the Flight listener's TLS material.

Every case here is a configuration mistake an operator can actually make,
and the assertion is on what they are told - a message naming the wrong
setting is worth more than an exception type, because the failure looks
identical to a typo from the outside.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ob_flight.tls import FlightTLSConfigError, load_flight_tls

_PEM = b"-----BEGIN CERTIFICATE-----\nnot a real certificate\n-----END CERTIFICATE-----\n"


@pytest.fixture
def pem_pair(tmp_path: Path) -> tuple[str, str]:
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_bytes(_PEM)
    key.write_bytes(_PEM.replace(b"CERTIFICATE", b"PRIVATE KEY"))
    return str(cert), str(key)


class TestNotConfigured:
    def test_neither_means_plaintext(self) -> None:
        assert load_flight_tls(None, None) is None

    def test_a_client_ca_alone_is_refused(self) -> None:
        """Mutual TLS still needs the server's own certificate."""
        with pytest.raises(FlightTLSConfigError, match="without FLIGHT_TLS_CERT"):
            load_flight_tls(None, None, "/tmp/ca.crt")


class TestBothOrNeither:
    """Half a pair is refused rather than ignored.

    Starting plaintext under a configuration that reads as encrypted is the
    outcome worth preventing: nothing fails, and the operator believes the
    wire is protected.
    """

    def test_cert_without_key(self, pem_pair: tuple[str, str]) -> None:
        cert, _ = pem_pair
        with pytest.raises(FlightTLSConfigError, match="FLIGHT_TLS_KEY is not"):
            load_flight_tls(cert, None)

    def test_key_without_cert(self, pem_pair: tuple[str, str]) -> None:
        _, key = pem_pair
        with pytest.raises(FlightTLSConfigError, match="FLIGHT_TLS_CERT is not"):
            load_flight_tls(None, key)


class TestWhatTheOperatorIsTold:
    def test_a_missing_file_names_the_setting_and_the_path(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "absent.crt")
        with pytest.raises(FlightTLSConfigError) as excinfo:
            load_flight_tls(missing, missing)
        assert "FLIGHT_TLS_CERT" in str(excinfo.value)
        assert missing in str(excinfo.value)

    def test_a_directory_is_named_as_such(self, tmp_path: Path) -> None:
        with pytest.raises(FlightTLSConfigError, match="is a directory"):
            load_flight_tls(str(tmp_path), str(tmp_path))

    def test_an_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.crt"
        empty.write_bytes(b"")
        with pytest.raises(FlightTLSConfigError, match="is empty"):
            load_flight_tls(str(empty), str(empty))

    def test_a_der_file_says_convert_to_pem(self, tmp_path: Path) -> None:
        """DER and PKCS#12 are the formats people already have to hand."""
        der = tmp_path / "server.der"
        der.write_bytes(b"\x30\x82\x01\x0a\x02\x82\x01\x01")
        with pytest.raises(FlightTLSConfigError, match="does not look like PEM"):
            load_flight_tls(str(der), str(der))

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
    def test_an_unreadable_key_explains_the_non_root_user(self, pem_pair: tuple[str, str]) -> None:
        """The failure the published image will actually produce.

        The image runs as a non-root user, so a key bind-mounted from the
        host as root-owned 0600 is present, correctly named, and unreadable.
        Without this message it reads as a wrong path.
        """
        cert, key = pem_pair
        Path(key).chmod(0o000)
        try:
            with pytest.raises(FlightTLSConfigError) as excinfo:
                load_flight_tls(cert, key)
        finally:
            Path(key).chmod(0o600)
        message = str(excinfo.value)
        assert "FLIGHT_TLS_KEY" in message
        assert "non-root" in message
        assert "fsGroup" in message, "the Kubernetes fix belongs in the message"


class TestLoaded:
    def test_a_pair_loads(self, pem_pair: tuple[str, str]) -> None:
        cert, key = pem_pair
        tls = load_flight_tls(cert, key)
        assert tls is not None
        assert tls.certificates == [(_PEM, _PEM.replace(b"CERTIFICATE", b"PRIVATE KEY"))]
        assert tls.verify_client is False
        assert tls.root_certificates is None
        assert tls.scheme == "grpc+tls"

    def test_a_client_ca_turns_on_mutual_tls(self, pem_pair: tuple[str, str]) -> None:
        cert, key = pem_pair
        tls = load_flight_tls(cert, key, cert)
        assert tls is not None
        assert tls.verify_client is True
        assert tls.root_certificates == _PEM
