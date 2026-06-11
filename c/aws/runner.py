"""Thin wrapper around the local `aws` binary.

Everything goes through `run_aws()` so profile/region propagation, JSON decoding,
and error formatting live in one place.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any, Sequence


class AwsCliMissing(RuntimeError):
    """The `aws` binary is not on PATH."""


class AwsError(RuntimeError):
    """An `aws` call returned a non-zero exit code."""

    def __init__(self, cmd: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        rendered = " ".join(cmd)
        msg = stderr.strip() or stdout.strip() or "(no output)"
        super().__init__(f"`{rendered}` exited {returncode}: {msg}")


def _aws_path() -> str:
    path = shutil.which("aws")
    if not path:
        hint = (
            "`brew install awscli`" if sys.platform == "darwin"
            else "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
        )
        raise AwsCliMissing(f"The `aws` cli was not found on PATH. Install it: {hint}")
    return path


def run_aws(
    args: Sequence[str],
    *,
    profile: str | None = None,
    region: str | None = None,
    parse_json: bool = False,
    check: bool = True,
    capture: bool = True,
) -> Any:
    """Invoke `aws <args>`. Returns stdout (str), parsed JSON, or the CompletedProcess.

    - If `parse_json` is True, adds `--output json` and returns the decoded body
      (empty string → None).
    - If `check` is True, raises `AwsError` on non-zero exit.
    - If `capture` is False, stdout/stderr stream straight to the terminal and
      this function returns the `CompletedProcess`.
    """
    cmd: list[str] = [_aws_path()]
    if profile:
        cmd += ["--profile", profile]
    if region:
        cmd += ["--region", region]
    cmd += list(args)
    if parse_json and "--output" not in cmd:
        cmd += ["--output", "json"]

    if capture:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    else:
        proc = subprocess.run(cmd, check=False)

    if check and proc.returncode != 0:
        raise AwsError(
            cmd=cmd,
            returncode=proc.returncode,
            stdout=getattr(proc, "stdout", "") or "",
            stderr=getattr(proc, "stderr", "") or "",
        )

    if not capture:
        return proc
    if parse_json:
        out = (proc.stdout or "").strip()
        return json.loads(out) if out else None
    return proc.stdout
