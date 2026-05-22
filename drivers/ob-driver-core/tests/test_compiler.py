"""Unit tests for the compilation bridge."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from ob_driver_core.compiler import compile_obml
from ob_driver_core.exceptions import OperationalError, ProgrammingError


SAMPLE_OBML = {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}}


def test_compile_success() -> None:
    """Successful REST call returns SQL."""
    mock_response = MagicMock()
    mock_response.is_success = True
    mock_response.json.return_value = {
        "sql": "SELECT region, SUM(amount) FROM orders GROUP BY region"
    }

    with patch("httpx.post", return_value=mock_response) as mock_post:
        sql = compile_obml(SAMPLE_OBML, dialect="duckdb")
        assert sql == "SELECT region, SUM(amount) FROM orders GROUP BY region"
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["params"] == {"dialect": "duckdb"}
        assert "/v1/query/sql" in call_kwargs.args[0]


def test_compile_connect_error_raises_operational() -> None:
    """Connection failure to REST API raises OperationalError."""
    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(OperationalError, match="unavailable"):
            compile_obml(SAMPLE_OBML, dialect="duckdb")


def test_compile_4xx_raises_programming() -> None:
    """4xx response from REST API raises ProgrammingError."""
    mock_response = MagicMock()
    mock_response.is_success = False
    mock_response.status_code = 400
    mock_response.json.return_value = {"detail": "bad query"}
    mock_response.text = "bad query"

    with patch("httpx.post", return_value=mock_response):
        with pytest.raises(ProgrammingError, match="bad query"):
            compile_obml(SAMPLE_OBML, dialect="duckdb")


def test_compile_5xx_raises_operational() -> None:
    """5xx response from REST API raises OperationalError."""
    mock_response = MagicMock()
    mock_response.is_success = False
    mock_response.status_code = 500
    mock_response.json.return_value = {"detail": "internal"}
    mock_response.text = "internal"

    with patch("httpx.post", return_value=mock_response):
        with pytest.raises(OperationalError, match="internal"):
            compile_obml(SAMPLE_OBML, dialect="duckdb")


def test_compile_custom_api_url() -> None:
    """Custom ob_api_url is used in the request."""
    mock_response = MagicMock()
    mock_response.is_success = True
    mock_response.json.return_value = {"sql": "SELECT 1"}

    with patch("httpx.post", return_value=mock_response) as mock_post:
        compile_obml(SAMPLE_OBML, dialect="postgres", ob_api_url="http://my-api:9000")
        url = mock_post.call_args.args[0]
        assert url == "http://my-api:9000/v1/query/sql"


def test_compile_trailing_slash_stripped() -> None:
    """Trailing slash in ob_api_url is stripped."""
    mock_response = MagicMock()
    mock_response.is_success = True
    mock_response.json.return_value = {"sql": "SELECT 1"}

    with patch("httpx.post", return_value=mock_response) as mock_post:
        compile_obml(SAMPLE_OBML, dialect="duckdb", ob_api_url="http://localhost:8000/")
        url = mock_post.call_args.args[0]
        assert url == "http://localhost:8000/v1/query/sql"
