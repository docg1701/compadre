"""Top-level `compadre-keys` Typer application.

Command groups are merged at the top level via `registered_commands.extend`
so operators invoke `compadre-keys create-customer` (flat) rather than
`compadre-keys customers create-customer` (nested).
"""

from __future__ import annotations

import typer

from compadre.keys.cli.commands.billing import app as billing_app
from compadre.keys.cli.commands.customers import app as customers_app
from compadre.keys.cli.commands.keys import app as keys_app
from compadre.keys.cli.commands.system import app as system_app

app = typer.Typer(
    name="compadre-keys",
    help="Compadre key registry — customer and API-key management CLI.",
    no_args_is_help=True,
    add_completion=False,
)
app.registered_commands.extend(customers_app.registered_commands)
app.registered_commands.extend(keys_app.registered_commands)
app.registered_commands.extend(billing_app.registered_commands)
app.registered_commands.extend(system_app.registered_commands)


@app.callback()
def _root() -> None:
    """Force Typer into multi-command mode even when only one command exists.

    Without a callback, Typer collapses a single-command app into that
    command's signature at the top level, which would break the spec's
    `compadre-keys create-customer` invocation pattern.
    """


def main() -> None:
    """Entry point registered in pyproject.toml [project.scripts]."""
    app()


if __name__ == "__main__":
    main()
