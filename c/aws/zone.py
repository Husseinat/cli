"""`c aws zone ensure DOMAIN` — ensure a public Route53 hosted zone exists."""
from __future__ import annotations

import uuid

import click

from c.aws.runner import run_aws


def _find_zone(domain: str, profile: str | None) -> dict | None:
    target = domain.rstrip(".") + "."
    data = run_aws(
        ["route53", "list-hosted-zones-by-name", "--dns-name", domain],
        profile=profile, parse_json=True,
    )
    for z in data.get("HostedZones", []):
        if z["Name"] == target and not z.get("Config", {}).get("PrivateZone", False):
            return z
    return None


def _zone_nameservers(zone_id: str, profile: str | None) -> list[str]:
    data = run_aws(
        ["route53", "get-hosted-zone", "--id", zone_id],
        profile=profile, parse_json=True,
    )
    return data["DelegationSet"]["NameServers"]


@click.group("zone", context_settings={"help_option_names": ["-h", "--help"]})
def zone() -> None:
    """Route53 hosted zone tools."""


@zone.command("ensure")
@click.argument("domain")
@click.pass_context
def ensure(ctx: click.Context, domain: str) -> None:
    """Create a public hosted zone for DOMAIN if missing, then print nameservers."""
    profile = ctx.obj.get("profile")
    domain = domain.strip().lower().rstrip(".")

    existing = _find_zone(domain, profile)
    if existing:
        zone_id = existing["Id"].split("/")[-1]
        click.secho(f"  ↷ hosted zone already exists: {zone_id}", fg="yellow")
    else:
        result = run_aws([
            "route53", "create-hosted-zone",
            "--name", domain,
            "--caller-reference", f"c-{uuid.uuid4().hex[:16]}",
        ], profile=profile, parse_json=True)
        zone_id = result["HostedZone"]["Id"].split("/")[-1]
        click.secho(f"  ✓ created hosted zone {zone_id}", fg="green")

    ns = _zone_nameservers(zone_id, profile)
    click.echo()
    click.echo(f"Hosted zone ID: {zone_id}")
    click.echo("Nameservers (point your registrar at these):")
    for n in ns:
        click.echo(f"  {n}")
