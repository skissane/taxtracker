"""GET a Fly Machines API path through the Machine's ``/.fly/api`` socket."""

import http.client
import json
import sys

import click

from lifetracker.core import fly_api


@click.command("fly-api")
@click.argument("path")
@click.option(
    "--socket",
    "socket_path",
    default=fly_api.SOCKET_PATH,
    show_default=True,
    help="The Machines API socket.",
)
def fly_api_command(path: str, socket_path: str) -> None:
    """GET PATH from the Fly Machines API, on a Fly Machine, without a token.

    {app} and {machine} in PATH become $FLY_APP_NAME and $FLY_MACHINE_ID, e.g.
    /v1/apps/{app}/machines. A JSON response is pretty-printed. Exits 1 if the
    status isn't 2xx.
    """
    try:
        path = fly_api.expand_path(path)
    except ValueError as e:
        raise click.UsageError(str(e)) from None
    try:
        response = fly_api.get(path, socket_path=socket_path)
    except (OSError, http.client.HTTPException) as e:
        raise click.ClickException(f"{socket_path}: {e!r}") from None
    body = response.body
    try:
        body = (json.dumps(json.loads(body), indent=2) + "\n").encode()
    except ValueError:
        pass
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()
    if not 200 <= response.status < 300:
        click.echo(f"HTTP {response.status} {response.reason}", err=True)
        sys.exit(1)
