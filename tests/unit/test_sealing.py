"""AES-256-GCM sealing of a BI token's warehouse sign-in, and ``TOKEN_STORE_KEY``."""

from __future__ import annotations

import base64
import secrets

import pytest

from orionbelt.auth import AuthConfigError
from orionbelt.auth.sealing import Sealer, key_id, parse_keys


def _key() -> bytes:
    return secrets.token_bytes(32)


class TestSealer:
    def test_a_value_opens_again_for_its_own_row(self) -> None:
        sealer = Sealer([_key()])
        sealed, key = sealer.seal("refresh-token", bound_to="3f2a9c41b7e0")
        assert b"refresh-token" not in sealed
        assert key == sealer.current_key
        assert sealer.open(sealed, key, bound_to="3f2a9c41b7e0") == "refresh-token"

    def test_it_does_not_open_for_another_row(self) -> None:
        sealer = Sealer([_key()])
        sealed, key = sealer.seal("refresh-token", bound_to="3f2a9c41b7e0")
        assert sealer.open(sealed, key, bound_to="000000000000") is None

    def test_a_changed_byte_does_not_open(self) -> None:
        sealer = Sealer([_key()])
        sealed, key = sealer.seal("refresh-token", bound_to="row")
        tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
        assert sealer.open(tampered, key, bound_to="row") is None

    def test_a_key_no_longer_configured_opens_nothing(self) -> None:
        sealed, key = Sealer([_key()]).seal("refresh-token", bound_to="row")
        assert Sealer([_key()]).open(sealed, key, bound_to="row") is None

    def test_rotation_seals_with_the_first_key_and_opens_with_each(self) -> None:
        old, new = _key(), _key()
        sealed, key = Sealer([old]).seal("refresh-token", bound_to="row")
        rotated = Sealer([new, old])
        assert rotated.current_key == key_id(new)
        assert rotated.open(sealed, key, bound_to="row") == "refresh-token"
        _, resealed_with = rotated.seal("refresh-token", bound_to="row")
        assert resealed_with == key_id(new)

    def test_its_repr_shows_no_key(self) -> None:
        key = _key()
        assert repr(Sealer([key])) == "Sealer(keys=1)"


class TestParseKeys:
    def test_standard_and_url_safe_base64_separated_by_commas_or_spaces(self) -> None:
        first, second = _key(), _key()
        raw = (
            f"{base64.b64encode(first).decode()}, "
            f"{base64.urlsafe_b64encode(second).decode().rstrip('=')}\n"
        )
        assert parse_keys(raw) == (first, second)

    def test_empty_is_no_key(self) -> None:
        assert parse_keys("  ") == ()

    @pytest.mark.parametrize("entry", [base64.b64encode(b"x" * 16).decode(), "not-base64!", "QUJD"])
    def test_a_wrong_key_is_named_by_its_place_not_its_value(self, entry: str) -> None:
        good = base64.b64encode(_key()).decode()
        with pytest.raises(AuthConfigError, match="TOKEN_STORE_KEY entry 2 ") as raised:
            parse_keys(f"{good},{entry}")
        assert entry not in str(raised.value)
        assert good not in str(raised.value)
