import click

from c.aws.cert import cert
from c.aws.check import check
from c.aws.static_site import static_site
from c.aws.zone import zone


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--profile",
    envvar="AWS_PROFILE",
    default=None,
    help="AWS profile to use (falls back to $AWS_PROFILE, then the default profile).",
)
@click.option(
    "--region",
    envvar="AWS_REGION",
    default=None,
    help="AWS region to use (falls back to $AWS_REGION, then the profile default).",
)
@click.pass_context
def aws(ctx: click.Context, profile: str | None, region: str | None) -> None:
    """AWS tools — wraps the local `aws` cli."""
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile
    ctx.obj["region"] = region


aws.add_command(check)
aws.add_command(cert)
aws.add_command(zone)
aws.add_command(static_site)
