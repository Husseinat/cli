"""`c godaddy configure` — prompt for API credentials and save to disk."""
from __future__ import annotations

import json
import os
from pathlib import Path

import click

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "c" / "godaddy.json"


def load() -> dict[str, str]:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(key: str, secret: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"api_key": key, "api_secret": secret}, indent=2))
    CONFIG_PATH.chmod(0o600)


@click.command()
def configure() -> None:
    """Prompt for GoDaddy API key/secret and save to ~/.config/c/godaddy.json."""
    existing = load()
    key = click.prompt(
        "GoDaddy API Key",
        default=existing.get("api_key") or None,
        show_default=bool(existing.get("api_key")),
    ).strip()
    secret = click.prompt(
        "GoDaddy API Secret",
        default=existing.get("api_secret") or None,
        show_default=False,
        hide_input=True,
    ).strip()
    if not key or not secret:
        raise click.ClickException("Both key and secret are required.")
    save(key, secret)
    click.echo(f"Saved credentials to {CONFIG_PATH}")
