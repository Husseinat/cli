import click

from c.godaddy.configure import configure
from c.godaddy.set_ns import set_ns


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--profile", envvar="AWS_PROFILE", default=None,
    help="AWS profile (used when reading nameservers from Route53).",
)
@click.option(
    "--region", envvar="AWS_REGION", default=None, help="AWS region (rarely needed here).",
)
@click.pass_context
def godaddy(ctx: click.Context, profile: str | None, region: str | None) -> None:
    """GoDaddy tools. Requires GODADDY_API_KEY + GODADDY_API_SECRET in your env."""
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile
    ctx.obj["region"] = region


godaddy.add_command(configure)
godaddy.add_command(set_ns)
