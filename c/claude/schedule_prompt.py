"""`c schedule-prompt` — block until a time, then open Claude Code with a prompt.

    c schedule-prompt 5h "Refactor the game loop" -C ~/dev/farming-game
    c schedule-prompt 03:00 "Nightly deep-research pass" --model fable --ultracode
    c schedule-prompt 2h30m "Fix the flaky tests" -p --permission-mode acceptEdits

WHEN is a duration ('30s', '5m', '2h', '1d', compounds like '2h30m'), a
wall-clock time ('03:00' — today, or tomorrow if already past), an ISO 8601
timestamp, or 'now'. The command blocks (Ctrl-C cancels), then replaces itself
with `claude` running in PATH — interactive by default, headless with -p.

Model and effort pass straight through to `claude --model` / `claude --effort`.
Ultracode is *not* an effort level — it is a per-session Claude Code setting
(sends xhigh and orchestrates dynamic workflows), so --ultracode is forwarded
as `--settings '{"ultracode": true}'`.

The wait recomputes the remaining time from the wall clock on every tick, so a
laptop that suspends mid-wait fires as soon as it wakes. On macOS the wait
additionally holds a `caffeinate -i` assertion so the machine doesn't
idle-sleep past the scheduled time (Linux: keep the machine awake yourself,
e.g. `systemd-inhibit c schedule-prompt …`).
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta

import click

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
)

_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_RE = re.compile(r"^(?:\d+[smhdw])+$")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _parse_when(s: str, *, now: datetime | None = None) -> datetime:
    """'now', a duration ('5m', '2h30m'), 'HH:MM[:SS]', or ISO 8601 → aware dt."""
    now = now or datetime.now().astimezone()
    s = s.strip()
    if s.lower() == "now":
        return now
    if _DURATION_RE.match(s):
        secs = sum(
            int(n) * _UNIT_SECS[u] for n, u in re.findall(r"(\d+)([smhdw])", s)
        )
        return now + timedelta(seconds=secs)
    m = _CLOCK_RE.match(s)
    if m:
        hh, mm, ss = int(m[1]), int(m[2]), int(m[3] or 0)
        if hh > 23 or mm > 59 or ss > 59:
            raise click.BadParameter(f"{s!r} is not a valid time of day")
        target = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    try:
        target = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise click.BadParameter(
            f"{s!r} is not a duration ('5m', '2h30m'), a time ('03:00'), "
            "an ISO 8601 timestamp, or 'now'"
        ) from e
    return target.astimezone() if target.tzinfo else target.astimezone()


def _human_delta(seconds: float) -> str:
    secs = max(0, int(seconds))
    parts: list[str] = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        n, secs = divmod(secs, size)
        if n or (unit == "s" and not parts):
            parts.append(f"{n}{unit}")
    return " ".join(parts)


def _keep_awake() -> subprocess.Popen | None:
    """On macOS, hold a `caffeinate -i` assertion for the life of this pid.

    Best-effort: returns None on other platforms or if caffeinate is missing.
    """
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        return None
    try:
        return subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def _wait_until(target: datetime) -> None:
    """Block until TARGET, recomputing from the wall clock (suspend-safe)."""
    tty = sys.stderr.isatty()
    while True:
        remaining = (target - datetime.now().astimezone()).total_seconds()
        if remaining <= 0:
            break
        if tty:
            click.echo(
                f"\r  ⏳ unblocks in {_human_delta(remaining)}        ",
                nl=False,
                err=True,
            )
        time.sleep(min(remaining, 1 if tty else 60))
    if tty:
        click.echo("\r" + " " * 50 + "\r", nl=False, err=True)


# ─── Command ─────────────────────────────────────────────────────────────────
@click.command(
    "schedule-prompt", context_settings={"help_option_names": ["-h", "--help"]}
)
@click.argument("when")
@click.argument("prompt")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--path",
    "-C",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="Directory to open Claude Code in.  [default: current directory]",
)
@click.option(
    "--model",
    "-m",
    default=None,
    help="Model — an alias ('fable', 'opus', 'sonnet', 'haiku') or a full "
    "name ('claude-fable-5'). Passed to `claude --model`.",
)
@click.option(
    "--effort",
    "-e",
    type=click.Choice(EFFORT_LEVELS + ("ultracode",)),
    default=None,
    help="Effort level, passed to `claude --effort`. 'ultracode' is accepted "
    "as shorthand for --ultracode (it is a setting, not an effort level).",
)
@click.option(
    "--ultracode",
    is_flag=True,
    help="Enable ultracode for the session — forwarded as "
    "`--settings '{\"ultracode\": true}'`. Sends xhigh effort and has Claude "
    "orchestrate dynamic workflows. Mutually exclusive with --effort.",
)
@click.option(
    "--headless",
    "-p",
    is_flag=True,
    help="Run non-interactively (`claude -p`) and print the result instead of "
    "opening the UI. Redirect output yourself if you want a log file.",
)
@click.option(
    "--permission-mode",
    type=click.Choice(PERMISSION_MODES),
    default=None,
    help="Permission mode for the session (passed to `claude "
    "--permission-mode`). 'auto' is autopilot — the classifier "
    "approves/denies tool calls on its own; best for unattended runs.",
)
@click.option(
    "--claude-bin",
    default=None,
    help="Path to the claude binary.  [default: found on PATH]",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the schedule and the claude command, then exit without waiting.",
)
def schedule_prompt(
    when: str,
    prompt: str,
    claude_args: tuple[str, ...],
    path: str,
    model: str | None,
    effort: str | None,
    ultracode: bool,
    headless: bool,
    permission_mode: str | None,
    claude_bin: str | None,
    dry_run: bool,
) -> None:
    """Block until WHEN, then open Claude Code in --path with PROMPT.

    Extra arguments after `--` are passed through to claude verbatim:

        c schedule-prompt 1h "fix CI" -- --allowed-tools "Bash,Edit,Read"
    """
    if effort == "ultracode":
        effort, ultracode = None, True
    if effort and ultracode:
        raise click.UsageError(
            "--effort and --ultracode are mutually exclusive "
            "(ultracode always runs at xhigh)."
        )

    target = _parse_when(when)

    binary = claude_bin or shutil.which("claude")
    if not binary:
        click.secho(
            "✗ claude not found on PATH (install Claude Code, or pass --claude-bin)",
            fg="red",
            err=True,
        )
        sys.exit(1)
    binary = os.path.abspath(binary)
    workdir = os.path.abspath(path)

    argv = [binary]
    if headless:
        argv.append("-p")
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if ultracode:
        argv += ["--settings", '{"ultracode": true}']
    if permission_mode:
        argv += ["--permission-mode", permission_mode]
    argv += list(claude_args)
    argv.append(prompt)

    now = datetime.now().astimezone()
    delta = _human_delta((target - now).total_seconds())
    click.secho(
        f"  ▶ scheduled for {target:%Y-%m-%d %H:%M:%S %Z} (in {delta})",
        fg="cyan",
        err=True,
    )
    click.echo(f"    cwd: {workdir}", err=True)
    click.echo(f"    cmd: {shlex.join(argv)}", err=True)

    if dry_run:
        return

    awake = _keep_awake()
    try:
        _wait_until(target)
    except KeyboardInterrupt:
        click.secho("\n✗ cancelled", fg="red", err=True)
        sys.exit(130)
    finally:
        if awake:
            awake.terminate()

    os.chdir(workdir)
    os.execv(binary, argv)
