import click

from c import __version__
from c.aws.cli import aws
from c.godaddy.cli import godaddy
from c.mcp.cli import mcp_cmd, setup_cmd


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="c")
def main() -> None:
    """c — personal CLI toolbox."""


main.add_command(aws)
main.add_command(godaddy)
main.add_command(mcp_cmd)
main.add_command(setup_cmd)


if __name__ == "__main__":
    main()
