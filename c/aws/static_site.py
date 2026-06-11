"""`c aws static-site DOMAIN` — provision a 2x-CloudFront static site, end to end.

Each resource is created with an explicit `aws` cli call. Every step first
checks whether the target resource already exists; if so it is left alone and
the step is skipped. Re-running the command is therefore idempotent: partial
failures can be resumed just by invoking it again.

Resources (in order):
    1. Route53 public hosted zone (created if missing).
    2. Registrar delegation: GoDaddy nameservers → Route53 (best-effort; if the
       domain isn't registered with GoDaddy or no credentials are configured,
       the nameservers are printed for manual setup and provisioning continues).
    3. ACM certificate in us-east-1 covering DOMAIN + *.DOMAIN, DNS-validated
       via Route53 (reused if one already covers DOMAIN and www.DOMAIN).
    4. S3 bucket for root content (private, served via CloudFront + OAC).
    5. S3 bucket for www (configured as S3 website redirect to https://root).
    6. CloudFront Origin Access Control.
    7. CloudFront distribution for the root alias.
    8. Root bucket policy (allow CloudFront SourceArn to GetObject).
    9. CloudFront distribution for the www alias.
   10. Route53 A + AAAA alias records (UPSERT) for root and www.

Pre-flight:
    - aws cli installed + credentials work.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid

import click

from c.aws.cert import ensure_certificate
from c.aws.runner import AwsCliMissing, AwsError, run_aws
from c.aws.zone import ensure_zone
from c.godaddy.api import GoDaddyError
from c.godaddy.set_ns import ensure_godaddy_ns

# ─── Constants ───────────────────────────────────────────────────────────────
CERT_REGION = "us-east-1"
CF_HOSTED_ZONE_ID = "Z2FDTNDATAQYW2"  # Fixed zone id for all CloudFront aliases.
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"  # Managed cache policy.

# S3 website endpoint URL uses a dash in these older regions, a dot in newer ones.
# https://docs.aws.amazon.com/AmazonS3/latest/userguide/WebsiteEndpoints.html
DASH_WEBSITE_REGIONS = {
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "ap-south-1", "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-southeast-1", "ap-southeast-2", "ca-central-1",
    "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "eu-north-1",
    "sa-east-1",
}


# ─── CLI output helpers ──────────────────────────────────────────────────────
def _step(msg: str) -> None:
    click.secho(f"→ {msg}", fg="cyan")


def _ok(msg: str) -> None:
    click.secho(f"  ✓ {msg}", fg="green")


def _skip(msg: str) -> None:
    click.secho(f"  ↷ {msg}", fg="yellow")


def _fail(msg: str) -> click.ClickException:
    return click.ClickException(msg)


# ─── DNS: hosted zone + registrar delegation ─────────────────────────────────
def _cert_covers(pattern: str, host: str) -> bool:
    pattern = pattern.lower().rstrip(".")
    host = host.lower().rstrip(".")
    if pattern == host:
        return True
    if pattern.startswith("*."):
        suffix = pattern[2:]
        labels = host.split(".")
        return len(labels) >= 2 and ".".join(labels[1:]) == suffix
    return False


def _ensure_delegation(domain: str, nameservers: list[str], *, godaddy: bool) -> None:
    """Best-effort: point the GoDaddy-registered domain at the Route53 nameservers.

    Failures (no credentials, domain not in the GoDaddy account, API errors)
    don't stop provisioning — the nameservers are printed for manual setup.
    """
    if not godaddy:
        _skip("GoDaddy delegation disabled (--no-godaddy); make sure your registrar points at:")
        for n in nameservers:
            click.echo(f"      {n}")
        return
    try:
        updated = ensure_godaddy_ns(domain, nameservers)
    except GoDaddyError as e:
        _skip(f"could not update GoDaddy nameservers: {e}")
        click.echo("    Point your registrar at these nameservers manually:")
        for n in nameservers:
            click.echo(f"      {n}")
        return
    if updated:
        _ok("GoDaddy nameservers now point at Route53 (propagation can take a while)")
    else:
        _skip("GoDaddy nameservers already point at Route53")


def _find_issued_certificate(domain: str, profile: str | None) -> str | None:
    """Return an ISSUED cert ARN covering `domain` + `www.domain`, or None."""
    www = f"www.{domain}"
    summaries = run_aws(
        ["acm", "list-certificates", "--certificate-statuses", "ISSUED"],
        profile=profile, region=CERT_REGION, parse_json=True,
    ).get("CertificateSummaryList", [])
    for s in summaries:
        arn = s["CertificateArn"]
        cert = run_aws(
            ["acm", "describe-certificate", "--certificate-arn", arn],
            profile=profile, region=CERT_REGION, parse_json=True,
        )["Certificate"]
        names = {cert.get("DomainName", "")} | set(cert.get("SubjectAlternativeNames") or [])
        if any(_cert_covers(n, domain) for n in names) and any(_cert_covers(n, www) for n in names):
            return arn
    return None


# ─── S3 buckets ──────────────────────────────────────────────────────────────
def _bucket_state(bucket: str, profile: str | None) -> str:
    """'ours' | 'other' | 'missing'."""
    try:
        run_aws(["s3api", "head-bucket", "--bucket", bucket], profile=profile)
        return "ours"
    except AwsError as e:
        blob = (e.stderr + " " + e.stdout).lower()
        if "404" in blob or "not found" in blob or "nosuchbucket" in blob:
            return "missing"
        if "403" in blob or "forbidden" in blob:
            return "other"
        raise _fail(f"Could not check bucket '{bucket}': {e}")


def _ensure_root_bucket(domain: str, profile: str | None, region: str) -> None:
    state = _bucket_state(domain, profile)
    if state == "other":
        raise _fail(f"Bucket '{domain}' already exists in another AWS account.")
    if state == "ours":
        _skip(f"s3://{domain} already exists")
    else:
        cmd = ["s3api", "create-bucket", "--bucket", domain]
        if region != "us-east-1":
            cmd += ["--create-bucket-configuration", f"LocationConstraint={region}"]
        run_aws(cmd, profile=profile, region=region)
        _ok(f"created s3://{domain}")

    run_aws([
        "s3api", "put-bucket-ownership-controls", "--bucket", domain,
        "--ownership-controls", "Rules=[{ObjectOwnership=BucketOwnerEnforced}]",
    ], profile=profile)
    run_aws([
        "s3api", "put-public-access-block", "--bucket", domain,
        "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,"
        "BlockPublicPolicy=true,RestrictPublicBuckets=true",
    ], profile=profile)
    _ok("root bucket hardened (ownership + public-access-block)")


def _ensure_www_bucket(domain: str, profile: str | None, region: str) -> None:
    bucket = f"www.{domain}"
    state = _bucket_state(bucket, profile)
    if state == "other":
        raise _fail(f"Bucket '{bucket}' already exists in another AWS account.")
    if state == "ours":
        _skip(f"s3://{bucket} already exists")
    else:
        cmd = ["s3api", "create-bucket", "--bucket", bucket]
        if region != "us-east-1":
            cmd += ["--create-bucket-configuration", f"LocationConstraint={region}"]
        run_aws(cmd, profile=profile, region=region)
        _ok(f"created s3://{bucket}")

    run_aws([
        "s3api", "put-bucket-ownership-controls", "--bucket", bucket,
        "--ownership-controls", "Rules=[{ObjectOwnership=BucketOwnerEnforced}]",
    ], profile=profile)
    website = {"RedirectAllRequestsTo": {"HostName": domain, "Protocol": "https"}}
    run_aws([
        "s3api", "put-bucket-website", "--bucket", bucket,
        "--website-configuration", json.dumps(website),
    ], profile=profile)
    _ok(f"www redirect → https://{domain}")


# ─── CloudFront OAC ──────────────────────────────────────────────────────────
def _ensure_oac(domain: str, profile: str | None) -> str:
    name = f"{domain}-oac"
    result = run_aws(
        ["cloudfront", "list-origin-access-controls"], profile=profile, parse_json=True,
    )
    for item in (result.get("OriginAccessControlList", {}) or {}).get("Items", []) or []:
        if item.get("Name") == name:
            _skip(f"OAC '{name}' already exists ({item['Id']})")
            return item["Id"]

    config = {
        "Name": name,
        "Description": f"OAC for {domain}",
        "OriginAccessControlOriginType": "s3",
        "SigningBehavior": "always",
        "SigningProtocol": "sigv4",
    }
    result = run_aws([
        "cloudfront", "create-origin-access-control",
        "--origin-access-control-config", json.dumps(config),
    ], profile=profile, parse_json=True)
    oac_id = result["OriginAccessControl"]["Id"]
    _ok(f"created OAC {oac_id}")
    return oac_id


# ─── CloudFront distribution builders ────────────────────────────────────────
def _viewer_certificate(cert_arn: str) -> dict:
    return {
        "CloudFrontDefaultCertificate": False,
        "ACMCertificateArn": cert_arn,
        "SSLSupportMethod": "sni-only",
        "MinimumProtocolVersion": "TLSv1.2_2021",
        "Certificate": cert_arn,
        "CertificateSource": "acm",
    }


def _default_cache_behavior(target_id: str) -> dict:
    return {
        "TargetOriginId": target_id,
        "TrustedSigners": {"Enabled": False, "Quantity": 0},
        "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {
            "Quantity": 2,
            "Items": ["GET", "HEAD"],
            "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
        },
        "SmoothStreaming": False,
        "Compress": True,
        "LambdaFunctionAssociations": {"Quantity": 0},
        "FunctionAssociations": {"Quantity": 0},
        "FieldLevelEncryptionId": "",
        "CachePolicyId": CACHING_OPTIMIZED,
    }


def _dist_config_root(domain: str, cert_arn: str, oac_id: str, region: str) -> dict:
    bucket_regional = f"{domain}.s3.{region}.amazonaws.com"
    return {
        "CallerReference": f"{domain}-root-{uuid.uuid4()}",
        "Aliases": {"Quantity": 1, "Items": [domain]},
        "DefaultRootObject": "index.html",
        "Origins": {
            "Quantity": 1,
            "Items": [{
                "Id": "S3Origin",
                "DomainName": bucket_regional,
                "OriginPath": "",
                "CustomHeaders": {"Quantity": 0},
                "S3OriginConfig": {"OriginAccessIdentity": ""},
                "OriginAccessControlId": oac_id,
                "ConnectionAttempts": 3,
                "ConnectionTimeout": 10,
            }],
        },
        "OriginGroups": {"Quantity": 0},
        "DefaultCacheBehavior": _default_cache_behavior("S3Origin"),
        "CacheBehaviors": {"Quantity": 0},
        "CustomErrorResponses": {"Quantity": 0},
        "Comment": f"c: {domain}",
        "Logging": {"Enabled": False, "IncludeCookies": False, "Bucket": "", "Prefix": ""},
        "PriceClass": "PriceClass_100",
        "Enabled": True,
        "ViewerCertificate": _viewer_certificate(cert_arn),
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "WebACLId": "",
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
    }


def _dist_config_www(domain: str, cert_arn: str, region: str) -> dict:
    bucket = f"www.{domain}"
    sep = "-" if region in DASH_WEBSITE_REGIONS else "."
    website_ep = f"{bucket}.s3-website{sep}{region}.amazonaws.com"
    return {
        "CallerReference": f"{domain}-www-{uuid.uuid4()}",
        "Aliases": {"Quantity": 1, "Items": [bucket]},
        "DefaultRootObject": "",
        "Origins": {
            "Quantity": 1,
            "Items": [{
                "Id": "S3WebsiteOrigin",
                "DomainName": website_ep,
                "OriginPath": "",
                "CustomHeaders": {"Quantity": 0},
                "CustomOriginConfig": {
                    "HTTPPort": 80,
                    "HTTPSPort": 443,
                    "OriginProtocolPolicy": "http-only",  # S3 website endpoints don't speak HTTPS.
                    "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                    "OriginReadTimeout": 30,
                    "OriginKeepaliveTimeout": 5,
                },
                "ConnectionAttempts": 3,
                "ConnectionTimeout": 10,
            }],
        },
        "OriginGroups": {"Quantity": 0},
        "DefaultCacheBehavior": _default_cache_behavior("S3WebsiteOrigin"),
        "CacheBehaviors": {"Quantity": 0},
        "CustomErrorResponses": {"Quantity": 0},
        "Comment": f"c: www.{domain} redirect",
        "Logging": {"Enabled": False, "IncludeCookies": False, "Bucket": "", "Prefix": ""},
        "PriceClass": "PriceClass_100",
        "Enabled": True,
        "ViewerCertificate": _viewer_certificate(cert_arn),
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "WebACLId": "",
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
    }


# ─── CloudFront distribution existence + create ──────────────────────────────
def _find_distribution_by_alias(alias: str, profile: str | None) -> dict | None:
    data = run_aws(["cloudfront", "list-distributions"], profile=profile, parse_json=True)
    for d in (data.get("DistributionList", {}) or {}).get("Items") or []:
        if alias in ((d.get("Aliases") or {}).get("Items") or []):
            return d
    return None


def _create_distribution(config: dict, profile: str | None) -> dict:
    # --distribution-config accepts file:// — avoid shell-escaping the JSON body.
    fd, path = tempfile.mkstemp(suffix=".json", prefix="c-dist-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
        result = run_aws([
            "cloudfront", "create-distribution",
            "--distribution-config", f"file://{path}",
        ], profile=profile, parse_json=True)
    finally:
        os.unlink(path)
    return result["Distribution"]


def _ensure_distribution(alias: str, config: dict, profile: str | None, *, label: str) -> dict:
    existing = _find_distribution_by_alias(alias, profile)
    if existing:
        _skip(f"{label} distribution already exists ({existing['Id']})")
        return {
            "Id": existing["Id"],
            "DomainName": existing["DomainName"],
            "ARN": existing["ARN"],
        }
    d = _create_distribution(config, profile)
    _ok(f"created {label} distribution {d['Id']}")
    return {"Id": d["Id"], "DomainName": d["DomainName"], "ARN": d["ARN"]}


# ─── Root bucket policy (CloudFront OAC → S3) ───────────────────────────────
def _ensure_root_bucket_policy(domain: str, root_dist_arn: str, profile: str | None) -> None:
    desired = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowCloudFrontOACRead",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{domain}/*",
            "Condition": {"StringEquals": {"AWS:SourceArn": root_dist_arn}},
        }],
    }

    try:
        current = run_aws(
            ["s3api", "get-bucket-policy", "--bucket", domain],
            profile=profile, parse_json=True,
        )
        current_doc = json.loads(current["Policy"]) if current and current.get("Policy") else None
    except AwsError as e:
        blob = (e.stderr + " " + e.stdout).lower()
        if "nosuchbucketpolicy" in blob or "no such bucket policy" in blob:
            current_doc = None
        else:
            raise

    if current_doc == desired:
        _skip("root bucket policy already up to date")
        return

    run_aws([
        "s3api", "put-bucket-policy", "--bucket", domain,
        "--policy", json.dumps(desired),
    ], profile=profile)
    _ok("root bucket policy applied (CloudFront OAC → s3:GetObject)")


# ─── Route53 alias records ───────────────────────────────────────────────────
def _ensure_alias_records(
    domain: str, zone_id: str, root_dns: str, www_dns: str, profile: str | None
) -> None:
    existing = run_aws(
        ["route53", "list-resource-record-sets", "--hosted-zone-id", zone_id],
        profile=profile, parse_json=True,
    ).get("ResourceRecordSets", [])

    def already_correct(name: str, rtype: str, target: str) -> bool:
        fqdn = name.rstrip(".") + "."
        for rr in existing:
            if rr["Name"] == fqdn and rr["Type"] == rtype:
                at = rr.get("AliasTarget") or {}
                if (
                    at.get("HostedZoneId") == CF_HOSTED_ZONE_ID
                    and at.get("DNSName", "").rstrip(".") == target.rstrip(".")
                ):
                    return True
        return False

    changes = []
    for name, target in [(domain, root_dns), (f"www.{domain}", www_dns)]:
        for rtype in ("A", "AAAA"):
            if already_correct(name, rtype, target):
                _skip(f"{rtype} alias {name} → {target} already set")
                continue
            changes.append({
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": name,
                    "Type": rtype,
                    "AliasTarget": {
                        "HostedZoneId": CF_HOSTED_ZONE_ID,
                        "DNSName": target,
                        "EvaluateTargetHealth": False,
                    },
                },
            })

    if not changes:
        return

    run_aws([
        "route53", "change-resource-record-sets",
        "--hosted-zone-id", zone_id,
        "--change-batch", json.dumps({"Changes": changes}),
    ], profile=profile)
    _ok(f"upserted {len(changes)} alias record(s)")


# ─── Waiters ─────────────────────────────────────────────────────────────────
def _wait_deployed(dist_id: str, profile: str | None, label: str) -> None:
    _step(f"waiting for {label} distribution {dist_id} to deploy (3–8 min)")
    run_aws(
        ["cloudfront", "wait", "distribution-deployed", "--id", dist_id],
        profile=profile, capture=False,
    )
    _ok(f"{label} distribution deployed")


# ─── Command ─────────────────────────────────────────────────────────────────
@click.command("static-site")
@click.argument("domain")
@click.option("--region", default=None, help="Region for the S3 buckets (default: us-east-1).")
@click.option(
    "--wait/--no-wait", default=True,
    help="Wait for CloudFront distributions to reach Deployed state.",
)
@click.option(
    "--godaddy/--no-godaddy", "godaddy", default=True,
    help="Point the domain's GoDaddy nameservers at the Route53 zone (best-effort).",
)
@click.pass_context
def static_site(
    ctx: click.Context, domain: str, region: str | None, wait: bool, godaddy: bool
) -> None:
    """Provision a static site end to end: Route53 zone + GoDaddy delegation +
    ACM cert + S3 + CloudFront (root + www redirect) + alias records."""
    domain = domain.strip().lower().rstrip(".")
    profile = ctx.obj.get("profile")
    region = region or ctx.obj.get("region") or "us-east-1"

    click.secho(f"Domain:   {domain}", bold=True)
    click.echo(f"Profile:  {profile or '(default)'}")
    click.echo(f"Region:   {region}  (ACM cert must be in {CERT_REGION})")
    click.echo()

    # ── Pre-flight ──
    _step("Checking aws cli and credentials")
    try:
        ident = run_aws(["sts", "get-caller-identity"], profile=profile, parse_json=True)
    except AwsCliMissing as e:
        raise _fail(str(e))
    except AwsError as e:
        raise _fail(f"Credentials not working: {e}")
    _ok(f"account {ident['Account']} ({ident['Arn']})")

    # ── DNS ──
    _step(f"Route53 hosted zone for {domain}")
    zone_id, nameservers, created = ensure_zone(domain, profile)
    if created:
        _ok(f"created hosted zone {zone_id}")
    else:
        _skip(f"hosted zone {zone_id} already exists")

    _step("Registrar delegation (GoDaddy → Route53)")
    _ensure_delegation(domain, nameservers, godaddy=godaddy)

    _step(f"Finding ACM cert covering {domain} and www.{domain} in {CERT_REGION}")
    cert_arn = _find_issued_certificate(domain, profile)
    if cert_arn:
        _ok(f"cert {cert_arn}")
    else:
        _skip(f"no matching ISSUED cert — provisioning one (DomainName={domain}, SAN=*.{domain})")
        cert_arn = ensure_certificate(domain, [f"*.{domain}"], profile, wait=True)
    click.echo()

    # ── Resources ──
    _step(f"S3 bucket: {domain}")
    _ensure_root_bucket(domain, profile, region)

    _step(f"S3 bucket: www.{domain} (website redirect)")
    _ensure_www_bucket(domain, profile, region)

    _step("CloudFront origin access control")
    oac_id = _ensure_oac(domain, profile)

    _step("CloudFront distribution: root")
    root = _ensure_distribution(
        domain, _dist_config_root(domain, cert_arn, oac_id, region),
        profile, label="root",
    )

    _step("Root bucket policy (CloudFront OAC → S3)")
    _ensure_root_bucket_policy(domain, root["ARN"], profile)

    _step("CloudFront distribution: www")
    www = _ensure_distribution(
        f"www.{domain}", _dist_config_www(domain, cert_arn, region),
        profile, label="www",
    )

    _step("Route53 alias records")
    _ensure_alias_records(domain, zone_id, root["DomainName"], www["DomainName"], profile)

    if wait:
        click.echo()
        _wait_deployed(root["Id"], profile, "root")
        _wait_deployed(www["Id"], profile, "www")

    # ── Summary ──
    click.echo()
    click.secho("✓ Done.", fg="green", bold=True)
    click.echo(f"  Content bucket:    s3://{domain}")
    click.echo(f"  Root distribution: {root['DomainName']}  ({root['Id']})")
    click.echo(f"  WWW  distribution: {www['DomainName']}  ({www['Id']})")
    click.echo()
    click.echo("Upload your site:")
    pfx = f" --profile {profile}" if profile else ""
    click.echo(f"  aws{pfx} s3 sync ./dist s3://{domain}/")
    click.echo(f"Then visit: https://{domain}")
