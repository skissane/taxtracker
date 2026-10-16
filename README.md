# lifetracker

Personal life admin tracker app. Tax return tracking (`taxtracker`) is currently its
only module.

## Cloud backup

As well as downloading a backup of the database (**💾 Download DB Backup** on the
Financial Years list), you can upload one to an S3-compatible bucket: AWS S3,
Cloudflare R2, Backblaze B2 and others. Open **☁️ Cloud Backup Status** on the
Financial Years list (superusers only) to see when the last backup ran and to click
**Run Backup Now**. Each backup is uploaded as a new object named
`<hostname>/db-backup.<YYYY-MM-DD.HH.MM.SS>.sqlite3` (the timestamp is in UTC),
e.g. `my-laptop.local/db-backup.2026-10-11.01.13.53.sqlite3`, so backups from each
machine or environment sit in their own folder. Backups only run when you click the
button; nothing schedules them.

The bucket and credentials come from these environment variables:

| Variable | Purpose |
|---|---|
| `LIFETRACKER_BACKUP_S3_BUCKET_NAME` | Required. Bucket to upload to. |
| `LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID` | Required. Access key ID. |
| `LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY` | Required. Secret access key. |
| `LIFETRACKER_BACKUP_S3_ENDPOINT_URL` | The provider's S3 endpoint, e.g. `https://<account-id>.r2.cloudflarestorage.com`. Leave unset for AWS S3. |
| `LIFETRACKER_BACKUP_S3_REGION_NAME` | Region, e.g. `ap-southeast-2` (use `auto` for R2). |
| `LIFETRACKER_BACKUP_HOSTNAME` | Overrides the `<hostname>` object key prefix (default: the machine's hostname, as `hostname` prints it). `just docker-run` sets it to `docker.<host's hostname>`, since the Docker container has its own separate database and a meaningless hostname. |

Until the required ones are set, the button is disabled and the page lists the
missing variables.

When running with `./run.sh`, the easiest place for them is a `.env` file in the
repo root, which `run.sh` loads automatically:

```dotenv
LIFETRACKER_BACKUP_S3_BUCKET_NAME=my-lifetracker-backups
LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID=...
LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY=...
```

It's in dotenv format (`NAME=value` lines, `#` comments), read by uv rather than
run as a shell script. Variables already set in your shell take precedence over the
file. `.env` is gitignored, but since it holds credentials, also make it readable
only by you (`chmod 600 .env`). To use the same file for a one-off command, run
`UV_ENV_FILE=.env uv run python manage.py ...`.

`just docker-run` passes the same `.env` to the container with `--env-file`. With
plain `docker run`, pass the variables with `-e`, or the file with `--env-file .env`.
Docker reads the file more literally than uv does: it doesn't strip quotes, so leave
values unquoted.

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
port on `127.0.0.1` only. Don't publish a `DEBUG` container on other interfaces. To
use a host port other than 8000, set `LIFETRACKER_DOCKER_PORT` (e.g.
`LIFETRACKER_DOCKER_PORT=8080 just docker-run`, then open
<http://localhost:8080/admin/>).

It also mounts your host config directory (`~/.config/lifetracker`, or
`$LIFETRACKER_CONFIG_DIR` if set) read-only at `/config`, so the container uses the
same secret key and `initial_admin_password` as `./run.sh`. Before starting the
container it runs `manage.py check` on the host, which creates the directory and
secret key as you if they don't exist yet; that way Docker never creates them as
root. The database is still separate: the container's lives in the
`lifetracker-data` volume, not in the repo.

The container startup path runs `upgrade_legacy_db`, then `migrate`, then
`ensure_superuser`, then starts the Gunicorn WSGI server on `0.0.0.0:${PORT:-8000}`.
Gunicorn's worker timeout is 120 seconds rather than its default 30, since ZIP
exports, DB backups and archive imports run in the request and can be slow for a
large database. Change it with `LIFETRACKER_GUNICORN_TIMEOUT` (in seconds).
On first start, `ensure_superuser` creates an `admin` user with a random password and
prints it to the container log (`docker logs lifetracker`); on later starts it leaves
the existing user's password alone. If you miss it (the log is gone once the
container exits), reset it with `just docker-shell python manage.py changepassword
admin` (or, without `just`, `docker exec -it -u lifetracker lifetracker python
manage.py changepassword admin`).

To choose the initial password yourself, and keep it out of the log (worth doing in
production, where logs are often shipped elsewhere), put it in
`/data/initial_admin_password` on the volume before the first start, or point
`LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE` at a file containing it. `ensure_superuser`
then uses it and doesn't print it.

The SQLite database and secret key live in `/data` inside the container
(`/data/db.sqlite3` and `/data/secret_key`, because the image sets
`LIFETRACKER_DATA_DIR=/data`), so mount a volume there to keep them across
container restarts and image rebuilds. `just docker-run` mounts the
`lifetracker-data` named volume there, and reads the secret key from `/config`
instead (see above); an older `lifetracker-data` volume's `/data/secret_key` is then
ignored, which logs out existing sessions once. `initial_admin_password` only takes
effect when `admin` is first created, so with an existing volume it won't change the
password. Remove the volume (`docker volume rm lifetracker-data`) to start fresh. To
put either file somewhere else, set `LIFETRACKER_DB_PATH` or
`LIFETRACKER_SECRET_KEY_FILE`, which take precedence.

The entrypoint starts as root only to set up the `lifetracker` user (UID and GID
10001 by default), then drops to that user to run migrations and Gunicorn. Set
`LIFETRACKER_UID` and `LIFETRACKER_GID` to run as different IDs, for example to match
the owner of a bind-mounted directory; `just docker-run` sets them to your own, so the
container can read your `secret_key` (mode 0600) from `/config`. On each start the
entrypoint re-owns anything in `/data` not already owned by that user, which also
upgrades volumes from older root-run images. Only `/data` is re-owned: if you've
pointed `LIFETRACKER_DB_PATH` or a writable `LIFETRACKER_CONFIG_DIR` somewhere else,
`chown` its files to that user yourself, or the app can't write to them. Running with
`docker run --user` skips all of this and runs as the given user, who then needs write
access to `/data`. Pass `-u lifetracker` to `docker exec` (as above, and as `just
docker-shell` does) so commands don't run as root.

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
| `LIFETRACKER_ALLOWED_HOSTS` | Required, except on Fly.io, where it defaults to `<app>.fly.dev`. Hostnames the app answers to, e.g. `tax.example.com`. |
| `LIFETRACKER_CSRF_TRUSTED_ORIGINS` | Public origins, e.g. `https://tax.example.com`. Needed behind a TLS-terminating reverse proxy, or the admin login fails its CSRF check. |
| `LIFETRACKER_TRUST_X_FORWARDED_PROTO` | Set to `1` instead of listing origins, if the proxy always sets `X-Forwarded-Proto` and the container is reachable only through it. |
| `LIFETRACKER_BACKUP_S3_*` | Optional. Cloud backup bucket and credentials; see [Cloud backup](#cloud-backup). |
| `LIFETRACKER_BACKUP_HOSTNAME` | Recommended with cloud backup. Prefix for backup object keys, also shown as the instance name in the admin header; without it the container's hostname is used, a random ID by default. On Fly.io it defaults to `fly.<app>`. |
| `LIFETRACKER_COMMIT_ID` | Optional. Commit shown in the admin header. `just docker-build` and `./run.sh` set it automatically from `git rev-parse HEAD`. |
| `LIFETRACKER_COMMIT_URL_BASE` | Optional. Base URL used for the commit link in the admin header. Defaults to `https://github.com/skissane/taxtracker/commit/`. |

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

### Fly.io

Production runs on [Fly.io](https://fly.io), configured by `fly.toml`: one Machine
in Sydney, a volume at `/data` for the database and secret key, and auto-stop when
idle. Every push to `master` deploys automatically once CI passes: CI pushes the
tested image to GHCR, then deploys that exact image to Fly.

The app's name is deliberately kept out of this public repo. `fly.toml` has no
`app` line; CI reads the name from the `FLY_APP` repository secret, and `flyctl`
reads it from the `FLY_APP` environment variable. When you run `fly` commands
yourself, export it first (or pass `-a <name>` to each command):

```bash
export FLY_APP=<app-name>
fly status
fly logs
```

Don't commit the name anywhere, including in issues, PRs or pasted command output.
On the Machine, `LIFETRACKER_ALLOWED_HOSTS` and `LIFETRACKER_BACKUP_HOSTNAME`
default from the name (via `FLY_APP_NAME`, which Fly sets), so they need no
configuration.

#### One-time setup

1. Pick a name that can't be guessed, e.g. `lifetracker-<random>`, and add it to
   `.env` as `FLY_APP`, along with all five `LIFETRACKER_BACKUP_S3_*` settings (see
   [Cloud backup](#cloud-backup)).
2. Run `just fly-setup` (you need `fly`, `gh` and `jq` installed and logged in). It
   creates whatever's missing of: the Fly app, its volume (name and region from
   `fly.toml`), the S3 settings as Fly secrets, and the `FLY_APP` and
   `FLY_API_TOKEN` GitHub Actions secrets, the latter a new deploy token scoped to
   the app. It's safe to re-run, and never changes anything that already exists; to
   change a value, use `fly secrets set` or `gh secret set` directly.
3. If `FLY_API_TOKEN` already existed (e.g. from an earlier app, which its token
   is scoped to), run `just fly-setup --replace-token`. That always creates a new
   deploy token and overwrites the secret with it, so also use it to rotate the
   token. It doesn't revoke the old token: export `FLY_APP` (`fly` doesn't read
   `.env`), then use `fly tokens list` and `fly tokens revoke`.
4. With `FLY_APP` exported, deploy once by hand with `fly deploy --ha=false`. A volume attaches to only one
   Machine, so the app must never run more than one; `--ha=false` stops the first
   deploy creating two. Later deploys, including CI's, keep the existing Machine.
5. On first boot, `ensure_superuser` creates `admin` with a random password and
   prints it to `fly logs`.

A manual `fly deploy` deploys `:latest`, i.e. master's most recently published
image. To deploy local changes, merge them to `master` rather than building on Fly.

Every boot runs migrations against the only copy of the data, so run a cloud backup
before merging a risky migration. Fly's daily volume snapshots are the only
automatic fallback.
