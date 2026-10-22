"""A JWT cookie for logged-in sessions, for the authenticating front proxy.

The proxy (issue #176) is to reject every request without a valid JWT, except
the few in ``auth_whitelist``, so nothing unauthenticated reaches Django even if
a view is ever exposed without login. ``JWTCookieMiddleware`` issues that JWT:
whenever there's a logged-in session (password or Google SSO login alike), it
sets the ``JWT_COOKIE_NAME`` cookie, and clears the cookie when there isn't one.

The token is signed with an Ed25519 key (``EdDSA``), generated on first use and
kept in ``LIFETRACKER_JWT_PRIVATE_KEY_FILE`` (by default in the config
directory, next to ``secret_key``). The public key is written as a JWKS to
``LIFETRACKER_JWT_JWKS_FILE``, for the proxy to read, so it never has to fetch
it from the app it guards.

Its ``exp`` is the session's expiry, and it's re-issued whenever that advances
(Django moves it forward each time it saves the session), so it never expires
before the session does; a shorter lifetime would get a POST from an admin page
left open blocked at the proxy. That's safe because Django's own session check
stays as the inner layer: after logout the session is gone, so a leftover JWT
gets no further than the proxy.

The session also stores a hash of the token, and on every request the
middleware checks the cookie against it, so the token is bound to the session
even if the proxy is misconfigured or bypassed. A session whose cookie doesn't
match is logged out. A session with no hash yet, e.g. one from before this
existed, isn't checked, and gets a token on that request.

That includes the whitelist's requests, although they need no token: the admin
sends a logged-in visitor from the login page to the index, so if a session
whose token the proxy rejects weren't logged out there, it would go round in
circles. For the same reason, a token is re-issued once the signing key changes,
as the proxy would reject one signed with the old key.
"""

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.contrib.auth import logout
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from jwt.algorithms import OKPAlgorithm

logger = logging.getLogger(__name__)

JWT_COOKIE_NAME = "lifetracker_jwt"
JWT_ALGORITHM = "EdDSA"
# For the proxy to check, so a token signed with this key for anything else
# wouldn't do.
JWT_ISSUER = "lifetracker"
JWT_AUDIENCE = "lifetracker"

# The session's record of the issued token: {"sub": <user pk as str>, "kid":
# <signing key's kid>, "hashes": [<current token's hash>, <previous token's
# hash, if any>]}. The
# previous one is accepted too, so a request the browser sent before it got a
# re-issued cookie isn't logged out.
SESSION_KEY = "_lifetracker_jwt"

_keys = {}
_keys_lock = threading.Lock()


def _jwk_thumbprint(jwk):
    """The RFC 7638 thumbprint of an OKP ``jwk``, used as its ``kid``."""
    required = {name: jwk[name] for name in ("crv", "kty", "x")}
    canonical = json.dumps(required, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _create_private_key_file(path):
    """Write a new key to ``path``, unless another process got there first.

    Returns the PEM that's at ``path`` afterwards, whichever process wrote it.
    """
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file with mode 0600. Linking it into place fails if
    # the path exists, so gunicorn's workers starting at once can't overwrite
    # each other's keys.
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        try:
            os.link(temp_name, path)
        except FileExistsError:
            return path.read_bytes()
    finally:
        os.unlink(temp_name)
    logger.info("Created JWT signing key %s", path)
    return pem


def _write_jwks(jwk, path):
    """Write a JWKS of ``jwk`` to ``path``, unless it's already there."""
    content = json.dumps({"keys": [jwk]}, indent=2) + "\n"
    with contextlib.suppress(OSError):
        if path.read_text(encoding="utf-8") == content:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # Public, unlike mkstemp's default.
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        os.unlink(temp_name)
        raise


def signing_key():
    """The private key and its ``kid``, creating the key (and JWKS) if need be.

    Loaded once per process, for each pair of file paths.
    """
    private_path = Path(settings.LIFETRACKER_JWT_PRIVATE_KEY_FILE)
    jwks_path = Path(settings.LIFETRACKER_JWT_JWKS_FILE)
    with _keys_lock:
        cached = _keys.get((private_path, jwks_path))
        if cached is not None:
            return cached
        try:
            pem = private_path.read_bytes()
        except FileNotFoundError:
            pem = _create_private_key_file(private_path)
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ImproperlyConfigured(f"{private_path} isn't an Ed25519 private key.")
        jwk = OKPAlgorithm.to_jwk(key.public_key(), as_dict=True)
        kid = _jwk_thumbprint(jwk)
        _write_jwks({**jwk, "kid": kid, "use": "sig", "alg": JWT_ALGORITHM}, jwks_path)
        _keys[(private_path, jwks_path)] = key, kid
        return key, kid


def issue_token(user, expires_at):
    """A signed JWT for ``user``, expiring at ``expires_at`` (a datetime)."""
    key, kid = signing_key()
    claims = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": str(user.pk),
        "iat": timezone.now(),
        "exp": expires_at,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(claims, key, algorithm=JWT_ALGORITHM, headers={"kid": kid})


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class JWTCookieMiddleware:
    """Issue, re-issue and clear the JWT cookie, and check it against the session.

    List it after ``AuthenticationMiddleware`` and before ``MessageMiddleware``:
    it re-issues the token when the session will be saved, so its response
    handling has to come after anything else that might change the session.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self._check_cookie(request)
        response = self.get_response(request)
        self._update_cookie(request, response)
        return response

    def _check_cookie(self, request):
        stored = request.session.get(SESSION_KEY)
        if stored is None or not request.user.is_authenticated:
            return
        cookie = request.COOKIES.get(JWT_COOKIE_NAME)
        presented = token_hash(cookie) if cookie else ""
        # Each comparison is constant-time; which of the two matched needn't be.
        if any(hmac.compare_digest(presented, h) for h in stored["hashes"]):
            return
        logger.warning(
            "Logging out user %s: JWT cookie %s",
            request.user.pk,
            "doesn't match the session" if cookie else "missing",
        )
        logout(request)

    def _update_cookie(self, request, response):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            if JWT_COOKIE_NAME in request.COOKIES:
                response.delete_cookie(JWT_COOKIE_NAME, path="/", samesite="Lax")
            return
        # SessionMiddleware doesn't save the session after a 500, so a token
        # recorded in it would be lost while the browser kept the cookie.
        if response.status_code == 500:
            return
        session = request.session
        stored = session.get(SESSION_KEY)
        sub = str(user.pk)
        same_user = stored is not None and stored["sub"] == sub
        _, kid = signing_key()
        # Django moves the session's expiry forward only when it saves the
        # session, which it does when it's modified (or on every request, with
        # SESSION_SAVE_EVERY_REQUEST).
        if (
            same_user
            and stored.get("kid") == kid
            and not session.modified
            and not settings.SESSION_SAVE_EVERY_REQUEST
        ):
            return
        token = issue_token(user, session.get_expiry_date())
        previous = stored["hashes"][:1] if same_user else []
        session[SESSION_KEY] = {
            "sub": sub,
            "kid": kid,
            "hashes": [token_hash(token), *previous],
        }
        response.set_cookie(
            JWT_COOKIE_NAME,
            token,
            # Like Django's session cookie.
            max_age=(
                None
                if session.get_expire_at_browser_close()
                else session.get_expiry_age()
            ),
            path="/",
            # Off only with DEBUG on, so it works over plain HTTP locally.
            secure=settings.SESSION_COOKIE_SECURE,
            httponly=True,
            samesite="Lax",
        )
