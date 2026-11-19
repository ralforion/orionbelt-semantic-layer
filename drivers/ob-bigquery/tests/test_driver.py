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
