import pytest

from lifetracker.core import client_ips


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


@pytest.fixture(autouse=True)
def _backup_state_file(settings, tmp_path):
    """Keep the state cloud backups record out of the source tree."""
    settings.LIFETRACKER_BACKUP_STATE_FILE = tmp_path / "backup-state.json"


@pytest.fixture(autouse=True)
def _no_relay_ranges_download(monkeypatch):
    """Never download Apple's iCloud Private Relay ranges: ``update_geoip``
    does unless given ``download_relay_ranges``, and so the collector and the
    ``update_geoip`` command. Instead the list is always unchanged, so there
    are none unless a test writes them."""
    monkeypatch.setattr(
        client_ips, "_download_relay_ranges", lambda url, destination, etag: None
    )
