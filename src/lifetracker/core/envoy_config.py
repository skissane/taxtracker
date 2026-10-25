"""Envoy's config, filled in from Django's settings and URLconf.

Envoy, the authenticating front proxy (issue #176), can't read environment
variables, and what it checks has to agree with Django: the requests that need
no login (``auth_whitelist``), the JWT's issuer, audience and cookie name, and
where the public key is (``jwt_auth``). So its config, ``envoy/envoy.yaml``, is
a template, and ``render()`` fills it in. ``docker-entrypoint.sh`` does that at
startup via the ``envoy_config`` management command, then starts Envoy.

Each ``@NAME@`` placeholder is replaced with a JSON value, which is valid YAML,
so nothing needs escaping.
"""

import ipaddress
import json
import re

from django.conf import settings
from django.urls import reverse

from lifetracker.core import jwt_auth
from lifetracker.core.auth_whitelist import auth_whitelist

# The name of jwt_authn's provider in the template.
JWT_PROVIDER = "django"

_PLACEHOLDER = re.compile(r"@([A-Z_]+)@")


def _method_header(method):
    return {"name": ":method", "string_match": {"exact": method}}


def jwt_rules():
    """jwt_authn's rules: the whitelist, needing no JWT, then everything else.

    One rule per path and method, each matched exactly, so it's plain from the
    config what each lets through. The last rule's ``prefix`` is the same as
    the route's, so every request that reaches gunicorn matched some rule.
    """
    rules = []
    for entry in auth_whitelist():
        match = {"prefix" if entry.prefix else "path": entry.path}
        for method in entry.methods:
            rules.append({"match": {**match, "headers": [_method_header(method)]}})
    rules.append({"match": {"prefix": "/"}, "requires": {"provider_name": JWT_PROVIDER}})
    return rules


def login_redirect():
    """The Location for a request without a valid JWT.

    The Lua filter in the template puts the percent-encoded path and query
    string in the ``lifetracker`` dynamic metadata's ``next``.
    """
    login_path = reverse("admin:login")
    # A literal "%" would start a formatter command operator.
    if "%" in login_path:
        raise ValueError(f"The login path can't contain %: {login_path}")
    return f"{login_path}?next=%DYNAMIC_METADATA(lifetracker:next)%"


def _port(name, value):
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be from 1 to 65535: {value}")
    return value


def render(template, *, listen_host, listen_port, upstream_port, request_log):
    """``template`` with every placeholder filled in. ``request_log`` is the
    file Envoy logs every request to, for ``client_ips``.

    Raises ``ValueError`` if ``listen_host`` isn't an IP address, either port
    isn't from 1 to 65535, the two ports are the same, or the template has a
    placeholder this doesn't know.
    """
    try:
        ipaddress.ip_address(listen_host)
    except ValueError:
        raise ValueError(f"The listen host must be an IP address: {listen_host}")
    _port("The listen port", listen_port)
    _port("The upstream port", upstream_port)
    if listen_port == upstream_port:
        raise ValueError(f"The listen and upstream ports must differ: {listen_port}")
    values = {
        "LISTEN_HOST": listen_host,
        "LISTEN_PORT": listen_port,
        "UPSTREAM_PORT": upstream_port,
        "JWT_ISSUER": jwt_auth.JWT_ISSUER,
        "JWT_AUDIENCE": jwt_auth.JWT_AUDIENCE,
        "JWT_COOKIE": jwt_auth.JWT_COOKIE_NAME,
        "JWKS_FILE": str(settings.LIFETRACKER_JWT_JWKS_FILE),
        "JWT_RULES": jwt_rules(),
        "LOGIN_REDIRECT": login_redirect(),
        "REQUEST_LOG": str(request_log),
    }

    def replace(match):
        try:
            return json.dumps(values[match[1]])
        except KeyError:
            raise ValueError(f"Unknown placeholder in the template: {match[0]}")

    return _PLACEHOLDER.sub(replace, template)
