import pytest


@pytest.fixture(scope="session")
def _jwt_key_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("jwt")


@pytest.fixture(autouse=True)
def _jwt_key_files(settings, _jwt_key_dir):
    """Keep the JWT key pair the middleware creates out of the config directory.

    One pair per test process, so it's generated only once.
    """
    settings.LIFETRACKER_JWT_PRIVATE_KEY_FILE = _jwt_key_dir / "jwt_private_key.pem"
    settings.LIFETRACKER_JWT_JWKS_FILE = _jwt_key_dir / "jwt_jwks.json"
