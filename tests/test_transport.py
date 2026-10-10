"""Transport selection and real MCP requests through the HTTP ASGI application."""

import json
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from monarch_mcp_server import app


@pytest.fixture(autouse=True)
def isolate_transport(monkeypatch):
    for suffix in (
        "TRANSPORT",
        "HOST",
        "PORT",
        "ALLOWED_HOSTS",
        "ALLOWED_ORIGINS",
        "ALLOW_REMOTE_BIND",
    ):
        monkeypatch.delenv(f"MONARCH_MCP_{suffix}", raising=False)
    # mcp 2.x keeps the streamable-HTTP session manager on the low-level server,
    # not on the MCPServer facade (it was ``app.mcp._session_manager`` under
    # mcp 1.x's FastMCP). Reset it there so each test rebuilds a fresh app.
    monkeypatch.setattr(app.mcp._lowlevel_server, "_session_manager", None)
    run = Mock()
    monkeypatch.setattr(app.mcp, "run", run)
    return run


def test_default_remains_stdio(isolate_transport):
    app.main([])
    isolate_transport.assert_called_once_with()


def test_environment_configures_http(monkeypatch, isolate_transport):
    monkeypatch.setenv("MONARCH_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MONARCH_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MONARCH_MCP_ALLOW_REMOTE_BIND", "1")
    monkeypatch.setenv("MONARCH_MCP_PORT", "9000")
    monkeypatch.setenv(
        "MONARCH_MCP_ALLOWED_HOSTS", "mcp.example.com, server.lan:9000, "
    )
    monkeypatch.setenv("MONARCH_MCP_ALLOWED_ORIGINS", "https://client.example.com")
    app.main([])
    isolate_transport.assert_called_once()
    call = isolate_transport.call_args
    assert call.args == ("streamable-http",)
    assert (call.kwargs["host"], call.kwargs["port"]) == ("0.0.0.0", 9000)
    security = call.kwargs["transport_security"]
    assert security.enable_dns_rebinding_protection
    assert "server.lan:9000" in security.allowed_hosts
    assert "https://client.example.com" in security.allowed_origins


def test_cli_overrides_environment(monkeypatch, isolate_transport):
    monkeypatch.setenv("MONARCH_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("MONARCH_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MONARCH_MCP_PORT", "invalid")
    monkeypatch.setenv("MONARCH_MCP_ALLOWED_HOSTS", "old.example.com")
    monkeypatch.setenv("MONARCH_MCP_ALLOWED_ORIGINS", "https://old.example.com")
    monkeypatch.setenv("MONARCH_MCP_ALLOW_REMOTE_BIND", "1")
    app.main(
        [
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
            "--allowed-host",
            "mcp.example.com",
            "--allowed-host",
            "server.lan:9001",
            "--allowed-origin",
            "https://client.example.com",
        ]
    )
    isolate_transport.assert_called_once()
    call = isolate_transport.call_args
    assert call.args == ("streamable-http",)
    assert (call.kwargs["host"], call.kwargs["port"]) == ("0.0.0.0", 9001)
    security = call.kwargs["transport_security"]
    assert "mcp.example.com" in security.allowed_hosts
    assert "server.lan:9001" in security.allowed_hosts
    assert "old.example.com" not in security.allowed_hosts
    assert "https://client.example.com" in security.allowed_origins
    assert "https://old.example.com" not in security.allowed_origins


def test_stdio_overrides_container_default(monkeypatch, isolate_transport):
    monkeypatch.setenv("MONARCH_MCP_TRANSPORT", "streamable-http")
    app.main(["--transport", "stdio"])
    isolate_transport.assert_called_once_with()


@pytest.mark.parametrize("port", ["0", "65536", "-1", "invalid"])
def test_invalid_environment_port_fails(monkeypatch, isolate_transport, port):
    monkeypatch.setenv("MONARCH_MCP_PORT", port)
    with pytest.raises(SystemExit) as exc:
        app.main([])
    assert exc.value.code == 2
    isolate_transport.assert_not_called()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "mcp.example.com"])
def test_non_loopback_bind_is_refused_without_opt_in(
    monkeypatch, isolate_transport, host
):
    monkeypatch.setenv("MONARCH_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MONARCH_MCP_HOST", host)
    with pytest.raises(SystemExit) as exc:
        app.main([])
    assert exc.value.code == 2
    isolate_transport.assert_not_called()


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_falsy_opt_in_still_refuses_non_loopback(
    monkeypatch, isolate_transport, value
):
    monkeypatch.setenv("MONARCH_MCP_ALLOW_REMOTE_BIND", value)
    with pytest.raises(SystemExit):
        app.main(["--transport", "http", "--host", "0.0.0.0"])
    isolate_transport.assert_not_called()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_bind_needs_no_opt_in(monkeypatch, isolate_transport, host):
    app.main(["--transport", "http", "--host", host, "--port", "8518"])
    call = isolate_transport.call_args
    assert (call.kwargs["host"], call.kwargs["port"]) == (host, 8518)


def test_default_http_bind_is_loopback(isolate_transport):
    app.main(["--transport", "http"])
    assert isolate_transport.call_args.kwargs["host"] == "127.0.0.1"


def test_stdio_ignores_the_bind_guard(monkeypatch, isolate_transport):
    monkeypatch.setenv("MONARCH_MCP_HOST", "0.0.0.0")
    app.main([])
    isolate_transport.assert_called_once_with()


def test_dockerfile_opts_in_to_its_remote_bind():
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "MONARCH_MCP_HOST=0.0.0.0" in dockerfile
    assert "MONARCH_MCP_ALLOW_REMOTE_BIND=1" in dockerfile


def test_invalid_environment_transport_fails(monkeypatch, isolate_transport):
    monkeypatch.setenv("MONARCH_MCP_TRANSPORT", "invalid")
    with pytest.raises(SystemExit) as exc:
        app.main([])
    assert exc.value.code == 2
    isolate_transport.assert_not_called()


HEADERS = {"Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "transport-test", "version": "1.0"},
    },
}


def rpc_result(response):
    assert response.status_code == 200, response.text
    # Keep the production SSE response mode, rather than changing server settings.
    data = next(
        line[6:] for line in response.text.splitlines() if line.startswith("data: ")
    )
    return json.loads(data)["result"]


def test_http_initialize_list_and_call_tool(mock_monarch_client):
    with TestClient(
        app.http_app(host="0.0.0.0"), base_url="http://localhost:8000"
    ) as client:
        response = client.post("/mcp", headers=HEADERS, json=INITIALIZE)
        assert rpc_result(response)["serverInfo"]["name"] == "Monarch Money MCP Server"
        headers = {**HEADERS, "Mcp-Session-Id": response.headers["mcp-session-id"]}
        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        assert initialized.status_code == 202
        listed = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        assert "get_accounts" in {tool["name"] for tool in rpc_result(listed)["tools"]}
        called = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_accounts", "arguments": {}},
            },
        )
        result = rpc_result(called)
        assert not result.get("isError")
        assert "Checking Account" in result["content"][0]["text"]
        mock_monarch_client.get_accounts.assert_awaited_once()
        assert client.delete("/mcp", headers=headers).status_code == 200


@pytest.mark.parametrize(
    "headers,status",
    [
        # mcp 2.x answers a rejected Host with 421 and a rejected Origin with 403
        # (mcp 1.x used 400 for the Origin case).
        ({"Host": "evil.example"}, 421),
        ({"Origin": "https://evil.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://localhost"}, 200),
        ({"Host": "localhost"}, 200),
    ],
)
def test_http_validates_host_and_origin(headers, status):
    with TestClient(
        app.http_app(host="0.0.0.0"), base_url="http://localhost:8000"
    ) as client:
        response = client.post("/mcp", headers={**HEADERS, **headers}, json=INITIALIZE)
        assert response.status_code == status


def test_http_allows_configured_remote_host_and_origin():
    with TestClient(
        app.http_app(
            allowed_hosts=["mcp.example.com"],
            allowed_origins=["https://client.example.com"],
        ),
        base_url="https://mcp.example.com",
    ) as client:
        response = client.post(
            "/mcp",
            json=INITIALIZE,
            headers={
                **HEADERS,
                "Origin": "https://client.example.com",
            },
        )
        assert "serverInfo" in rpc_result(response)


def test_trailing_slash_does_not_redirect_to_a_forged_host():
    """/mcp/ must not 307 to the request's own Host (upstream ee295b6). Under mcp
    2.x the wrapper has to forward the builder's keyword arguments, or http_app()
    and run() both fail before serving anything."""
    with TestClient(app.http_app(), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/mcp/",
            json=INITIALIZE,
            headers={**HEADERS, "Host": "evil.example.com"},
            follow_redirects=False,
        )
    assert response.status_code != 307
    assert "location" not in response.headers


class TestMcp1xHttpPath:
    """mcp 1.x reads HTTP settings from mcp.settings and its run() and builder
    take no keywords. pyproject still admits 1.x, so both entry points need a
    path that does not pass 2.x keywords."""

    def test_main_configures_settings_and_runs_without_keywords(
        self, monkeypatch, isolate_transport
    ):
        from types import SimpleNamespace

        settings = SimpleNamespace(host=None, port=None, transport_security=None)
        monkeypatch.setattr(app, "_MCP_V2", False)
        monkeypatch.setattr(app.mcp, "settings", settings)
        app.main(
            ["--transport", "http", "--port", "8518", "--allowed-host", "mcp.ali.fyi"]
        )
        isolate_transport.assert_called_once_with("streamable-http")
        assert (settings.host, settings.port) == ("127.0.0.1", 8518)
        assert "mcp.ali.fyi" in settings.transport_security.allowed_hosts

    def test_http_app_calls_the_builder_without_keywords(self, monkeypatch):
        from types import SimpleNamespace

        settings = SimpleNamespace(host=None, port=None, transport_security=None)
        builder = Mock(return_value="app")
        monkeypatch.setattr(app, "_MCP_V2", False)
        monkeypatch.setattr(app.mcp, "settings", settings)
        monkeypatch.setattr(app.mcp, "streamable_http_app", builder)
        assert app.http_app(allowed_hosts=["mcp.ali.fyi"]) == "app"
        builder.assert_called_once_with()
        assert settings.host == "127.0.0.1"
        assert "mcp.ali.fyi" in settings.transport_security.allowed_hosts

    def test_a_1x_without_transport_security_is_refused(
        self, monkeypatch, isolate_transport
    ):
        from types import SimpleNamespace

        monkeypatch.setattr(app, "_MCP_V2", False)
        monkeypatch.setattr(app.mcp, "settings", SimpleNamespace(host=None, port=None))
        with pytest.raises(RuntimeError, match="transport_security"):
            app.main(["--transport", "http"])
        isolate_transport.assert_not_called()
