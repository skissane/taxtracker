"""Call the Fly Machines API through the Machine's own ``/.fly/api`` socket.

On a Fly Machine, ``/.fly/api`` is a Unix socket that forwards HTTP requests
to the Machines API, authenticated as the Machine itself, so no API token is
needed (see Fly's suspend/resume docs). It's root-only by default, and
``docker-entrypoint.sh`` gives the ``lifetracker`` group access (issue #211).
``lifetracker-cli fly-api`` (``lifetracker.cli.fly_api``) wraps ``get`` and
``post``, for use over ``./flyssh.sh``. It doesn't import Django.
"""

import http.client
import json
import os
import socket
from dataclasses import dataclass

SOCKET_PATH = "/.fly/api"

# The Host header Fly's docs use for Machines API requests over the socket.
HOST = "flaps"

TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class Response:
    status: int
    reason: str
    content_type: str
    body: bytes


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__(HOST, timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def expand_path(path: str, environ=os.environ) -> str:
    """Replace ``{app}`` and ``{machine}`` in ``path`` with ``FLY_APP_NAME`` and
    ``FLY_MACHINE_ID``, which Fly sets on the Machine; ``./flyssh.sh`` runs no
    shell there, so ``$FLY_APP_NAME`` wouldn't expand. Raises ``ValueError`` if
    a placeholder's variable is unset or blank, or ``path`` isn't absolute."""
    if not path.startswith("/"):
        raise ValueError(f"path must start with '/': {path!r}")
    placeholders = (("{app}", "FLY_APP_NAME"), ("{machine}", "FLY_MACHINE_ID"))
    for placeholder, name in placeholders:
        if placeholder in path:
            value = environ.get(name, "").strip()
            if not value:
                raise ValueError(f"{placeholder} needs {name}, which isn't set")
            path = path.replace(placeholder, value)
    return path


def get(path: str, socket_path: str = SOCKET_PATH, timeout: float = TIMEOUT_SECONDS) -> Response:
    """GET ``path`` (e.g. ``/v1/apps/<app>/machines``) through the socket.

    Raises ``OSError`` if the socket can't be reached (``FileNotFoundError``
    off a Fly Machine, ``PermissionError`` without access to it) and
    ``http.client.HTTPException`` on a malformed response."""
    return _request("GET", path, None, socket_path, timeout)


def post(
    path: str,
    json_body: dict | None = None,
    socket_path: str = SOCKET_PATH,
    timeout: float = TIMEOUT_SECONDS,
) -> Response:
    """POST ``path`` (e.g. ``/v1/apps/<app>/machines/<id>/suspend``) through the
    socket, with ``json_body`` as an ``application/json`` body, or with an empty
    body (``Content-Length: 0``) if it's ``None``. Raises as ``get`` does."""
    body = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode()
    return _request("POST", path, body, socket_path, timeout)


def _request(method: str, path: str, body: bytes | None, socket_path: str, timeout: float) -> Response:
    headers = {} if body is None else {"Content-Type": "application/json"}
    conn = _UnixHTTPConnection(socket_path, timeout)
    try:
        # With no body, http.client still sends Content-Length: 0 for a POST.
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return Response(
            status=resp.status,
            reason=resp.reason,
            content_type=resp.getheader("Content-Type", ""),
            body=resp.read(),
        )
    finally:
        conn.close()
