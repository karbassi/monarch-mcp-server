"""MCPServer application instance and entry point."""

import argparse
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette

try:  # mcp >= 2.0 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer

    #: mcp 2.x takes HTTP settings per call; 1.x reads them from mcp.settings.
    _MCP_V2 = True
except ImportError:  # mcp < 2.0
    _MCP_V2 = False
    # mypy type-checks against the installed (2.x) mcp, where these 1.x names
    # are absent; the fallback is only taken at runtime under mcp 1.x.
    from mcp.server.fastmcp import (  # type: ignore[no-redef,attr-defined]
        FastMCP as MCPServer,
    )

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# The gql aiohttp transport logs full GraphQL requests/responses at INFO, which
# can include Monarch account payloads. Raise its floor to WARNING so those
# payloads are not written to logs. Transport-level errors still surface; drop
# this to INFO/DEBUG temporarily if you need to trace GraphQL traffic.
logging.getLogger("gql.transport.aiohttp").setLevel(logging.WARNING)

# Initialize the MCP server
mcp = MCPServer("Monarch Money MCP Server")

# Both gates must run before the tool modules are imported, since they work by
# wrapping mcp.tool() and registration happens at import time.
#
# read_only is installed first so that it is the INNER wrapper and tool_policy
# the outer one. The outer wrapper sees every registration attempt, which is
# what keeps tool_inventory() complete; installed the other way round,
# read_only short-circuited the write tools before tool_policy could record
# them, so under MONARCH_MCP_READ_ONLY the inventory silently lost all of them.
# tool_policy also folds the read-only flag into its own decision, so the
# withheld/registered split it reports is the real one.
from monarch_mcp_server import read_only  # noqa: E402

read_only.install(mcp)

# Anything not enabled in tools.toml is never registered, so it is not
# advertised to the model and cannot be invoked even by name. See tool_policy
# for why this is an allowlist.
from monarch_mcp_server import tool_policy  # noqa: E402

_withheld, _registered = tool_policy.enforce(mcp)

# Import tools package to trigger @mcp.tool() registration
import monarch_mcp_server.tools  # noqa: E402, F401


# Deliberately functions, not tuples snapshotted at import time. The tools
# package imports back into this module, so when an import chain starts from the
# tools side, `import monarch_mcp_server.tools` below finds a partially
# initialised module in sys.modules and returns immediately -- registration then
# completes *after* this point. Snapshotting here recorded an empty inventory.
def tool_inventory() -> tuple[str, ...]:
    """Every tool that attempted registration, exposed or not.

    The authoritative inventory: the MCP registry holds only the enabled subset,
    so an unclassified tool is missing from it precisely because it was withheld.
    """
    return tuple(sorted(_withheld + _registered))


def withheld_tools() -> tuple[str, ...]:
    return tuple(sorted(_withheld))


def registered_tools() -> tuple[str, ...]:
    return tuple(sorted(_registered))


def log_tool_policy() -> None:
    """Report both sides of the gate. Called from main(), once imports settle.

    A silent gate is indistinguishable from one that has stopped working, and a
    configured name that never registers means upstream renamed it.
    """
    withheld, registered = withheld_tools(), registered_tools()
    logger.info(
        "Tool policy: %d exposed, %d withheld (config: %s%s)",
        len(registered),
        len(withheld),
        tool_policy.config_path(),
        f", {read_only.ENV_VAR} on" if read_only.is_read_only() else "",
    )
    if withheld:
        logger.info("Withheld (enable in tools.toml): %s", ", ".join(withheld))
    unclassified = (
        set(tool_inventory())
        - set(tool_policy.READ_TOOLS)
        - set(tool_policy.WRITE_TOOLS)
    )
    if unclassified:
        logger.warning(
            "Tools present but unclassified, so withheld: %s",
            ", ".join(sorted(unclassified)),
        )

# Starlette redirects /mcp/ to /mcp at routing time, before the endpoint and so
# before the Host check, and it builds the Location header from the request's
# own Host. A request with Host: evil.example therefore got a 307 to
# http://evil.example/mcp, and a 307 preserves the method and body, which here
# means tool arguments. Behind the reverse proxy the README recommends, which
# passes the public Host through, a client configured with a trailing slash
# would resend its body to an attacker chosen host.
#
# mcp.run() builds the Starlette app internally, so the builder is wrapped
# rather than the run path, which leaves the transport selection untouched.
_build_streamable_http_app = mcp.streamable_http_app


def _streamable_http_app_without_slash_redirect(
    *args: Any, **kwargs: Any
) -> "Starlette":
    # Forward everything: mcp 2.x passes host, transport_security and the rest
    # as keywords, both from run() and from http_app() below.
    http_app = _build_streamable_http_app(*args, **kwargs)
    http_app.router.redirect_slashes = False
    return http_app


mcp.streamable_http_app = _streamable_http_app_without_slash_redirect  # type: ignore[method-assign]

# Export for `mcp run`
app = mcp


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _env_list(name: str) -> list[str]:
    return [
        value.strip() for value in os.environ.get(name, "").split(",") if value.strip()
    ]


#: Bind addresses that only accept connections from this machine.
LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: The HTTP transport does not authenticate callers, so a non-loopback bind hands
#: the Monarch session to anything that can reach the port. Refuse it unless the
#: operator opts in -- the Docker image does, because inside a container the
#: server must listen on 0.0.0.0 for a published port to reach it.
ALLOW_REMOTE_BIND_ENV = "MONARCH_MCP_ALLOW_REMOTE_BIND"

_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})


def _remote_bind_allowed() -> bool:
    return os.environ.get(ALLOW_REMOTE_BIND_ENV, "").strip().lower() in _TRUTHY


# Loopback names are always accepted for Host/Origin validation. Listening on all
# interfaces must not disable that check; remote names are added explicitly.
_LOCAL_ALLOWED_HOSTS = [
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
    "[::1]",
    "[::1]:*",
]
_LOCAL_ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:*",
    "http://127.0.0.1",
    "http://127.0.0.1:*",
    "http://[::1]",
    "http://[::1]:*",
]


def _transport_security(
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> "TransportSecuritySettings":
    """Host/Origin validation (DNS-rebinding protection) for the HTTP transport.

    Loopback names are always allowed. ``allowed_hosts``/``allowed_origins`` add
    remote names; when either is None it falls back to the matching
    MONARCH_MCP_ALLOWED_* env var (comma-separated).

    mcp 2.x takes this per call rather than through mutable server settings, so
    both ``main`` and ``http_app`` build it here rather than assigning it once.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        allowed_hosts=[
            *_LOCAL_ALLOWED_HOSTS,
            *(
                allowed_hosts
                if allowed_hosts is not None
                else _env_list("MONARCH_MCP_ALLOWED_HOSTS")
            ),
        ],
        allowed_origins=[
            *_LOCAL_ALLOWED_ORIGINS,
            *(
                allowed_origins
                if allowed_origins is not None
                else _env_list("MONARCH_MCP_ALLOWED_ORIGINS")
            ),
        ],
    )


def _configure_v1_http(
    security: "TransportSecuritySettings", host: str, port: int | None = None
) -> None:
    """mcp 1.x: HTTP settings live on mcp.settings, and its builder and run()
    take no keywords. Refuse a 1.x old enough to lack transport_security rather
    than serve HTTP without Host/Origin validation."""
    # Typed as Any: mypy checks against the installed 2.x, whose settings have
    # no host/port, and this path only runs under 1.x.
    settings: Any = mcp.settings
    if not hasattr(settings, "transport_security"):
        raise RuntimeError(
            "this mcp 1.x release has no transport_security setting, so the HTTP "
            "transport would run without Host/Origin validation; upgrade mcp"
        )
    settings.host = host
    if port is not None:
        settings.port = port
    settings.transport_security = security


def http_app(
    host: str = "127.0.0.1",
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> "Starlette":
    """The streamable-HTTP ASGI app with Host/Origin validation applied."""
    security = _transport_security(allowed_hosts, allowed_origins)
    if not _MCP_V2:
        _configure_v1_http(security, host)
        return mcp.streamable_http_app()
    return mcp.streamable_http_app(host=host, transport_security=security)


def main(argv: list[str] | None = None) -> None:
    """Main entry point for the server."""
    parser = argparse.ArgumentParser(description="Monarch Money MCP Server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "http"),
        default=os.environ.get("MONARCH_MCP_TRANSPORT", "stdio"),
        help="MCP transport (env: MONARCH_MCP_TRANSPORT; default: stdio)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MONARCH_MCP_HOST", "127.0.0.1"),
        help="HTTP bind address (env: MONARCH_MCP_HOST; default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=os.environ.get("MONARCH_MCP_PORT", "8000"),
        help="HTTP port (env: MONARCH_MCP_PORT; default: 8000)",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="Additional HTTP Host value, e.g. mcp.example.com (repeatable; "
        "env: MONARCH_MCP_ALLOWED_HOSTS, comma-separated)",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=None,
        help="Additional browser Origin, e.g. https://client.example.com (repeatable; "
        "env: MONARCH_MCP_ALLOWED_ORIGINS, comma-separated)",
    )
    args = parser.parse_args(argv)
    # argparse does not check choices for defaults supplied by the environment.
    if args.transport not in ("stdio", "streamable-http", "http"):
        parser.error("MONARCH_MCP_TRANSPORT must be stdio, streamable-http, or http")
    if not args.host.strip():
        parser.error("HTTP host must not be empty")
    if (
        args.transport != "stdio"
        and args.host.strip() not in LOOPBACK_BIND_HOSTS
        and not _remote_bind_allowed()
    ):
        parser.error(
            f"refusing to bind HTTP to non-loopback host {args.host!r}: the HTTP "
            "transport does not authenticate callers. Bind 127.0.0.1 behind an "
            f"authenticating proxy, or set {ALLOW_REMOTE_BIND_ENV}=1 to accept "
            "the risk (the Docker image does this)."
        )

    logger.info("Starting Monarch Money MCP Server (%s)...", args.transport)
    log_tool_policy()
    try:
        if args.transport == "stdio":
            mcp.run()
        else:
            security = _transport_security(
                allowed_hosts=args.allowed_host,
                allowed_origins=args.allowed_origin,
            )
            if _MCP_V2:
                # mcp 2.x configures the HTTP transport per call rather than
                # through mutable settings. Host/Origin validation is passed
                # alongside the bind host and port.
                mcp.run(
                    "streamable-http",
                    host=args.host,
                    port=args.port,
                    transport_security=security,
                )
            else:
                _configure_v1_http(security, args.host, args.port)
                mcp.run("streamable-http")
    except Exception as e:
        logger.error(f"Failed to run server: {str(e)}")
        raise


if __name__ == "__main__":
    main()
