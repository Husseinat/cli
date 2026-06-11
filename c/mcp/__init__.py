"""`c mcp` — Model Context Protocol servers exposed by the c toolbox.

Each registered server is a thin MCP wrapper around an existing `c` subcommand,
letting an MCP client (e.g. Claude) drive it as a set of tools. `c mcp [NAME]`
runs one server over stdio; `c setup` registers them in the Claude config files.

To add a server: write `c/mcp/<name>_server.py` exposing `build() -> FastMCP`,
then add an entry to `SERVERS` and a branch in `build_server()`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the heavy mcp SDK just to read the registry
    from mcp.server.fastmcp import FastMCP

# server name -> one-line description (shown by `c mcp --list`)
SERVERS: dict[str, str] = {
    "logs": "AWS Lambda / CloudWatch Logs — list log groups and search function logs.",
}


def build_server(name: str) -> "FastMCP":
    """Construct the FastMCP server registered under ``name``."""
    if name == "logs":
        from c.mcp.logs_server import build

        return build()
    raise KeyError(name)
