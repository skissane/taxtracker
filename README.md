# lifetracker

Personal life admin tracker app. Tax return tracking (`taxtracker`) is currently its
only module.

## Docker

Build the image with:

```bash
just docker-build
```

Run it with:

```bash
just docker-run
```

and open <http://localhost:8000/admin/>.

`just docker-run` is for local use: it sets `LIFETRACKER_DEBUG=1` and publishes the
port on `127.0.0.1` only. Don't publish a `DEBUG` container on other interfaces.

The container startup path runs `upgrade_legacy_db`, then `migrate`, then
`ensure_superuser`, then starts the Gunicorn WSGI server on `0.0.0.0:${PORT:-8000}`.
Gunicorn's worker timeout is 120 seconds rather than its default 30, since ZIP
exports, DB backups and archive imports run in the request and can be slow for a
large database. Change it with `LIFETRACKER_GUNICORN_TIMEOUT` (in seconds).
On first start, `ensure_superuser` creates an `admin` user with a random password and
prints it to the container log (`docker logs lifetracker`); on later starts it leaves
the existing user's password alone. If you miss it (the log is gone once the
container exits), reset it with `docker exec -it lifetracker python manage.py
changepassword admin`.

To choose the initial password yourself, and keep it out of the log (worth doing in
production, where logs are often shipped elsewhere), put it in
`/data/initial_admin_password` on the volume before the first start, or point
`LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE` at a file containing it. `ensure_superuser`
then uses it and doesn't print it.

The SQLite database and secret key live in `/data` inside the container
(`/data/db.sqlite3` and `/data/secret_key`, because the image sets
`LIFETRACKER_DATA_DIR=/data`), which `just docker-run` mounts as the
`lifetracker-data` named volume, so they survive container restarts and image
rebuilds. Remove the volume (`docker volume rm lifetracker-data`) to start fresh. To
put either file somewhere else, set `LIFETRACKER_DB_PATH` or
`LIFETRACKER_SECRET_KEY_FILE`, which take precedence.

To keep config separate from data, for example with the secret key and initial
admin password coming from a read-only secrets mount, set `LIFETRACKER_CONFIG_DIR`.
The secret key and initial password file are then read from there, while the
database stays in `/data`. A read-only config directory must already contain
`secret_key`, since it can't be generated there.

Static files (the admin's CSS/JS) are collected into the image at build time and
served by WhiteNoise.

### Production deployment

The image defaults to `LIFETRACKER_DEBUG=0`, and then refuses to start unless
`LIFETRACKER_ALLOWED_HOSTS` is set. Configure it with these environment variables
(lists are comma-separated):

| Variable | Purpose |
|---|---|
| `LIFETRACKER_ALLOWED_HOSTS` | Required. Hostnames the app answers to, e.g. `tax.example.com`. |
| `LIFETRACKER_CSRF_TRUSTED_ORIGINS` | Public origins, e.g. `https://tax.example.com`. Needed behind a TLS-terminating reverse proxy, or the admin login fails its CSRF check. |
| `LIFETRACKER_TRUST_X_FORWARDED_PROTO` | Set to `1` instead of listing origins, if the proxy always sets `X-Forwarded-Proto` and the container is reachable only through it. |

For example:

```bash
docker run -d --name=lifetracker -p 127.0.0.1:8000:8000 -v lifetracker-data:/data \
  -e LIFETRACKER_ALLOWED_HOSTS=tax.example.com \
  -e LIFETRACKER_CSRF_TRUSTED_ORIGINS=https://tax.example.com \
  lifetracker
```

With `DEBUG` off, session and CSRF cookies are marked `Secure`, so the app must be
served over HTTPS (typically via the reverse proxy); over plain HTTP you can't log in.
The app doesn't redirect HTTP to HTTPS or send an HSTS header itself; configure
both on the reverse proxy.
