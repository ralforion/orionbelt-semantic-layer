"""Tests for ob_bigquery.connect()."""

from __future__ import annotations

from unittest.mock import patch

import ob_bigquery


def test_connect_names_the_client_in_the_user_agent() -> None:
    with patch("google.cloud.bigquery.Client") as client:
        ob_bigquery.connect(project="p", client_name="app/1.0")
        assert client.call_args.kwargs["client_info"].user_agent == "app/1.0"
        ob_bigquery.connect(project="p")
        assert "client_info" not in client.call_args.kwargs


def test_connect_with_an_access_token() -> None:
    """A federated user's Google access token, instead of a key file or ADC."""
    from google.oauth2.credentials import Credentials

    with patch("google.cloud.bigquery.Client") as client:
        ob_bigquery.connect(project="p", access_token="the-token", credentials_file=None)
        credentials = client.call_args.kwargs["credentials"]
        assert isinstance(credentials, Credentials)
        assert credentials.token == "the-token"
