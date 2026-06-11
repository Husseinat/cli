"""`c godaddy set-ns DOMAIN` — point a GoDaddy domain's nameservers at Route53 (or an explicit list)."""
from __future__ import annotations

import click

from c.aws.runner import AwsError, run_aws
from c.godaddy.api import GoDaddyError, request as gd_request


def _route53_nameservers(domain: str, profile: str | None) -> list[str]:
    target = domain.rstrip(".") + "."
    zones = run_aws(
        ["route53", "list-hosted-zones-by-name", "--dns-name", domain],
        profile=profile, parse_json=True,
    ).get("HostedZones", [])
    zone = next(
        (z for z in zones if z["Name"] == target and not z.get("Config", {}).get("PrivateZone", False)),
        None,
    )
    if not zone:
        raise click.ClickException(
            f"No public Route53 hosted zone for '{domain}'. "
            f"Run `c aws zone ensure {domain}` first, or pass --nameservers explicitly."
        )
    zone_id = zone["Id"].split("/")[-1]
    data = run_aws(
        ["route53", "get-hosted-zone", "--id", zone_id],
        profile=profile, parse_json=True,
    )
    return data["DelegationSet"]["NameServers"]


def ensure_godaddy_ns(domain: str, nameservers: list[str]) -> bool:
    """Point the GoDaddy-registered `domain` at `nameservers`.

    Returns True when an update was made, False when GoDaddy already matches.
    Raises GoDaddyError (missing credentials, domain not in account, API error).
    """
    current = gd_request("GET", f"/v1/domains/{domain}")
    current_ns = sorted((n or "").lower().rstrip(".") for n in (current.get("nameServers") or []))
    target_ns = sorted(n.lower().rstrip(".") for n in nameservers)
    if current_ns == target_ns:
        return False
    gd_request("PATCH", f"/v1/domains/{domain}", {"nameServers": nameservers})
    return True


@click.command("set-ns")
@click.argument("domain")
@click.option(
    "--nameservers", "-n", multiple=True,
    help="NS hostname(s) to set. Pass multiple times. If omitted, read from the Route53 zone for DOMAIN.",
)
@click.pass_context
def set_ns(ctx: click.Context, domain: str, nameservers: tuple[str, ...]) -> None:
    """Update the authoritative nameservers for a GoDaddy-registered domain."""
    profile = ctx.obj.get("profile")
    domain = domain.strip().lower().rstrip(".")

    ns = [n.strip().rstrip(".") for n in nameservers] or _route53_nameservers(domain, profile)
    if len(ns) < 2:
        raise click.ClickException(
            f"Got only {len(ns)} nameserver(s); need at least 2. (Route53 delegation sets always return 4.)"
        )

    try:
        updated = ensure_godaddy_ns(domain, ns)
    except GoDaddyError as e:
        raise click.ClickException(str(e))

    if not updated:
        click.secho(f"  ↷ GoDaddy NS for {domain} already match:", fg="yellow")
        for n in ns:
            click.echo(f"    {n}")
        return

    click.secho(f"→ Updated GoDaddy NS for {domain}:", fg="cyan")
    for n in ns:
        click.echo(f"    {n}")
    click.secho("  ✓ updated (propagation can take a few hours)", fg="green")
