"""MCPServer application instance and entry point."""

import logging

from mcp.server.mcpserver import MCPServer

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

# Gate tool registration BEFORE the tools package is imported. Anything not
# enabled in tools.toml is never registered, so it is not advertised to the model
# and cannot be invoked even by name. See tool_policy for why this is an
# allowlist.
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
        "Tool policy: %d exposed, %d withheld (config: %s)",
        len(registered),
        len(withheld),
        tool_policy.config_path(),
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


# Export for `mcp run`
app = mcp


def main() -> None:
    """Main entry point for the server."""
    logger.info("Starting Monarch Money MCP Server...")
    log_tool_policy()
    try:
        mcp.run()
    except Exception as e:
        logger.error(f"Failed to run server: {str(e)}")
        raise


if __name__ == "__main__":
    main()
