import shutil
import subprocess
import sys

import click

from c.aws.runner import AwsCliMissing, AwsError, run_aws


@click.command("check")
@click.pass_context
def check(ctx: click.Context) -> None:
    """Verify the local aws cli is installed and the profile works."""
    profile = ctx.obj.get("profile")
    region = ctx.obj.get("region")

    aws_path = shutil.which("aws")
    if not aws_path:
        click.secho("✗ aws cli not found on PATH", fg="red", err=True)
        if sys.platform == "darwin":
            click.echo("  Install: brew install awscli", err=True)
        else:
            click.echo(
                "  Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
                err=True,
            )
        sys.exit(1)

    try:
        v = subprocess.run(
            [aws_path, "--version"], capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        click.secho(f"✗ `aws --version` failed: {e}", fg="red", err=True)
        sys.exit(1)
    version = (v.stdout or "").strip() or (v.stderr or "").strip()

    click.secho(f"✓ {version}", fg="green")
    click.echo(f"  path:    {aws_path}")
    click.echo(f"  profile: {profile or '(default)'}")
    if region:
        click.echo(f"  region:  {region}")

    try:
        identity = run_aws(
            ["sts", "get-caller-identity"],
            profile=profile,
            region=region,
            parse_json=True,
        )
    except AwsCliMissing as e:
        click.secho(f"✗ {e}", fg="red", err=True)
        sys.exit(1)
    except AwsError as e:
        click.secho("✗ credentials check failed", fg="red", err=True)
        click.echo(f"  {e}", err=True)
        sys.exit(1)

    click.secho("✓ credentials ok", fg="green")
    click.echo(f"  account: {identity['Account']}")
    click.echo(f"  arn:     {identity['Arn']}")
