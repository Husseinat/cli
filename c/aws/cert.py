"""`c aws cert issue DOMAIN` — request a DNS-validated ACM cert and auto-validate via Route53.

Default SANs: `*.DOMAIN` (covers any single-label subdomain). The apex is the
DomainName itself. Extra SANs can be added with `--san`.

Re-running is safe: we look for an existing ACM certificate (ISSUED or
PENDING_VALIDATION) that already covers everything we need, and only request a
new cert when none is found.
"""
from __future__ import annotations

import json
import time
import uuid

import click

from c.aws.runner import AwsError, run_aws

CERT_REGION = "us-east-1"  # CloudFront requires certs in us-east-1.


def _step(msg: str) -> None:
    click.secho(f"→ {msg}", fg="cyan")


def _ok(msg: str) -> None:
    click.secho(f"  ✓ {msg}", fg="green")


def _skip(msg: str) -> None:
    click.secho(f"  ↷ {msg}", fg="yellow")


def _covers(pattern: str, host: str) -> bool:
    pattern = pattern.lower().rstrip(".")
    host = host.lower().rstrip(".")
    if pattern == host:
        return True
    if pattern.startswith("*."):
        suffix = pattern[2:]
        labels = host.split(".")
        return len(labels) >= 2 and ".".join(labels[1:]) == suffix
    return False


def _find_existing(needs: list[str], profile: str | None) -> dict | None:
    """Return an ACM cert detail dict covering every `needs`, or None."""
    summaries = run_aws(
        [
            "acm", "list-certificates",
            "--certificate-statuses", "ISSUED", "PENDING_VALIDATION",
        ],
        profile=profile, region=CERT_REGION, parse_json=True,
    ).get("CertificateSummaryList", [])
    for s in summaries:
        arn = s["CertificateArn"]
        cert = run_aws(
            ["acm", "describe-certificate", "--certificate-arn", arn],
            profile=profile, region=CERT_REGION, parse_json=True,
        )["Certificate"]
        names = {cert.get("DomainName", "")} | set(cert.get("SubjectAlternativeNames") or [])
        if all(any(_covers(n, need) for n in names) for need in needs):
            return cert
    return None


def _find_hosted_zone(domain: str, profile: str | None) -> str:
    data = run_aws(
        ["route53", "list-hosted-zones-by-name", "--dns-name", domain],
        profile=profile, parse_json=True,
    )
    target = domain.rstrip(".") + "."
    for zone in data.get("HostedZones", []):
        if zone["Name"] == target and not zone.get("Config", {}).get("PrivateZone", False):
            return zone["Id"].split("/")[-1]
    raise click.ClickException(
        f"No public Route53 hosted zone for '{domain}'. Run `c aws zone ensure {domain}` first."
    )


def _wait_for_validation_records(cert_arn: str, profile: str | None) -> list[dict]:
    """ACM publishes the DNS validation records a few seconds after request-certificate."""
    for _ in range(30):
        cert = run_aws(
            ["acm", "describe-certificate", "--certificate-arn", cert_arn],
            profile=profile, region=CERT_REGION, parse_json=True,
        )["Certificate"]
        options = cert.get("DomainValidationOptions", []) or []
        if options and all(o.get("ResourceRecord") for o in options):
            return options
        time.sleep(2)
    raise click.ClickException("ACM did not publish validation records in time.")


def _write_validation_records(
    options: list[dict], zone_id: str, profile: str | None
) -> int:
    """UPSERT one CNAME per unique validation record. ACM dupes records across
    SANs that resolve to the same zone; we dedupe so change-batch stays valid."""
    seen: set[tuple[str, str]] = set()
    changes: list[dict] = []
    for o in options:
        rr = o["ResourceRecord"]
        key = (rr["Name"], rr["Type"])
        if key in seen:
            continue
        seen.add(key)
        changes.append({
            "Action": "UPSERT",
            "ResourceRecordSet": {
                "Name": rr["Name"],
                "Type": rr["Type"],
                "TTL": 300,
                "ResourceRecords": [{"Value": rr["Value"]}],
            },
        })
    if not changes:
        return 0
    run_aws([
        "route53", "change-resource-record-sets",
        "--hosted-zone-id", zone_id,
        "--change-batch", json.dumps({"Changes": changes}),
    ], profile=profile)
    return len(changes)


def ensure_certificate(
    domain: str,
    sans: list[str] | None,
    profile: str | None,
    *,
    wait: bool = True,
) -> str:
    """Return the ARN of an ISSUED ACM cert covering `domain` + `sans`.

    Reuses an existing matching cert if one exists (ISSUED or PENDING_VALIDATION);
    otherwise requests a new one and auto-validates via Route53. When `wait` is
    True, blocks until the cert reaches ISSUED.
    """
    san_list = list(sans) if sans else [f"*.{domain}"]
    needs = [domain] + san_list

    _step("Looking for an existing matching certificate")
    existing = _find_existing(needs, profile)
    if existing and existing["Status"] == "ISSUED":
        _skip(f"already ISSUED: {existing['CertificateArn']}")
        return existing["CertificateArn"]

    if existing:
        cert_arn = existing["CertificateArn"]
        _skip(f"reusing PENDING_VALIDATION cert: {cert_arn}")
    else:
        _step(f"Requesting ACM cert for {domain} (SANs: {', '.join(san_list)})")
        cmd = [
            "acm", "request-certificate",
            "--domain-name", domain,
            "--validation-method", "DNS",
            "--idempotency-token", f"c{uuid.uuid4().hex[:16]}",
        ]
        if san_list:
            cmd += ["--subject-alternative-names", *san_list]
        result = run_aws(cmd, profile=profile, region=CERT_REGION, parse_json=True)
        cert_arn = result["CertificateArn"]
        _ok(f"requested: {cert_arn}")

    _step("Fetching DNS validation records from ACM")
    options = _wait_for_validation_records(cert_arn, profile)
    _ok(f"{len(options)} validation option(s) returned")

    _step(f"Writing validation CNAME(s) into Route53 zone for {domain}")
    zone_id = _find_hosted_zone(domain, profile)
    n = _write_validation_records(options, zone_id, profile)
    _ok(f"upserted {n} record(s) into zone {zone_id}")

    if wait:
        _step("Waiting for ACM to validate and issue (typically 1–5 min)")
        try:
            run_aws(
                ["acm", "wait", "certificate-validated", "--certificate-arn", cert_arn],
                profile=profile, region=CERT_REGION, capture=False,
            )
        except AwsError as e:
            raise click.ClickException(f"ACM validation did not complete: {e}")
        _ok("certificate ISSUED")

    return cert_arn


@click.group("cert", context_settings={"help_option_names": ["-h", "--help"]})
def cert() -> None:
    """ACM certificate tools (us-east-1, DNS-validated)."""


@cert.command("issue")
@click.argument("domain")
@click.option(
    "--san", "sans", multiple=True,
    help="Additional SAN(s). Default: '*.DOMAIN' (wildcard for single-label subdomains).",
)
@click.option("--wait/--no-wait", default=True, help="Wait until the certificate is ISSUED.")
@click.pass_context
def issue(ctx: click.Context, domain: str, sans: tuple[str, ...], wait: bool) -> None:
    """Request a DNS-validated ACM cert for DOMAIN (+ `*.DOMAIN` by default) and auto-validate via Route53."""
    profile = ctx.obj.get("profile")
    domain = domain.strip().lower().rstrip(".")

    click.secho(f"Domain:  {domain}", bold=True)
    click.echo(f"SANs:    {', '.join(sans) if sans else f'*.{domain}'}")
    click.echo(f"Region:  {CERT_REGION}")
    click.echo(f"Profile: {profile or '(default)'}")
    click.echo()

    cert_arn = ensure_certificate(domain, list(sans) or None, profile, wait=wait)
    click.echo()
    click.echo(cert_arn)
