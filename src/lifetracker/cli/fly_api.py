"""Call a Fly Machines API path through the Machine's ``/.fly/api`` socket."""

import http.client
import json
import sys

import click

from lifetracker.core import fly_api


def _parse_json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError as e:
        raise click.UsageError(f"--json isn't valid JSON: {e}") from None
    if not isinstance(value, dict):
        raise click.UsageError("--json must be a JSON object")
    return value


@click.command("fly-api")
@click.argument("path")
@click.option("--post", is_flag=True, help="POST instead of GET.")
@click.option(
    "--json",
    "json_text",
    metavar="OBJECT",
    help="Send this JSON object as the POST body (needs --post).",
)
@click.option(
    "--socket",
    "socket_path",
    default=fly_api.SOCKET_PATH,
    show_default=True,
    help="The Machines API socket.",
)
def fly_api_command(
    path: str, post: bool, json_text: str | None, socket_path: str
) -> None:
    """Call PATH on the Fly Machines API, on a Fly Machine, without a token.

    {app} and {machine} in PATH become $FLY_APP_NAME and $FLY_MACHINE_ID, e.g.
    /v1/apps/{app}/machines. It GETs PATH, or with --post POSTs to it, with an
    empty body, or with --json's object, e.g. --post --json '{"value": "x"}'.
    A JSON response is pretty-printed. Exits 1 if the status isn't 2xx.
    """
    if json_text is not None and not post:
        raise click.UsageError("--json needs --post")
    json_body = None if json_text is None else _parse_json_object(json_text)
    try:
        path = fly_api.expand_path(path)
    except ValueError as e:
        raise click.UsageError(str(e)) from None
    try:
        if post:
            response = fly_api.post(path, json_body, socket_path=socket_path)
        else:
            response = fly_api.get(path, socket_path=socket_path)
    except (OSError, http.client.HTTPException) as e:
        raise click.ClickException(f"{socket_path}: {e!r}") from None
    body = response.body
    try:
        pretty = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
        body = (pretty + "\n").encode()
    except ValueError:
        pass
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()
    if not 200 <= response.status < 300:
        click.echo(f"HTTP {response.status} {response.reason}", err=True)
        sys.exit(1)
