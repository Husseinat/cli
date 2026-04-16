"""`c aws logs …` — cross-function CloudWatch Logs for serverless apps.

One Lambda = one log group (`/aws/lambda/<fn>`). AWS has no first-class "app"
grouping, so these commands treat a glob over log-group names as the app/env
selector and then fan out across the matched groups:

    c aws logs list   myapp-prod-*
    c aws logs tail   myapp-prod-*            # live, up to 10 groups
    c aws logs search myapp-prod-* -f ERROR   # history, up to 50 groups

PATTERN is a shell glob (`*`, `?`, `[…]`). If it doesn't start with `/` the
default prefix `/aws/lambda/` is prepended, so `myapp-prod-*` → all Lambda
log groups beginning with `myapp-prod-`. Provide a full path to target other
services (`/aws/apigateway/myapp-prod*`).
"""
from __future__ import annotations

import fnmatch
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import click

from c.aws.runner import AwsCliMissing, AwsError, _aws_path, run_aws

DEFAULT_PREFIX = "/aws/lambda/"
LIVE_TAIL_MAX_GROUPS = 10
INSIGHTS_MAX_GROUPS = 50


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _resolve_pattern(pattern: str) -> tuple[str, str]:
    """Return (describe-log-groups prefix, full glob) for PATTERN."""
    full = pattern if pattern.startswith("/") else DEFAULT_PREFIX + pattern
    for i, ch in enumerate(full):
        if ch in "*?[":
            return full[:i], full
    return full, full


def _discover_groups(
    pattern: str, profile: str | None, region: str | None
) -> list[dict[str, Any]]:
    prefix, glob = _resolve_pattern(pattern)
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        args = ["logs", "describe-log-groups", "--log-group-name-prefix", prefix]
        if token:
            args += ["--next-token", token]
        resp = run_aws(args, profile=profile, region=region, parse_json=True) or {}
        out.extend(resp.get("logGroups", []))
        token = resp.get("nextToken")
        if not token:
            break
    return [g for g in out if fnmatch.fnmatchcase(g["logGroupName"], glob)]


_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_time(s: str, *, now: datetime | None = None) -> datetime:
    """Accept '5m', '2h', '1d', '1w', '30s', or an ISO 8601 timestamp."""
    now = now or datetime.now(tz=timezone.utc)
    s = s.strip()
    if s and s[-1] in _UNIT_SECS and s[:-1].isdigit():
        return now - timedelta(seconds=int(s[:-1]) * _UNIT_SECS[s[-1]])
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise click.BadParameter(
            f"{s!r} is not a duration (e.g. '5m', '2h') or ISO 8601 timestamp"
        ) from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _short(name: str) -> str:
    return name[len(DEFAULT_PREFIX):] if name.startswith(DEFAULT_PREFIX) else name


def _arn(group: dict[str, Any]) -> str:
    # describe-log-groups returns an ARN with a trailing `:*`; live-tail rejects it.
    return (group.get("arn") or "").rstrip(":*")


# ─── Command group ───────────────────────────────────────────────────────────
@click.group("logs", context_settings={"help_option_names": ["-h", "--help"]})
def logs() -> None:
    """CloudWatch Logs tools — cross-function tail and search."""


# ─── list ────────────────────────────────────────────────────────────────────
@logs.command("list")
@click.argument("pattern", default="*")
@click.pass_context
def list_cmd(ctx: click.Context, pattern: str) -> None:
    """List log groups matching PATTERN (glob). Default lists all Lambda groups."""
    profile = ctx.obj.get("profile")
    region = ctx.obj.get("region")

    groups = _discover_groups(pattern, profile, region)
    if not groups:
        click.secho(f"  (no log groups match {pattern!r})", fg="yellow")
        return

    groups.sort(key=lambda g: g["logGroupName"])
    width = max(len(_short(g["logGroupName"])) for g in groups)
    for g in groups:
        last = g.get("lastEventTime") or g.get("creationTime") or 0
        when = (
            datetime.fromtimestamp(last / 1000, tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
            if last
            else "—"
        )
        size = _human_size(g.get("storedBytes", 0))
        click.echo(
            f"  {_short(g['logGroupName']):<{width}}  {size:>10}  last: {when}"
        )
    click.echo()
    click.secho(f"  {len(groups)} group(s)", fg="cyan")


# ─── tail ────────────────────────────────────────────────────────────────────
@logs.command("tail")
@click.argument("pattern")
@click.option(
    "--filter",
    "-f",
    "filter_pattern",
    default=None,
    help="CloudWatch filter pattern (e.g. 'ERROR', '{ $.level = \"error\" }').",
)
@click.option(
    "--list",
    "-l",
    "list_only",
    is_flag=True,
    help="Print the matched groups and exit without tailing.",
)
@click.pass_context
def tail_cmd(
    ctx: click.Context, pattern: str, filter_pattern: str | None, list_only: bool
) -> None:
    """Live-tail every log group matching PATTERN (up to 10)."""
    profile = ctx.obj.get("profile")
    region = ctx.obj.get("region")

    groups = _discover_groups(pattern, profile, region)
    if not groups:
        click.secho(f"✗ no log groups match {pattern!r}", fg="red", err=True)
        sys.exit(1)

    if list_only:
        for g in groups:
            click.echo(f"  {g['logGroupName']}")
        click.secho(f"  {len(groups)} group(s)", fg="cyan")
        return

    if len(groups) > LIVE_TAIL_MAX_GROUPS:
        click.secho(
            f"✗ {len(groups)} groups match; StartLiveTail accepts at most "
            f"{LIVE_TAIL_MAX_GROUPS}. Refine the pattern or use `c aws logs search`.",
            fg="red",
            err=True,
        )
        sys.exit(1)

    arns = [_arn(g) for g in groups]
    labels = {arn: _short(g["logGroupName"]) for arn, g in zip(arns, groups)}
    width = max(len(v) for v in labels.values())

    cmd = [_aws_path()]
    if profile:
        cmd += ["--profile", profile]
    if region:
        cmd += ["--region", region]
    cmd += [
        "logs",
        "start-live-tail",
        "--log-group-identifiers",
        *arns,
        "--mode",
        "print-only",
    ]
    if filter_pattern:
        cmd += ["--log-event-filter-pattern", filter_pattern]

    click.secho(
        f"  ▶ tailing {len(groups)} group(s){' — filter: ' + filter_pattern if filter_pattern else ''} (Ctrl-C to stop)",
        fg="cyan",
    )
    for g in groups:
        click.echo(f"    • {_short(g['logGroupName'])}")
    click.echo()

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except FileNotFoundError as e:
        raise AwsCliMissing(str(e))

    interrupted = False
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                click.echo(line)
                continue
            update = evt.get("sessionUpdate")
            if not update:
                continue
            for r in update.get("sessionResults", []) or []:
                ts = datetime.fromtimestamp(
                    r["timestamp"] / 1000, tz=timezone.utc
                ).astimezone()
                label = labels.get(r.get("logGroupIdentifier", ""), "?").ljust(width)
                msg = (r.get("message") or "").rstrip()
                click.echo(
                    f"  {ts.strftime('%H:%M:%S.%f')[:-3]}  "
                    f"{click.style(label, fg='cyan')}  {msg}"
                )
    except KeyboardInterrupt:
        interrupted = True
        proc.terminate()
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not interrupted and proc.returncode not in (0, None):
        err = ((proc.stderr.read() if proc.stderr else "") or "").strip()
        if err:
            click.secho(f"✗ {err}", fg="red", err=True)
        sys.exit(proc.returncode)


# ─── search ──────────────────────────────────────────────────────────────────
@logs.command("search")
@click.argument("pattern")
@click.option(
    "--since",
    "-s",
    default="1h",
    help="Start time — duration ('5m', '2h', '1d', '1w') or ISO 8601.",
)
@click.option("--until", default=None, help="End time (default: now).")
@click.option(
    "--filter",
    "-f",
    "filter_expr",
    default=None,
    help="Substring to match in @message (wraps in Insights `like \"…\"`).",
)
@click.option(
    "--query",
    "-q",
    default=None,
    help="Raw CloudWatch Logs Insights query (overrides --filter).",
)
@click.option("--limit", default=100, show_default=True, type=int)
@click.pass_context
def search_cmd(
    ctx: click.Context,
    pattern: str,
    since: str,
    until: str | None,
    filter_expr: str | None,
    query: str | None,
    limit: int,
) -> None:
    """Historical search across log groups matching PATTERN (up to 50)."""
    profile = ctx.obj.get("profile")
    region = ctx.obj.get("region")

    groups = _discover_groups(pattern, profile, region)
    if not groups:
        click.secho(f"✗ no log groups match {pattern!r}", fg="red", err=True)
        sys.exit(1)
    if len(groups) > INSIGHTS_MAX_GROUPS:
        click.secho(
            f"✗ {len(groups)} groups match; Logs Insights accepts at most "
            f"{INSIGHTS_MAX_GROUPS}. Refine the pattern.",
            fg="red",
            err=True,
        )
        sys.exit(1)

    start_dt = _parse_time(since)
    end_dt = _parse_time(until) if until else datetime.now(tz=timezone.utc)

    if query:
        q = query
    else:
        parts = ["fields @timestamp, @log, @message"]
        if filter_expr:
            escaped = filter_expr.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'| filter @message like "{escaped}"')
        parts += ["| sort @timestamp desc", f"| limit {limit}"]
        q = " ".join(parts)

    arns = [_arn(g) for g in groups]
    start = run_aws(
        [
            "logs",
            "start-query",
            "--start-time",
            str(int(start_dt.timestamp())),
            "--end-time",
            str(int(end_dt.timestamp())),
            "--log-group-identifiers",
            *arns,
            "--query-string",
            q,
        ],
        profile=profile,
        region=region,
        parse_json=True,
    )
    query_id = start["queryId"]

    click.secho(
        f"  ▶ insights query {query_id} — {len(groups)} group(s), "
        f"{start_dt.astimezone():%Y-%m-%d %H:%M} → "
        f"{end_dt.astimezone():%Y-%m-%d %H:%M}",
        fg="cyan",
    )

    status = "Running"
    result: dict[str, Any] = {}
    while status in ("Running", "Scheduled"):
        time.sleep(0.5)
        result = run_aws(
            ["logs", "get-query-results", "--query-id", query_id],
            profile=profile,
            region=region,
            parse_json=True,
        )
        status = result.get("status", "Unknown")

    if status != "Complete":
        click.secho(f"✗ query {status.lower()}", fg="red", err=True)
        sys.exit(1)

    rows = [{c["field"]: c["value"] for c in row} for row in result.get("results", [])]
    if not rows:
        click.secho("  (no matches)", fg="yellow")
        return

    # `@log` is "<account>:<logGroupName>" — strip to just the function name.
    def _label(row: dict[str, str]) -> str:
        log = row.get("@log", "")
        name = log.split(":", 1)[1] if ":" in log else log
        return _short(name)

    width = min(40, max(len(_label(r)) for r in rows))
    for row in rows:
        ts = row.get("@timestamp", "")
        msg = (row.get("@message") or "").rstrip()
        click.echo(
            f"  {ts}  {click.style(_label(row).ljust(width)[:width], fg='cyan')}  {msg}"
        )

    stats = result.get("statistics", {}) or {}
    click.echo()
    click.secho(
        f"  {len(rows)} record(s) — scanned "
        f"{int(stats.get('recordsScanned', 0)):,} records "
        f"({_human_size(int(stats.get('bytesScanned', 0)))})",
        fg="cyan",
    )
