#!/usr/bin/env python3
"""Live Environment & Verification Harness (Phase 0).

Inspects host environment prerequisites:
- Python and Go versions
- Environment keys in .env and process env
- Docker daemon responsiveness
- ClickHouse native TCP (9000) and HTTP (8123) ports
- Live Tavily Search API connectivity (when key is configured)
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

PLACEHOLDER_SUBSTRINGS = (
    "your_",
    "your-",
    "todo",
    "replace_me",
    "xxx",
    "<your",
)


def is_placeholder_value(val: str) -> bool:
    """Check if value is empty or obvious placeholder."""
    cleaned = val.strip().lower()
    if not cleaned:
        return True
    return any(p in cleaned for p in PLACEHOLDER_SUBSTRINGS)


def load_env_file(filepath: Optional[str] = None) -> Dict[str, str]:
    """Parse a simple .env file into a dictionary without third-party dependencies."""
    if filepath is None:
        filepath = str(Path(__file__).resolve().parent.parent / ".env")

    path = Path(filepath)
    if not path.is_file():
        return {}

    env_vars: Dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return {}

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            val = val[1:-1]
        env_vars[key] = val

    return env_vars


def get_merged_env(filepath: Optional[str] = None) -> Dict[str, str]:
    """Merge environment from .env file and os.environ."""
    merged = load_env_file(filepath)
    # Process environment takes precedence if set
    for k, v in os.environ.items():
        if v:
            merged[k] = v
    return merged


def check_environment_variables(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Check required and optional environment keys."""
    if env is None:
        env = get_merged_env()

    required_keys = ["ANTHROPIC_API_KEY", "TAVILY_API_KEY"]
    optional_keys = [
        "YOUTUBE_API_KEY",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_HTTP_PORT",
        "CLICKHOUSE_DB",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
    ]

    missing_required: List[str] = []
    keys_report: Dict[str, Dict[str, Any]] = {}

    for key in required_keys:
        val = env.get(key, "")
        if not val or is_placeholder_value(val):
            keys_report[key] = {
                "status": "MISSING",
                "required": True,
                "preview": "Not set or placeholder",
            }
            missing_required.append(key)
        else:
            preview = f"{val[:4]}...{val[-4:]}" if len(val) > 8 else "***"
            keys_report[key] = {
                "status": "SET",
                "required": True,
                "preview": preview,
            }

    for key in optional_keys:
        val = env.get(key, "")
        if not val or is_placeholder_value(val):
            keys_report[key] = {
                "status": "UNSET",
                "required": False,
                "preview": "Optional",
            }
        else:
            preview = f"{val[:3]}...{val[-3:]}" if len(val) > 6 else val
            keys_report[key] = {
                "status": "SET",
                "required": False,
                "preview": preview,
            }

    return {
        "ok": len(missing_required) == 0,
        "keys": keys_report,
        "missing_required": missing_required,
    }


def check_python_version() -> Dict[str, Any]:
    """Verify Python runtime meets minimum requirements (>= 3.10)."""
    major, minor, micro = sys.version_info.major, sys.version_info.minor, sys.version_info.micro
    version_str = f"{major}.{minor}.{micro}"
    is_ok = (major, minor) >= (3, 10)
    return {
        "ok": is_ok,
        "version": version_str,
        "message": f"Python {version_str} (>= 3.10 required)" if is_ok else f"Python {version_str} is unsupported (< 3.10)",
        "remediation": "Install Python 3.11+ or 3.14 via uv / pyenv" if not is_ok else "",
    }


def check_go_installed() -> Dict[str, Any]:
    """Verify Go toolchain availability."""
    go_path = shutil.which("go")
    if not go_path:
        return {
            "ok": False,
            "version": "Not found",
            "message": "Go executable not found in PATH",
            "remediation": "Install Go 1.22+ from https://go.dev/dl/ and add to PATH",
        }

    try:
        res = subprocess.run(["go", "version"], capture_output=True, text=True, timeout=5)
        out = res.stdout.strip()
        return {
            "ok": res.returncode == 0,
            "version": out,
            "message": out if res.returncode == 0 else "Failed to run 'go version'",
            "remediation": "Ensure Go is properly installed and executable" if res.returncode != 0 else "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "version": "Error",
            "message": f"Go check error: {exc}",
            "remediation": "Check Go toolchain permissions and PATH",
        }


def check_docker_daemon() -> Dict[str, Any]:
    """Verify Docker daemon is reachable and responding."""
    docker_path = shutil.which("docker")
    if not docker_path:
        return {
            "ok": False,
            "message": "Docker CLI is not installed or not in PATH",
            "remediation": "Install Docker Desktop from https://www.docker.com/products/docker-desktop/",
        }

    try:
        res = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            version = res.stdout.strip() or "running"
            return {
                "ok": True,
                "message": f"Docker daemon is running (version {version})",
                "remediation": "",
            }
        else:
            err_msg = res.stderr.strip() or "Docker daemon is unreachable"
            return {
                "ok": False,
                "message": f"Docker daemon is not running ({err_msg[:60]}...)",
                "remediation": "Start Docker Desktop on Windows and wait for engine status to reach 'Running'",
            }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "message": "Docker daemon timed out after 5s",
            "remediation": "Check if Docker Desktop is starting up or frozen",
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": f"Docker check encountered error: {exc}",
            "remediation": "Verify Docker Desktop service is running",
        }


def check_clickhouse_connection(
    host: str = "localhost",
    tcp_port: int = 9000,
    http_port: int = 8123,
    timeout: float = 2.0,
) -> Dict[str, Any]:
    """Ping ClickHouse native TCP port and HTTP ping endpoint."""
    tcp_ok = False
    http_ok = False
    details: List[str] = []

    # Check TCP Native Port
    try:
        sock = socket.create_connection((host, int(tcp_port)), timeout=timeout)
        sock.close()
        tcp_ok = True
        details.append(f"TCP :{tcp_port} reachable")
    except Exception as exc:
        details.append(f"TCP :{tcp_port} unreachable ({exc.__class__.__name__})")

    # Check HTTP ping endpoint
    try:
        url = f"http://{host}:{http_port}/ping"
        req = urllib.request.Request(url, headers={"User-Agent": "HarnessCheck/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="ignore").strip()
            if resp.status == 200 and "Ok." in content:
                http_ok = True
                details.append(f"HTTP :{http_port}/ping healthy ('Ok.')")
            else:
                details.append(f"HTTP :{http_port}/ping returned HTTP {resp.status}")
    except Exception as exc:
        details.append(f"HTTP :{http_port}/ping unreachable ({exc.__class__.__name__})")

    overall_ok = tcp_ok and http_ok
    return {
        "ok": overall_ok,
        "tcp_ok": tcp_ok,
        "http_ok": http_ok,
        "message": "; ".join(details),
        "remediation": "Run 'docker compose up -d' to start the ClickHouse container" if not overall_ok else "",
    }


def check_tavily_api(api_key: Optional[str] = None, timeout: float = 5.0) -> Dict[str, Any]:
    """Verify live Tavily API connectivity with a simple test query."""
    if not api_key or is_placeholder_value(api_key):
        return {
            "ok": False,
            "skipped": True,
            "message": "Tavily check skipped (TAVILY_API_KEY is not set)",
            "remediation": "Provide valid TAVILY_API_KEY in .env (get free tier at https://tavily.com)",
        }

    url = "https://api.tavily.com/search"
    payload = json.dumps({"query": "health check", "max_results": 1}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "HarnessCheck/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return {
                    "ok": True,
                    "message": "Tavily Search API healthy (HTTP 200)",
                    "remediation": "",
                }
            return {
                "ok": False,
                "message": f"Tavily returned unexpected status {resp.status}",
                "remediation": "Verify your Tavily API key and quota at https://tavily.com",
            }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "message": f"Tavily API HTTP error {exc.code}: {exc.reason}",
            "remediation": "Verify your Tavily API key quota or validity",
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": f"Tavily connection failed: {exc}",
            "remediation": "Check internet connectivity and DNS resolution",
        }


def run_all_checks(env_path: Optional[str] = None) -> Dict[str, Any]:
    """Execute all system, service, and credential checks."""
    env = get_merged_env(env_path)

    ch_host = env.get("CLICKHOUSE_HOST", "localhost")
    ch_tcp = int(env.get("CLICKHOUSE_PORT") or "9000")
    ch_http = int(env.get("CLICKHOUSE_HTTP_PORT") or "8123")
    tavily_key = env.get("TAVILY_API_KEY", "")

    return {
        "python": check_python_version(),
        "go": check_go_installed(),
        "environment": check_environment_variables(env),
        "docker": check_docker_daemon(),
        "clickhouse": check_clickhouse_connection(ch_host, ch_tcp, ch_http),
        "tavily": check_tavily_api(tavily_key),
    }


def format_summary_table(results: Dict[str, Any]) -> str:
    """Format check results as a clean terminal summary table."""
    lines: List[str] = []
    bar = "=" * 78
    divider = "-" * 78

    lines.append(bar)
    lines.append("  Agentic YouTube Analyzer & Fact-Checker: Verification Harness")
    lines.append(bar)
    lines.append(f"{'Component':<22} | {'Status':<10} | {'Details':<40}")
    lines.append(divider)

    # Python
    py = results["python"]
    py_status = "[OK]" if py["ok"] else "[FAIL]"
    lines.append(f"{'Python Runtime':<22} | {py_status:<10} | {py['version']:<40}")

    # Go
    go = results["go"]
    go_status = "[OK]" if go["ok"] else "[FAIL]"
    go_ver = go.get("version", "").split("\n")[0][:38]
    lines.append(f"{'Go Toolchain':<22} | {go_status:<10} | {go_ver:<40}")

    # Environment Variables
    env = results["environment"]
    env_status = "[OK]" if env["ok"] else "[WARN]"
    missing = ", ".join(env["missing_required"]) if env["missing_required"] else "All required present"
    lines.append(f"{'Environment Keys':<22} | {env_status:<10} | {missing:<40}")

    # Docker Daemon
    docker = results["docker"]
    docker_status = "[OK]" if docker["ok"] else "[FAIL]"
    docker_msg = docker["message"][:38]
    lines.append(f"{'Docker Engine':<22} | {docker_status:<10} | {docker_msg:<40}")

    # ClickHouse
    ch = results["clickhouse"]
    ch_status = "[OK]" if ch["ok"] else "[WARN]"
    ch_msg = ("TCP & HTTP OK" if ch["tcp_ok"] and ch["http_ok"] else ch["message"])[:38]
    lines.append(f"{'ClickHouse DB':<22} | {ch_status:<10} | {ch_msg:<40}")

    # Tavily API
    tavily = results["tavily"]
    if tavily.get("skipped"):
        tavily_status = "[SKIP]"
    else:
        tavily_status = "[OK]" if tavily["ok"] else "[WARN]"
    tavily_msg = tavily["message"][:38]
    lines.append(f"{'Tavily Search API':<22} | {tavily_status:<10} | {tavily_msg:<40}")

    lines.append(divider)

    # Collect remediation actions
    remediations: List[str] = []
    if not py["ok"] and py.get("remediation"):
        remediations.append(f"- Python: {py['remediation']}")
    if not go["ok"] and go.get("remediation"):
        remediations.append(f"- Go: {go['remediation']}")
    if not env["ok"]:
        remediations.append(
            f"- Environment: Populate missing keys ({', '.join(env['missing_required'])}) in .env (see .env.example)"
        )
    if not docker["ok"] and docker.get("remediation"):
        remediations.append(f"- Docker: {docker['remediation']}")
    if not ch["ok"] and ch.get("remediation"):
        remediations.append(f"- ClickHouse: {ch['remediation']}")
    if not tavily["ok"] and tavily.get("remediation"):
        remediations.append(f"- Tavily: {tavily['remediation']}")

    if remediations:
        lines.append("Action Items / Remediation:")
        for r in remediations:
            lines.append(f"  {r}")
        lines.append(divider)
    else:
        lines.append("  All live verification checks PASSED! System is ready.")
        lines.append(divider)

    return "\n".join(lines)


def main() -> int:
    """Run CLI entrypoint."""
    results = run_all_checks()
    table = format_summary_table(results)
    print(table)

    # Core requirements for running tests: Python, Go toolchain, and basic env
    # Note: When Docker Desktop or live keys aren't configured yet, exit cleanly without unhandled exception.
    all_critical_ok = results["python"]["ok"] and results["go"]["ok"]
    return 0 if all_critical_ok else 1


if __name__ == "__main__":
    sys.exit(main())
