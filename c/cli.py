import click

from c import __version__
from c.aws.cli import aws
from c.godaddy.cli import godaddy


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="c")
def main() -> None:
    """c — personal CLI toolbox."""


main.add_command(aws)
main.add_command(godaddy)


if __name__ == "__main__":
    main()
