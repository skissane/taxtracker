# lifetracker

Personal life admin tracker app. Tax return tracking (`taxtracker`) is currently its
only module.

## Docker

Build the image with:

```bash
just docker-build
```

The container startup path runs `upgrade_legacy_db`, then `migrate`, then starts the
Django development server on `0.0.0.0:${PORT:-8000}`.

Superuser creation is intentionally left out of automatic container startup. If you
need one, create it manually after the container is running, for example:

```bash
docker exec <container> python manage.py ensure_superuser
```
