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

`just docker-run` publishes the port on `127.0.0.1` only. The app runs with `DEBUG`
on and isn't hardened for network exposure, so don't publish it on other interfaces.

The container startup path runs `upgrade_legacy_db`, then `migrate`, then
`ensure_superuser`, then starts the Gunicorn WSGI server on `0.0.0.0:${PORT:-8000}`.
On first start, `ensure_superuser` creates an `admin` user with a random password and
prints it to the container log (`docker logs lifetracker`); on later starts it leaves
the existing user's password alone. If you miss it (the log is gone once the
container exits), reset it with `docker exec -it lifetracker python manage.py
changepassword admin`.

The SQLite database and secret key live in `/data` inside the container
(`LIFETRACKER_DB_PATH` and `LIFETRACKER_SECRET_KEY_FILE`), which `just docker-run`
mounts as the `lifetracker-data` named volume, so they survive container restarts and
image rebuilds. Remove the volume (`docker volume rm lifetracker-data`) to start fresh.

Static files (the admin's CSS/JS) are collected into the image at build time and
served by WhiteNoise.
