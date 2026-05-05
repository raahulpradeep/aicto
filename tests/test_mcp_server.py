"""Tests for the MCP server wrapper around bin/cto."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock the mcp package before importing server.py (mcp may not be installed).
class _FakeFastMCP:
    def __init__(self, name):
        self.name = name

    def tool(self):
        def decorator(func):
            return func
        return decorator

mcp_mock = MagicMock()
mcp_mock.server.fastmcp.FastMCP = _FakeFastMCP
sys.modules["mcp"] = mcp_mock
sys.modules["mcp.server"] = mcp_mock.server
sys.modules["mcp.server.fastmcp"] = mcp_mock.server.fastmcp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))

import server  # noqa: E402


def test_task_bypass_cto_passes_flag():
    with patch.object(server, "_run", return_value="ok") as mock_run:
        server.task(
            team="demo",
            title="Test epic",
            kind="epic",
            bypass_cto=True,
        )
    mock_run.assert_called_once_with(
        ["task", "demo", "Test epic", "-p", "2", "--epic", "--bypass-cto"]
    )


def test_task_without_bypass_cto_does_not_pass_flag():
    with patch.object(server, "_run", return_value="ok") as mock_run:
        server.task(
            team="demo",
            title="Test epic",
            kind="epic",
        )
    mock_run.assert_called_once_with(
        ["task", "demo", "Test epic", "-p", "2", "--epic"]
    )


def test_task_bypass_cto_with_ops_raises():
    with patch.object(server, "_run", return_value="ok"):
        try:
            server.task(
                team="demo",
                title="Test epic",
                kind="epic",
                bypass_cto=True,
                ops=True,
            )
            assert False, "expected ValueError"
        except ValueError as e:
            assert "incompatible" in str(e).lower()


def test_task_bypass_cto_with_dev_raises():
    with patch.object(server, "_run", return_value="ok"):
        try:
            server.task(
                team="demo",
                title="Test dev",
                kind="dev",
                bypass_cto=True,
            )
            assert False, "expected ValueError"
        except ValueError as e:
            assert "bypass_cto" in str(e).lower()
