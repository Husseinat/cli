"""`c mcp` (run an MCP server) and `c setup` (register them with Claude)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import click

from c.mcp import SERVERS, build_server

# Key under which a server is registered in a Claude config's `mcpServers`.
_KEY_PREFIX = "c-"


# ─── `c mcp` ─────────────────────────────────────────────────────────────────
@click.command("mcp", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("server", default="logs")
@click.option(
    "--list",
    "-l",
    "list_only",
    is_flag=True,
    help="List the available MCP servers and exit.",
)
def mcp_cmd(server: str, list_only: bool) -> None:
    """Run an MCP server over stdio (SERVER defaults to 'logs').

    This is normally launched by an MCP client, not by hand — run `c setup`
    once to register it in the Claude config files. Use `--list` to see the
    available servers.
    """
    if list_only:
        for name, desc in SERVERS.items():
            click.echo(f"  {click.style(name, fg='cyan')}  {desc}")
        return

    if server not in SERVERS:
        click.secho(
            f"✗ unknown MCP server {server!r}. Available: {', '.join(SERVERS)}",
            fg="red",
            err=True,
        )
        sys.exit(1)

    # stdout is the MCP transport — never write to it before run().
    build_server(server).run()


# ─── `c setup` ───────────────────────────────────────────────────────────────
def _server_command() -> tuple[str, list[str]]:
    """How an MCP client should invoke `c`. Prefer an absolute path so the
    client finds it regardless of its own PATH."""
    exe = shutil.which("c")
    if exe:
        return exe, []
    return sys.executable, ["-m", "c.cli"]


def _claude_desktop_config() -> Path:
    """Platform-specific Claude Desktop config path."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform.startswith("win"):
        import os

        base = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        return base / "Claude/claude_desktop_config.json"
    return home / ".config/Claude/claude_desktop_config.json"


def _targets() -> dict[str, Path]:
    """Map each target name to the config file it writes."""
    return {
        "claude-code": Path.home() / ".claude.json",
        "claude-desktop": _claude_desktop_config(),
        "project": Path.cwd() / ".mcp.json",
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise click.ClickException(f"{path} is not valid JSON: {e}")
    return data if isinstance(data, dict) else {}


@click.command("setup", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--target",
    "targets",
    multiple=True,
    type=click.Choice(["claude-code", "claude-desktop", "project"]),
    help="Claude config(s) to update. Repeatable. Default: claude-code, plus "
    "claude-desktop if it is installed.",
)
@click.option(
    "--server",
    "servers",
    multiple=True,
    help="MCP server(s) to register. Repeatable. Default: all.",
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would change without writing."
)
def setup_cmd(
    targets: tuple[str, ...], servers: tuple[str, ...], dry_run: bool
) -> None:
    """Register the `c` MCP servers in the Claude config files.

    Adds an entry to each config's `mcpServers` so Claude launches `c mcp
    <server>` on startup. Existing config content is preserved and a `.bak`
    backup is written before any change.
    """
    chosen = list(servers) or list(SERVERS)
    unknown = [s for s in chosen if s not in SERVERS]
    if unknown:
        raise click.ClickException(
            f"unknown server(s): {', '.join(unknown)}. "
            f"Available: {', '.join(SERVERS)}"
        )

    all_targets = _targets()
    if targets:
        selected = {t: all_targets[t] for t in targets}
    else:
        # Auto: always Claude Code; Claude Desktop only if it's installed.
        selected = {"claude-code": all_targets["claude-code"]}
        if all_targets["claude-desktop"].parent.is_dir():
            selected["claude-desktop"] = all_targets["claude-desktop"]

    cmd, base_args = _server_command()
    entries = {
        f"{_KEY_PREFIX}{name}": {"command": cmd, "args": [*base_args, "mcp", name]}
        for name in chosen
    }

    click.secho(
        f"  registering {len(entries)} server(s): {', '.join(entries)}", fg="cyan"
    )

    for target, path in selected.items():
        data = _load_json(path)
        servers_obj = data.setdefault("mcpServers", {})
        if not isinstance(servers_obj, dict):
            raise click.ClickException(f"{path}: `mcpServers` is not an object")

        changes = [
            ("update" if k in servers_obj else "add")
            for k in entries
        ]
        servers_obj.update(entries)

        verb = "would write" if dry_run else "wrote"
        click.echo()
        click.secho(f"  {target}  →  {path}", fg="cyan")
        for key, change in zip(entries, changes):
            click.echo(f"    {change:<6} {key}")

        if dry_run:
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_text(path.read_text())
            click.echo(f"    backup {backup}")
        path.write_text(json.dumps(data, indent=2) + "\n")
        click.secho(f"    {verb} {path}", fg="green")

    click.echo()
    if dry_run:
        click.secho("  dry run — nothing written.", fg="yellow")
    else:
        click.secho(
            "  done. Restart Claude (or `claude` / Claude Desktop) to load the "
            "servers.",
            fg="green",
        )
