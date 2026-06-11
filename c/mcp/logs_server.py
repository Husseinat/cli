"""MCP server `logs` — AWS Lambda / CloudWatch Logs.

Wraps `c aws logs` so an MCP client can discover a serverless app's Lambda log
groups and search/iterate their logs. One Lambda = one log group
(`/aws/lambda/<fn>`); a shell glob over log-group names is the app/env selector.

Tools:
  * ``list_log_groups`` — discover Lambda log groups matching a glob.
  * ``search_logs``     — CloudWatch Logs Insights search across matched groups.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from c.aws.logs import _discover_groups, _human_size, search_log_groups

INSTRUCTIONS = """\
Tools for AWS Lambda / CloudWatch logs of a serverless application.

A `pattern` is a shell glob (`*`, `?`, `[…]`) over log-group names. Without a
leading `/` the prefix `/aws/lambda/` is added, so `myapp-prod-*` matches every
Lambda whose name starts with `myapp-prod-`. Use a full path (e.g.
`/aws/apigateway/myapp-*`) to target other services.

Typical flow: call `list_log_groups` to see what exists, then `search_logs` to
read function logs. To iterate further back in time, call `search_logs` again
with `until` set to the oldest `timestamp` from the previous result.
"""


def build() -> FastMCP:
    """Build the `c-logs` FastMCP server."""
    mcp = FastMCP("c-logs", instructions=INSTRUCTIONS)

    @mcp.tool()
    def list_log_groups(
        pattern: str = "*",
        profile: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        """List AWS Lambda (CloudWatch) log groups matching PATTERN.

        Args:
            pattern: Shell glob over log-group names. Defaults to all Lambda
                groups. Without a leading `/`, `/aws/lambda/` is prepended.
            profile: AWS profile name (falls back to the environment default).
            region: AWS region (falls back to the environment default).

        Returns a dict with `count` and a `groups` list, each entry carrying the
        log-group name, stored size, and last-event time (ms since epoch).
        """
        try:
            groups = _discover_groups(pattern, profile, region)
        except Exception as e:  # AwsError / AwsCliMissing / unexpected
            return {"error": str(e)}

        groups.sort(key=lambda g: g["logGroupName"])
        return {
            "count": len(groups),
            "groups": [
                {
                    "log_group": g["logGroupName"],
                    "stored_bytes": g.get("storedBytes", 0),
                    "stored_size": _human_size(g.get("storedBytes", 0)),
                    "last_event_ms": g.get("lastEventTime"),
                }
                for g in groups
            ],
        }

    @mcp.tool()
    def search_logs(
        pattern: str,
        since: str = "1h",
        until: str | None = None,
        filter: str | None = None,
        query: str | None = None,
        limit: int = 100,
        profile: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        """Search CloudWatch logs across every Lambda log group matching PATTERN.

        Runs one CloudWatch Logs Insights query fanned out over up to 50 matched
        log groups and returns matching log lines, newest-first.

        Args:
            pattern: Shell glob over log-group names (see `list_log_groups`).
            since: Start of the time window — a duration (`5m`, `2h`, `1d`, `1w`)
                or an ISO 8601 timestamp. Defaults to the last hour.
            until: End of the window (duration or ISO 8601). Defaults to now.
                To page further back, set this to the oldest `timestamp` you
                already received.
            filter: Substring to match in the log message (case-sensitive).
            query: A raw CloudWatch Logs Insights query. Overrides `filter` and
                the default `fields … | sort | limit` pipeline when given.
            limit: Maximum number of log lines to return (default 100).
            profile: AWS profile name (falls back to the environment default).
            region: AWS region (falls back to the environment default).

        Returns a dict with `groups`, the resolved `start`/`end`, a `rows` list
        (each: `timestamp`, `function`, `log_group`, `message`), and scan
        `stats`. On bad input or a failed query, returns `{"error": "..."}`.
        """
        try:
            return search_log_groups(
                pattern,
                since=since,
                until=until,
                filter_expr=filter,
                query=query,
                limit=limit,
                profile=profile,
                region=region,
            )
        except Exception as e:  # ValueError from core, AwsError, etc.
            return {"error": str(e)}

    return mcp
