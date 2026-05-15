"""Tests for Flight server startup/shutdown."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import ob_flight.startup as startup_module
from ob_flight.startup import start_flight_background, stop_flight_server


class TestStartFlightBackground:
    def test_starts_daemon_thread(self, monkeypatch):
        monkeypatch.setenv("FLIGHT_PORT", "9999")
        monkeypatch.setenv("DB_VENDOR", "postgres")

        mock_server = MagicMock()
        with patch("ob_flight.server.OBFlightServer", return_value=mock_server):
            with patch("ob_flight.auth.create_auth_handler", return_value=MagicMock()):
                thread = start_flight_background(session_manager=MagicMock())
                assert thread.daemon is True
                assert thread.name == "ob-flight-server"

    def test_custom_port(self):
        mock_server = MagicMock()
        with patch("ob_flight.server.OBFlightServer", return_value=mock_server) as mock_cls:
            with patch("ob_flight.auth.create_auth_handler", return_value=MagicMock()):
                start_flight_background(session_manager=MagicMock(), port=12345)
                call_args = mock_cls.call_args
                assert "grpc://0.0.0.0:12345" in call_args.args

    def test_custom_auth_handler(self):
        mock_server = MagicMock()
        custom_auth = MagicMock()
        with patch("ob_flight.server.OBFlightServer", return_value=mock_server) as mock_cls:
            start_flight_background(session_manager=MagicMock(), auth_handler=custom_auth)
            call_kwargs = mock_cls.call_args.kwargs
            assert call_kwargs["auth_handler"] is custom_auth

    def test_default_port_from_env(self, monkeypatch):
        monkeypatch.setenv("FLIGHT_PORT", "7777")
        mock_server = MagicMock()
        with patch("ob_flight.server.OBFlightServer", return_value=mock_server) as mock_cls:
            with patch("ob_flight.auth.create_auth_handler", return_value=MagicMock()):
                start_flight_background(session_manager=MagicMock())
                call_args = mock_cls.call_args
                assert "grpc://0.0.0.0:7777" in call_args.args

    def test_default_dialect_from_env(self, monkeypatch):
        monkeypatch.setenv("DB_VENDOR", "snowflake")
        mock_server = MagicMock()
        with patch("ob_flight.server.OBFlightServer", return_value=mock_server) as mock_cls:
            with patch("ob_flight.auth.create_auth_handler", return_value=MagicMock()):
                start_flight_background(session_manager=MagicMock())
                call_kwargs = mock_cls.call_args.kwargs
                assert call_kwargs["default_dialect"] == "snowflake"

    def test_session_manager_passed_through(self):
        mock_server = MagicMock()
        mock_mgr = MagicMock()
        with patch("ob_flight.server.OBFlightServer", return_value=mock_server) as mock_cls:
            with patch("ob_flight.auth.create_auth_handler", return_value=MagicMock()):
                start_flight_background(session_manager=mock_mgr)
                call_kwargs = mock_cls.call_args.kwargs
                assert call_kwargs["session_manager"] is mock_mgr


class TestStopFlightServer:
    def test_stop_calls_shutdown(self):
        mock_server = MagicMock()
        startup_module._server = mock_server
        startup_module._thread = MagicMock()

        stop_flight_server()
        mock_server.shutdown.assert_called_once()
        assert startup_module._server is None
        assert startup_module._thread is None

    def test_stop_when_not_running(self):
        startup_module._server = None
        startup_module._thread = None
        stop_flight_server()  # should not raise

    def test_stop_handles_shutdown_error(self):
        mock_server = MagicMock()
        mock_server.shutdown.side_effect = RuntimeError("already stopped")
        startup_module._server = mock_server
        startup_module._thread = MagicMock()

        stop_flight_server()  # should not raise
        assert startup_module._server is None
        assert startup_module._thread is None
