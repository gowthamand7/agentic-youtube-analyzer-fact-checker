"""Unit tests for the live verification harness (Phase 0)."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from harness_check import (
    check_clickhouse_connection,
    check_docker_daemon,
    check_environment_variables,
    check_go_installed,
    check_python_version,
    check_tavily_api,
    format_summary_table,
    load_env_file,
    run_all_checks,
)


def test_load_env_file_parses_content(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Comment line\n"
        "ANTHROPIC_API_KEY=sk-ant-test123\n"
        'TAVILY_API_KEY="tvly-secret"\n'
        "EMPTY_VAL=\n"
        "CLICKHOUSE_PORT=9000\n",
        encoding="utf-8",
    )
    result = load_env_file(str(env_file))
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-test123"
    assert result["TAVILY_API_KEY"] == "tvly-secret"
    assert result["EMPTY_VAL"] == ""
    assert result["CLICKHOUSE_PORT"] == "9000"


def test_load_env_file_missing_returns_empty():
    result = load_env_file("/non/existent/path/.env")
    assert result == {}


def test_check_environment_variables_all_present():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-real-key-123",
        "TAVILY_API_KEY": "tvly-real-key-123",
        "YOUTUBE_API_KEY": "AIzaSy-test",
        "CLICKHOUSE_HOST": "localhost",
        "CLICKHOUSE_PORT": "9000",
        "CLICKHOUSE_HTTP_PORT": "8123",
        "CLICKHOUSE_DB": "youtube_agent",
        "CLICKHOUSE_USER": "admin",
        "CLICKHOUSE_PASSWORD": "secret",
    }
    status = check_environment_variables(env)
    assert status["ok"] is True
    assert status["missing_required"] == []
    assert status["keys"]["ANTHROPIC_API_KEY"]["status"] == "SET"
    assert status["keys"]["TAVILY_API_KEY"]["status"] == "SET"


def test_check_environment_variables_missing_required_or_placeholder():
    env = {
        "ANTHROPIC_API_KEY": "your_anthropic_api_key_here",  # placeholder
        "TAVILY_API_KEY": "",  # empty
    }
    status = check_environment_variables(env)
    assert status["ok"] is False
    assert "ANTHROPIC_API_KEY" in status["missing_required"]
    assert "TAVILY_API_KEY" in status["missing_required"]


def test_check_python_version():
    status = check_python_version()
    assert status["ok"] is True
    assert "version" in status
    assert (sys.version_info.major, sys.version_info.minor) >= (3, 10)


def test_check_go_installed():
    with patch("shutil.which", return_value="/usr/local/go/bin/go"), patch(
        "subprocess.run"
    ) as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="go version go1.25.2 windows/amd64\n")
        status = check_go_installed()
        assert status["ok"] is True
        assert "go1.25.2" in status["version"]


def test_check_go_missing():
    with patch("shutil.which", return_value=None):
        status = check_go_installed()
        assert status["ok"] is False
        assert "remediation" in status


def test_check_docker_daemon_running():
    with patch("shutil.which", return_value="docker"), patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="Server Version: 28.4.0\n", stderr="")
        status = check_docker_daemon()
        assert status["ok"] is True
        assert "running" in status["message"].lower()


def test_check_docker_daemon_stopped():
    with patch("shutil.which", return_value="docker"), patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error during connect: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.",
        )
        status = check_docker_daemon()
        assert status["ok"] is False
        assert "remediation" in status


def test_check_docker_cli_missing():
    with patch("shutil.which", return_value=None):
        status = check_docker_daemon()
        assert status["ok"] is False
        assert "Docker CLI is not installed" in status["message"]


def test_check_clickhouse_connection_success():
    with patch("socket.create_connection") as mock_sock, patch("urllib.request.urlopen") as mock_url:
        mock_sock.return_value = MagicMock()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"Ok.\n"
        mock_resp.status = 200
        mock_url.return_value.__enter__.return_value = mock_resp

        status = check_clickhouse_connection(host="localhost", tcp_port=9000, http_port=8123)
        assert status["ok"] is True
        assert status["tcp_ok"] is True
        assert status["http_ok"] is True


def test_check_clickhouse_connection_refused():
    with patch("socket.create_connection", side_effect=OSError("Connection refused")), patch(
        "urllib.request.urlopen", side_effect=OSError("Connection refused")
    ):
        status = check_clickhouse_connection(host="localhost", tcp_port=9000, http_port=8123)
        assert status["ok"] is False
        assert status["tcp_ok"] is False
        assert status["http_ok"] is False
        assert "remediation" in status


def test_check_tavily_api_missing_or_placeholder():
    status = check_tavily_api(api_key=None)
    assert status["ok"] is False
    assert status.get("skipped", False) is True

    status_placeholder = check_tavily_api(api_key="tvly-your_tavily_key_here")
    assert status_placeholder["ok"] is False
    assert status_placeholder.get("skipped", False) is True


def test_check_tavily_api_success():
    with patch("urllib.request.urlopen") as mock_url:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"results": []}'
        mock_resp.status = 200
        mock_url.return_value.__enter__.return_value = mock_resp

        status = check_tavily_api(api_key="tvly-valid-test-key")
        assert status["ok"] is True
        assert "healthy" in status["message"].lower()


def test_check_tavily_api_error_handled_cleanly():
    import urllib.error

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            url="https://api.tavily.com/search",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"detail":"Invalid API Key"}'),
        ),
    ):
        status = check_tavily_api(api_key="tvly-invalid-key")
        assert status["ok"] is False
        assert "401" in status["message"] or "Unauthorized" in status["message"] or "Invalid" in status["message"]


def test_run_all_checks_and_format_summary(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=sk-ant-test\nTAVILY_API_KEY=tvly-test\n",
        encoding="utf-8",
    )
    with patch("subprocess.run") as mock_run, patch("socket.create_connection"), patch(
        "urllib.request.urlopen"
    ) as mock_url:
        mock_run.return_value = MagicMock(returncode=0, stdout="go version go1.25.2\n")
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"Ok.\n"
        mock_resp.status = 200
        mock_url.return_value.__enter__.return_value = mock_resp

        results = run_all_checks(env_path=str(env_file))
        assert "environment" in results
        assert "python" in results
        assert "go" in results
        assert "docker" in results
        assert "clickhouse" in results
        assert "tavily" in results

        table_output = format_summary_table(results)
        assert "Verification Harness" in table_output
        assert "ClickHouse" in table_output
