# Life Tracker

Personal life admin tracker app.
It started out as Tax Tracker, which is why the repository (and so the published image) is still named `taxtracker`.

It runs on Linux (x86_64 or aarch64) and macOS.
Windows isn't supported.

## What it does

The whole UI is the Django admin, and `/` redirects to `/admin/`.
It has two modules:

- **Tax tracker:** a checklist of items for each Australian financial year (ending 30 June), arranged as a tree, with notes, a done/pending status and file attachments.
  Each year has a summary page and a ZIP download of its attachments with a Markdown index, and several years can be downloaded as one ZIP.
  **Copy to new year** starts next year's checklist from this year's.
  An item can import PDFs from a Fidelity `.har` file or a `.zip`, and move attachments dated outside its year to the matching item in the right year.
- **Task tracker:** tasks arranged as a tree, each pending or done, with notes and file attachments.
  A task's type can let it link to another record in the app.

Attachments are stored in the SQLite database rather than as separate files, so the database (and a backup of it) holds everything.

## Running locally

You need [uv](https://docs.astral.sh/uv/) 0.13.5 or later (`uv self update`), which installs the right Python version itself.
Start the app with:

```bash
./run.sh
```

and open <http://localhost:8000/admin/>.
`run.sh` installs dependencies, upgrades a database created before the app was split into `core` and `taxtracker` (a no-op otherwise), applies migrations, creates an `admin` superuser if there isn't one, and starts Django's development server with `DEBUG` on.
When it creates `admin`, it prints a random password, unless you've set one in the `LIFETRACKER_INITIAL_ADMIN_PASSWORD` environment variable or in `~/.config/lifetracker/initial_admin_password`.

The database is `db.sqlite3` in the repo root (gitignored), and the generated secret key is `~/.config/lifetracker/secret_key`.
Set `LIFETRACKER_DB_PATH` or `LIFETRACKER_CONFIG_DIR` to put them elsewhere.
The database runs in SQLite's WAL mode, so recent changes may still be in `db.sqlite3-wal` beside it: to copy the database by hand, stop the app first and copy both, or use the admin's DB backup.

### The `.env` file

Settings such as the [Cloud backup](#cloud-backup) and [Google sign-in](#google-sign-in) credentials are environment variables.
The easiest place for them is a `.env` file in the repo root, which `run.sh` loads automatically:

```dotenv
LIFETRACKER_BACKUP_S3_BUCKET_NAME=my-lifetracker-backups
LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID=...
LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY=...
```

It's in dotenv format (`NAME=value` lines, `#` comments), read by uv rather than run as a shell script.
Variables already set in your shell take precedence over the file.
`.env` is gitignored, but since it holds credentials, also make it readable only by you (`chmod 600 .env`).
To use the same file for a one-off command, run `UV_ENV_FILE=.env uv run python manage.py ...`.

`just docker-run` passes the same `.env` to the container with `--env-file`.
With plain `docker run`, pass the variables with `-e`, or the file with `--env-file .env`.
Docker reads the file more literally than uv does: it doesn't strip quotes, so leave values unquoted.

## Docker

CI publishes the image from every commit to `master` as `ghcr.io/skissane/taxtracker:latest` (and `:<commit SHA>`), which anyone can pull.
It's built for x86_64 only.
To build your own from your checkout instead, run:

```bash
just docker-build
```

Run it with:

```bash
just docker-run
```

and open <http://localhost:8000/admin/>.

`just docker-run` is for local use: it sets `LIFETRACKER_DEBUG=1` and publishes the port on `127.0.0.1` only.
Don't publish a `DEBUG` container on other interfaces.
To use a host port other than 8000, set `LIFETRACKER_DOCKER_PORT` (e.g. `LIFETRACKER_DOCKER_PORT=8080 just docker-run`, then open <http://localhost:8080/admin/>).

It also mounts your host config directory (`~/.config/lifetracker`, or `$LIFETRACKER_CONFIG_DIR` if set) read-only at `/config`, so the container uses the same secret key and `initial_admin_password` as `./run.sh`.
Before starting the container it runs `manage.py check` and `manage.py ensure_jwt_key` on the host, which create the directory, the secret key and the JWT key pair as you if they don't exist yet; that way Docker never creates them as root.
The database is still separate: the container's lives in the `lifetracker-data` volume, not in the repo.

The container startup path runs `upgrade_legacy_db`, then `migrate`, then `ensure_superuser`, then `envoy_config` (which writes Envoy's config and creates the JWT key pair if it's missing; see [Front proxy JWT](#front-proxy-jwt)), then starts the Gunicorn WSGI server on `127.0.0.1:8001` behind an [Envoy](https://www.envoyproxy.io/) reverse proxy on `0.0.0.0:${PORT:-8000}`.
Envoy turns away requests without a valid login token (see [Front proxy JWT](#front-proxy-jwt)), and as Gunicorn listens only on the container's loopback interface, nothing outside the container can get to it without going through Envoy.
If Envoy exits, it's restarted a second later; Gunicorn is the container's main process, so the container ends when Gunicorn does.
If Envoy keeps failing (5 exits in a row, each within a minute of starting), the container stops Gunicorn and exits with status 1, so Fly records a failure and restarts the Machine.
Requests that arrive while Gunicorn is still starting wait for it (for up to about 10 seconds).
Envoy puts no time limit on a request, so long downloads and uploads aren't cut off.
`LIFETRACKER_HOST` (an IP address) and `PORT` set where Envoy listens, and `LIFETRACKER_GUNICORN_PORT` Gunicorn's port on loopback.
Envoy's config is the template `envoy/envoy.yaml`, filled in at startup; Envoy logs only requests it answered itself, such as a 503 when Gunicorn couldn't be reached or a redirect to the login page for want of a valid token (with the reason), as Gunicorn logs the rest.
Gunicorn's worker timeout is 120 seconds rather than its default 30, since ZIP exports, DB backups and archive imports run in the request and can be slow for a large database.
Change it with `LIFETRACKER_GUNICORN_TIMEOUT` (in seconds).
It uses gthread workers with 8 threads (`LIFETRACKER_GUNICORN_THREADS`) rather than a single sync worker, which a browser's idle spare connection can tie up, leaving other requests waiting.
With gthread, the timeout only restarts a worker whose main loop stops responding; it doesn't cut off a slow request in one of its threads.
On first start, `ensure_superuser` creates an `admin` user with a random password and prints it to the container log (`docker logs lifetracker`); on later starts it leaves the existing user's password alone, but turns its active, staff and superuser flags back on if any have been turned off.
If you miss the password (the log is gone once the container exits), reset it with `just docker-shell python manage.py changepassword admin` (or, without `just`, `docker exec -it -u lifetracker lifetracker python manage.py changepassword admin`).

To choose the initial password yourself, and keep it out of the log (worth doing in production, where logs are often shipped elsewhere), set it in the `LIFETRACKER_INITIAL_ADMIN_PASSWORD` environment variable, put it in `/data/initial_admin_password` on the volume before the first start, or point `LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE` at a file containing it.
The environment variable wins over the file if both are set.
`ensure_superuser` then uses it and doesn't print it.
Since `./run.sh` and `just docker-run` both read `.env`, a `LIFETRACKER_INITIAL_ADMIN_PASSWORD` there also sets the password of a newly created local `admin`.

The SQLite database and secret key live in `/data` inside the container (`/data/db.sqlite3` and `/data/secret_key`, because the image sets `LIFETRACKER_DATA_DIR=/data`), so mount a volume there to keep them across container restarts and image rebuilds.
`just docker-run` mounts the `lifetracker-data` named volume there, and reads the secret key from `/config` instead (see above); an older `lifetracker-data` volume's `/data/secret_key` is then ignored, which logs out existing sessions once.
`initial_admin_password` only takes effect when `admin` is first created, so with an existing volume it won't change the password.
Remove the volume (`docker volume rm lifetracker-data`) to start fresh.
To put either file somewhere else, set `LIFETRACKER_DB_PATH` or `LIFETRACKER_SECRET_KEY_FILE`, which take precedence.

The entrypoint starts as root only to set up the `lifetracker` user (UID and GID 10001 by default), then drops to that user to run migrations and Gunicorn.
Set `LIFETRACKER_UID` and `LIFETRACKER_GID` to run as different IDs, for example to match the owner of a bind-mounted directory; `just docker-run` sets them to your own, so the container can read your `secret_key` (mode 0600) from `/config`.
On each start the entrypoint re-owns anything in `/data` not already owned by that user, which also upgrades volumes from older root-run images.
On a Fly Machine it also gives the `lifetracker` group read and write access to the Fly Machines API socket, `/.fly/api`, which otherwise only root can use, so the app and `./flyssh.sh` sessions can call it without root.
Only `/data` is re-owned: if you've pointed `LIFETRACKER_DB_PATH` or a writable `LIFETRACKER_CONFIG_DIR` somewhere else, `chown` its files to that user yourself, or the app can't write to them.
Running with `docker run --user` skips all of this and runs as the given user, who then needs write access to `/data`.
Pass `-u lifetracker` to `docker exec` (as above, and as `just docker-shell` does) so commands don't run as root.

To keep config separate from data, for example with the secret key and initial admin password coming from a read-only secrets mount, set `LIFETRACKER_CONFIG_DIR`.
The secret key and initial password file are then read from there, while the database stays in `/data`.
A read-only config directory must already contain `secret_key`, `jwt_private_key.pem` and `jwt_jwks.json`, since they can't be generated there (`manage.py ensure_jwt_key` creates the last two).

Static files (the admin's CSS/JS) are collected into the image at build time and served by WhiteNoise.

### Production deployment

The image defaults to `LIFETRACKER_DEBUG=0`, and then refuses to start unless `LIFETRACKER_ALLOWED_HOSTS` is set.
Configure it with these environment variables (lists are comma-separated):

| Variable | Purpose |
|---|---|
| `LIFETRACKER_ALLOWED_HOSTS` | Required, except on Fly.io, where it defaults to `<app>.fly.dev`. Hostnames the app answers to, e.g. `lifetracker.example.com`. |
| `LIFETRACKER_CSRF_TRUSTED_ORIGINS` | Public origins, e.g. `https://lifetracker.example.com`. Needed behind a TLS-terminating reverse proxy, or the admin login fails its CSRF check. |
| `LIFETRACKER_TRUST_X_FORWARDED_PROTO` | Set to `1` instead of listing origins, if the proxy always sets `X-Forwarded-Proto` and the container is reachable only through it. |
| `LIFETRACKER_BACKUP_S3_*` | Optional. Cloud backup bucket and credentials; see [Cloud backup](#cloud-backup). |
| `LIFETRACKER_GOOGLE_SSO_*` | Optional. Google sign-in client and allowed emails; see [Google sign-in](#google-sign-in). |
| `LIFETRACKER_BACKUP_HOSTNAME` | Recommended with cloud backup. Prefix for backup object keys, also shown as the instance name in the admin header; without it the container's hostname is used, a random ID by default. On Fly.io it defaults to `<app>.fly.dev`. |
| `LIFETRACKER_UNAUTHENTICATED_SITE_NAME` | Optional. Site name shown in the admin header and page titles to visitors who aren't logged in, e.g. on the login page, instead of `Life Tracker` (the default). `just fly-setup` copies it from `.env` to a Fly secret. |
| `LIFETRACKER_COMMIT_ID` | Optional. Commit shown in the admin header. `just docker-build` and `./run.sh` set it automatically from `git rev-parse HEAD`. |
| `LIFETRACKER_COMMIT_URL_BASE` | Optional. Base URL used for the commit link in the admin header, shown only to logged-in users. Defaults to `https://github.com/skissane/taxtracker/commit/`. |
| `LIFETRACKER_LOG_TIMESTAMPS` | Optional. Set to `0` or `false` to leave the timestamp off the start of each log line, if whatever collects the logs adds its own. `fly.toml` sets it, as Fly does. |

For example:

```bash
docker run -d --name=lifetracker -p 127.0.0.1:8000:8000 -v lifetracker-data:/data \
  -e LIFETRACKER_ALLOWED_HOSTS=lifetracker.example.com \
  -e LIFETRACKER_CSRF_TRUSTED_ORIGINS=https://lifetracker.example.com \
  ghcr.io/skissane/taxtracker:latest
```

For an image you built with `just docker-build`, use `lifetracker` as the image name instead.

With `DEBUG` off, session and CSRF cookies are marked `Secure`, so the app must be served over HTTPS (typically via the reverse proxy); over plain HTTP you can't log in.
The app doesn't redirect HTTP to HTTPS or send an HSTS header itself; configure both on the reverse proxy.

### Fly.io

Production runs on [Fly.io](https://fly.io), configured by `fly.toml`: one Machine in Sydney, a volume at `/data` for the database and secret key, and the idle watchdog (below) stopping it when idle.
Every push to `master` deploys automatically once CI passes: CI pushes the tested image to GHCR, then deploys that exact image to Fly.
A deploy leaves a stopped Machine stopped, so CI then requests `/healthz/`, which starts it, and fails unless the app answers within 5 minutes that it's healthy (the database answers and every migration is applied) and running the deployed commit; if it fails, check `fly logs`.
That request keeps the Machine up until the idle watchdog stops it again.

The app's name is deliberately kept out of this public repo.
`fly.toml` has no `app` line; CI reads the name from the `FLY_APP` repository secret, and `flyctl` reads it from the `FLY_APP` environment variable.
When you run `fly` commands yourself, use `./flyctl.sh`, which reads `FLY_APP` from `.env`, exports it and runs `flyctl` with its arguments (it fails if `.env` doesn't set `FLY_APP`):

```bash
./flyctl.sh status
./flyctl.sh logs
```

Or export it yourself first (or pass `-a <name>` to each command):

```bash
export FLY_APP=<app-name>
fly status
```

To run a command on the Machine as the `lifetracker` user, use `./flyssh.sh`.
With no arguments it opens an interactive `bash`; with arguments it runs them as a command, directly rather than through a shell (each argument quoted, so spaces and quotes survive; use `./flyssh.sh bash -c '...'` for shell syntax).
It starts in the app directory and keeps the Machine's environment (the database location, the venv on `PATH` and the Fly secrets), as it switches user with `setpriv` like the container's entrypoint does, not `su -`.
It goes through `./flyctl.sh`, and passes `--pty` only when run from a terminal:

```bash
./flyssh.sh
./flyssh.sh python manage.py showmigrations
```

To run as another user, put `--user=<name>` first, e.g. `./flyssh.sh --user=root` (`HOME` is then `/home/<name>`, or `/root` for root).
Use `--` before a command that itself starts with `--`.

To query the Fly Machines API from the Machine itself, without an API token, use `lifetracker-cli fly-api`, which sends a GET through the Machine's `/.fly/api` socket and pretty-prints the JSON.
`{app}` and `{machine}` in the path become the Machine's app name and ID, as no shell expands `$FLY_APP_NAME` there:

```bash
./flyssh.sh lifetracker-cli fly-api /v1/apps/{app}/machines
./flyssh.sh lifetracker-cli fly-api /v1/apps/{app}/machines/{machine}
```

`--post` sends a POST instead, with an empty body, or with `--json`'s JSON object as the body; `./flyssh.sh` passes the argument through intact, quotes and all:

```bash
./flyssh.sh lifetracker-cli fly-api --post /v1/apps/{app}/machines/{machine}/metadata/note --json '{"value": "hello"}'
./flyssh.sh lifetracker-cli fly-api --post /v1/apps/{app}/machines/{machine}/stop  # stops the Machine, ending the session
```

If the Machine isn't running, `./flyssh.sh` starts it first, waiting for it to finish stopping if need be (this needs `jq`).
Fly's own auto-stop judges idleness by HTTP traffic through its proxy, not SSH sessions, so it could cut off a long session or command; `fly.toml` turns it off.

Instead, the Machine is stopped by the idle watchdog (`src/lifetracker/core/idle_watchdog.py`), which gunicorn starts as a separate process.
It counts established connections to the app's port (Envoy's) and SSH's (port 22) in `/proc/net/tcp` and `/proc/net/tcp6`.
Envoy closes a connection after it's been idle for 5 seconds (then waits up to 5 more for it to close), so Fly Proxy holding one open doesn't keep the Machine running.
Most HTTP requests are over between two checks, so gunicorn's workers also touch a file after every request, which counts as activity too.
Requests Envoy turns away for want of a login token never reach gunicorn, so they don't touch it; a scanner probing the site doesn't keep the Machine up beyond the connections it holds open.
Once there's been no activity for a while, the watchdog stops gunicorn gracefully.
A clean exit leaves the Machine stopped until the next request starts it.
It's off unless `LIFETRACKER_IDLE_STOP` is set:

- `off` (the default): it doesn't run.
- `monitor`: it checks and reports, but never stops anything.
- `stop`: it stops gunicorn once idle.

`LIFETRACKER_IDLE_STOP_MINUTES` sets the idle timeout (default 10).
Other settings are `LIFETRACKER_IDLE_STOP_PORTS` (default `$PORT` or 8000, and 22), `LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS` (default 30), `LIFETRACKER_IDLE_STOP_REQUEST_FILE` (the file the workers touch, default `/tmp/lifetracker-last-request`) and `LIFETRACKER_IDLE_STOP_STATUS_FILE`.
Every check, it writes its status as JSON to that file (default `/tmp/lifetracker-idle-watchdog.json`).
It logs to stderr, so on Fly its messages are in `./flyctl.sh logs`.

Superusers can see the status in the admin, from the **Idle Watchdog** link in the top bar:
- the mode;
- when the watchdog last checked, with a warning if it's gone quiet for more than three check intervals, which probably means it has stopped running;
- the last request, and how long until the idle timeout runs out;
- the watchdog's and the Machine's uptimes;
- whether [automatic backups](#automatic-backups) are on, how the last one went and when the next is due;
- for each port, its open connections and when it last saw one.

Loading the page is itself a request, so it restarts the idle timer.

The watchdog needs Linux, as it reads `/proc/net`; the Docker image is Linux even on a macOS host, so it works with `just docker-run` too.
`./run.sh` uses Django's development server, not gunicorn, so it never starts it.

The same process also records which seconds the app was up, so you can see what share of each day the Machine ran without piecing it together from the logs.
Each UTC day gets a 10,800-byte file, `<YYYY-MM-DD>.bin`, with one bit per second, most significant bit first, set while the app was up.
A day the app never ran has no file.
The files go in `LIFETRACKER_UPTIME_DIR`, which defaults to `$LIFETRACKER_DATA_DIR/uptime` (`/data/uptime` in the Docker image, so on the Fly volume), and `off` turns it off.
It records even with `LIFETRACKER_IDLE_STOP` off.
To see the last week (`--days` for more), with each day's share up and how many times the app started (a stall of over 5 seconds, e.g. a throttled CPU, also counts as a stop and start):

```bash
./flyssh.sh python manage.py uptime_report
```

Superusers can also see it in the admin, from the **Uptime** link in the top bar, with a bar per day; its Days box does what `--days` does.
Click a day's starts to see each up period that day: when it started, its last second up and how long it lasted.
Times there are UTC, and also shown in your browser's time zone.

The files aren't in the cloud backup, which only has the database.

`fly.toml` runs it in `stop` mode, with Fly's own auto-stop (`auto_stop_machines`) off; Fly still starts the Machine on the next request.

`./fly-machine-id.sh` prints the Machine's ID (it needs `jq`), for `flyctl` commands that take one:

```bash
./flyctl.sh machine status $(./fly-machine-id.sh)
```

Don't commit the name anywhere, including in issues, PRs or pasted command output.
On the Machine, `LIFETRACKER_ALLOWED_HOSTS` and `LIFETRACKER_BACKUP_HOSTNAME` default from the name (via `FLY_APP_NAME`, which Fly sets), so they need no configuration.
Superusers also get a **Fly Machine** link in the admin's top bar, to the Machine's page in Fly's dashboard (`https://fly.io/apps/<app>/machines/<machine id>`), shown only when Fly's `FLY_APP_NAME` and `FLY_MACHINE_ID` are both set.

#### One-time setup

1. Pick a name that can't be guessed, e.g. `lifetracker-<random>`, and add it to `.env` as `FLY_APP`, along with `LIFETRACKER_INITIAL_ADMIN_PASSWORD` (the password `admin` gets on first boot) and the `LIFETRACKER_BACKUP_S3_*` settings (see [Cloud backup](#cloud-backup)).
   `ENDPOINT_URL` and `REGION_NAME` are optional, as are `LIFETRACKER_UNAUTHENTICATED_SITE_NAME` (see [Production deployment](#production-deployment)) and the `LIFETRACKER_GOOGLE_SSO_*` settings (see [Google sign-in](#google-sign-in)); `just fly-setup` leaves them unset on Fly if they're blank or absent.
2. Run `just fly-setup` (you need `fly`, `gh` and `jq` installed, and `fly` and `gh` logged in).
   It creates whatever's missing of: the Fly app, its volume (name and region from `fly.toml`), the admin password, S3 and Google sign-in settings as Fly secrets, and the `FLY_APP` and `FLY_API_TOKEN` GitHub Actions secrets, the latter a new deploy token scoped to the app.
   It's safe to re-run, and never changes anything that already exists; to change a value, use `fly secrets set` or `gh secret set` directly.
   It refuses a value in `.env` containing `#`, a quote, `\` or `$`, which Fly might store differently; set that one with `fly secrets set` instead.
3. If `FLY_API_TOKEN` already existed (e.g. from an earlier app, which its token is scoped to), run `just fly-setup --replace-token`.
   That always creates a new deploy token and overwrites the secret with it, so also use it to rotate the token.
   It doesn't revoke the old token; use `./flyctl.sh tokens list` and `./flyctl.sh tokens revoke`.
4. Push to `master` (e.g. merge a PR); CI's deploy creates the Machine.
   A volume attaches to only one Machine, so the app must never run more than one; CI passes `--ha=false`, which stops a deploy to an app with no Machine creating two.
   Pass it too on any manual `fly deploy`.
   Later deploys keep the existing Machine.
5. On first boot, `ensure_superuser` creates `admin` with the password from `LIFETRACKER_INITIAL_ADMIN_PASSWORD`.
   Changing that secret later has no effect; change the password in the admin instead.

A manual `fly deploy` deploys `:latest`, i.e. master's most recently published image.
To deploy local changes, merge them to `master` rather than building on Fly.

Every boot runs migrations against the only copy of the data, so run a cloud backup before merging a risky migration.
Fly's daily volume snapshots are the only automatic fallback.

## Cloud backup

Backups cover the whole database, so they live on their own page: **Backups** under **Core** on the admin home page (superusers only).
As well as downloading a backup of the database (**Download DB Backup**), you can upload one to an S3-compatible bucket: AWS S3, Cloudflare R2, Backblaze B2 and others.
The page shows when the last cloud backup ran and has a **Run Backup Now** button.
Each backup is uploaded as a new object named `<hostname>/db-backup.<YYYY-MM-DD.HH.MM.SS>.sqlite3` (the timestamp is in UTC), e.g. `my-laptop.local/db-backup.2026-10-11.01.13.53.sqlite3`, so backups from each machine or environment sit in their own folder.
**View Backups in Bucket** on the same page lists the backups actually in the bucket (key, size and last-modified time in UTC, newest first): this machine's folder by default, with links to each other folder and to everything in the bucket.

The `cloud_backup` management command does the same as **Run Backup Now** from the command line, printing the uploaded object's key, and exits non-zero if the backup fails:

```bash
UV_ENV_FILE=.env uv run python manage.py cloud_backup   # locally, with .env's settings
./flyssh.sh python manage.py cloud_backup               # on the Fly.io Machine
```

### Automatic backups

With `LIFETRACKER_AUTO_BACKUP=on` (as `fly.toml` sets), the idle watchdog's process (see [Fly.io](#flyio)) also backs up the database automatically:

- when the app starts;
- every 6 hours while it's up (`LIFETRACKER_AUTO_BACKUP_INTERVAL_HOURS` changes that);
- with `LIFETRACKER_IDLE_STOP=stop`, just before stopping the Machine, which waits for the backup to finish (or to fail; a backup that fails is logged and the next start tries again).

Each runs `manage.py cloud_backup --if-changed`, which uploads nothing if the database hasn't changed since the last cloud backup, so a Machine that's started and stopped again without anyone changing anything adds no backups.
Nothing can change the database while the Machine is stopped, so there's no need to wake it on a timetable.
The backup at startup catches changes from before a stop that skipped the one before stopping, such as a crash or a deploy restarting the Machine.
Logging in counts as a change, as sessions are stored in the database.
To tell, every cloud backup records the database file's size and modification time in `db.sqlite3-backup-state.json` beside it (`LIFETRACKER_BACKUP_STATE_FILE` moves it).
Nothing deletes old backups yet.

The bucket and credentials come from these environment variables:

| Variable | Purpose |
|---|---|
| `LIFETRACKER_BACKUP_S3_BUCKET_NAME` | Required. Bucket to upload to. |
| `LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID` | Required. Access key ID. |
| `LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY` | Required. Secret access key. |
| `LIFETRACKER_BACKUP_S3_ENDPOINT_URL` | The provider's S3 endpoint, e.g. `https://<account-id>.r2.cloudflarestorage.com`. Leave unset for AWS S3. |
| `LIFETRACKER_BACKUP_S3_REGION_NAME` | Region, e.g. `ap-southeast-2` (use `auto` for R2). |
| `LIFETRACKER_BACKUP_HOSTNAME` | Overrides the `<hostname>` object key prefix (default: the machine's hostname, as `hostname` prints it). `just docker-run` sets it to `docker.<host's hostname>`, since the Docker container has its own separate database and a meaningless hostname. |

Until the required ones are set, the button is disabled and the page lists the missing variables.

Locally, put them in [`.env`](#the-env-file).

### Restoring a cloud backup

The `restore_cloud_backup` management command replaces the whole database with a cloud backup, given its key as **View Backups in Bucket** lists it:

```bash
./flyssh.sh python manage.py restore_cloud_backup my-app.fly.dev/db-backup.2026-10-11.01.13.53.sqlite3
```

It downloads the backup and checks it's an intact SQLite database from this app, asks you to type `yes` (`--no-input` skips that), uploads a cloud backup of the current database, then copies the downloaded one over it and runs migrations.
It stops before changing anything if the download, the check or that backup fails, and once the restore is done prints the key of the backup it made, which you can restore to undo it.
The **Backups** page's last backup is that one too, not whatever the restored database had recorded.

You don't need to stop the app first.
The copy goes through SQLite (its online backup API), as a single write transaction, rather than replacing the file: while it runs, the app can still read the old data but its writes wait (for up to 30 seconds, after which they fail), and once it finishes every request sees the restored data, never a mix of the two; SQLite looks after `db.sqlite3-wal` and `db.sqlite3-shm` itself, so there's nothing to delete.
Still, run it when nobody's using the app: anything changed after the backup of the current database is taken (while it uploads) is in neither copy, a form opened before the restore and saved after it saves into the restored data, and as sessions are in the database, whoever is logged in is likely to be logged out.
It needs temporary space (`/tmp`) for two copies of the database, the download and the backup of the current one, and, while copying, room beside the database for its write-ahead log to grow to the restored database's size.
The [client IP record](#client-ips) is a separate file, so a restore leaves it alone.

## Purging orphaned files

Deleting an attachment deletes its file from the database too.
Before that also covered attachments deleted along with their item, task, received document or financial year, or by **Delete selected**, those deletes left the files behind.
The `purge_orphaned_files` management command finds stored files no attachment refers to and prints how many there are and their total size; with `--delete` it deletes them, which only restoring a backup can undo.
Run a [cloud backup](#cloud-backup) first, and run it while nobody is uploading attachments, as a file uploaded while it runs could be deleted too.

```bash
uv run python manage.py purge_orphaned_files            # locally: report only
./flyssh.sh python manage.py purge_orphaned_files       # on the Fly.io Machine
./flyssh.sh python manage.py purge_orphaned_files --delete
```

## Google sign-in

As well as the usual username and password, the admin login page can offer **Sign in with Google**.
It's off until it's configured, and even then it's locked down: a Google account can sign in only if all of these hold:

- Google reports its email address as verified;
- the email is in `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS`;
- exactly one Django user has that email address (compared case-insensitively), and that user is active.

Google sign-in never creates users, and never changes an existing user's permissions.
The user also needs **Staff status** to get into the admin, as with password login.
Password login keeps working alongside it, and a Google sign-in session lasts as long as a password one (Django's default, two weeks).

It's configured with these environment variables (lists are comma-separated):

| Variable | Purpose |
|---|---|
| `LIFETRACKER_GOOGLE_SSO_CLIENT_ID` | Required. The OAuth client ID from Google Cloud, ending in `.apps.googleusercontent.com`. |
| `LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET` | Required. The OAuth client's secret. Google sign-in is enabled only when both this and the client ID are set; otherwise the login page shows no Google button and `/google_sso/` returns 404. |
| `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS` | Google email addresses allowed to sign in, e.g. `me@gmail.com`. Empty (the default) means no one can, even with the client configured. |
| `LIFETRACKER_GOOGLE_SSO_PROJECT_ID` | Optional. The Google Cloud project ID; not needed to sign in. |
| `LIFETRACKER_GOOGLE_SSO_CALLBACK_DOMAIN` | Optional. Host (and port, if any) used to build the callback URL that Google redirects back to, e.g. `lifetracker.example.com`. Defaults to the host of the request, which is normally right; set it if the app is reached through a proxy that rewrites the `Host` header. |

The callback URL is `<scheme>://<host>/google_sso/callback/`, and it must exactly match one of the client's authorized redirect URIs in Google Cloud, or Google rejects the sign-in with `redirect_uri_mismatch`.
The scheme is the request's, or the `X-Forwarded-Proto` header's when a proxy sets one.

### Creating the Google OAuth client

You need a Google Cloud project with an OAuth client.
One client serves both local development and production: register each one's callback URL as a redirect URI (see below for the values), and `just fly-setup` copies the same settings from `.env` to Fly.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project (or pick an existing one).
2. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview), check the project picker at the top shows the project you want to use, then click **Get started** and fill in the form: give it an app name and your email as the support and developer contact, and choose **External** as the audience (the only option for a personal Google account).
3. Under **Audience**, leave the publishing status as **Testing** and add each Google account that will sign in under **Test users**.
   In testing mode, Google itself refuses sign-in by anyone else, which is an extra lock on top of `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS`.
   The app only asks for the basic `openid`, `email` and `profile` scopes, so it doesn't need Google's verification.
4. Under **Clients**, click **Create client**, choose **Web application**, and add the callback URLs under **Authorized redirect URIs** (see below for the values).
   Leave **Authorized JavaScript origins** empty.
5. Click **Create**, then copy the client ID and client secret.
   Google may only show the secret once, so store it safely (e.g. in a password manager).

### Local development

1. Create an OAuth client as above, with the redirect URI `http://localhost:8000/google_sso/callback/`.
   Google allows plain `http` only for `localhost` and loopback addresses.
   The host must match how you open the app: if you browse to `http://127.0.0.1:8000/` instead, register `http://127.0.0.1:8000/google_sso/callback/` (or both).
   With `LIFETRACKER_DOCKER_PORT` or a different `runserver` port, use that port.
2. Add the client and your email to `.env` in the repo root (see [The `.env` file](#the-env-file); leave values unquoted):

   ```dotenv
   LIFETRACKER_GOOGLE_SSO_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
   LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET=...
   LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=me@gmail.com
   ```

3. Start the app with `./run.sh` (or `just docker-run`; `.env` reaches the container too).
4. Give your Django user the same email: log in with your password, open **Users**, edit your user, set **Email address** to your Google email, and save.
5. Log out, open <http://localhost:8000/admin/>, and click **Sign in with Google**.
   If sign-in fails, the login page shows why (e.g. "Email address not allowed" or "User not found").

### Production (Fly.io)

1. In Google Cloud, add a second redirect URI to the OAuth client, `https://<app>.fly.dev/google_sso/callback/` (plus `https://<your domain>/google_sso/callback/` for each custom domain).
   Fly's proxy sets `X-Forwarded-Proto`, so the scheme is `https` without `LIFETRACKER_GOOGLE_SSO_CALLBACK_DOMAIN`.
2. Run `just fly-setup` (see [One-time setup](#one-time-setup)), which copies the other `LIFETRACKER_GOOGLE_SSO_*` settings from `.env` to Fly secrets.
   It refuses to set only one of the client ID and secret, and warns if `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS` is empty.
3. Set your user's **Email address** and test sign-in at `https://<app>.fly.dev/admin/`, as in steps 4 and 5 of local development.
   Production's database is separate, so the email has to be set there too.

To change the allowed accounts later, run `fly secrets set LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=...` with the full new list (`just fly-setup` won't overwrite it), and update `.env` and the test users in Google Cloud to match.
To turn Google sign-in off, run `fly secrets unset LIFETRACKER_GOOGLE_SSO_CLIENT_ID LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET`.

For another host, pass the same variables to the container with `-e` or `--env-file`, as in [Production deployment](#production-deployment).
Serve it over HTTPS through a proxy that sets `X-Forwarded-Proto: https`, so the callback URL is `https`; the callback URL takes the scheme from that header regardless of `LIFETRACKER_TRUST_X_FORWARDED_PROTO`.

## Front proxy JWT

In the Docker image (so on Fly.io too), an authenticating proxy, Envoy, sits in front of the app ([#176](https://github.com/skissane/taxtracker/issues/176)): it turns away any request that doesn't carry a valid token from the app, apart from the few that logging in needs and the health check, so even a page accidentally left open without login couldn't be reached.
A browser without a valid token is redirected to the login page, which then sends it back to the page it asked for, as the app itself would; a request with an `Authorization` header gets a 401 instead.
Envoy checks the token's signature, expiry, issuer and audience; it takes it from the cookie below or from an `Authorization: Bearer` header.
Paths are normalised first (`..`, `.` and repeated slashes resolved, and requests with an encoded `/` or `\` rejected), so no spelling of a path gets past the check that its plain form wouldn't.
`./run.sh` runs the app without Envoy.

Whenever you're logged in (with a password or with Google), it sets a `lifetracker_jwt` cookie holding a signed token that expires when your session does, and renews it whenever the session's expiry moves forward; logging out deletes it.
The app also checks the cookie itself: a logged-in session that comes back without its own token is logged out, so a stolen session cookie alone isn't enough.
Sessions from before this, which have no token yet, just get one.

The token is signed with a key the app creates on first start, `jwt_private_key.pem` in the config directory (`/data` in Docker and on Fly.io, `~/.config/lifetracker` locally), and the public key is written beside it as `jwt_jwks.json`, for the proxy.
Set `LIFETRACKER_JWT_PRIVATE_KEY_FILE` or `LIFETRACKER_JWT_JWKS_FILE` to put either somewhere else.
Nothing replaces the key automatically; deleting it makes the app create a new one on its next start, which logs everyone out once, so it's the way to revoke every token.
The requests that need no login are listed by:

```bash
uv run python manage.py auth_whitelist
```

Envoy's config is generated from the same list at startup, by `manage.py envoy_config`, so the two can't drift apart.
Envoy reads the public key when it starts, so a new key only takes effect when the container restarts (which a new key needs anyway, as it's only created at startup).

## Client IPs

In the Docker image, the app keeps a lasting record of where requests come from ([#201](https://github.com/skissane/taxtracker/issues/201)), so unexpected access stands out; `fly logs` keeps only a short window.
Envoy writes a line for every request, including the ones it turns away, which never reach the app.
A small process started with gunicorn reads those every few seconds and adds them up per client IP: when it was first and last seen, how many requests it made, how many Envoy turned away for want of a login token, how many had a valid token (so came from a logged-in browser), its last request, and its country and network (ASN).
The IP is the one Fly Proxy reports in its `Fly-Client-IP` header (Envoy's peer without it, e.g. under `just docker-run`).
`./run.sh` runs no Envoy, so records nothing.

The record is a SQLite file of its own, `client_ips.sqlite3` in `LIFETRACKER_DATA_DIR` (so `/data` on the Fly volume), not part of the main database or its backups.
Set `LIFETRACKER_CLIENT_IP_DB` to put it somewhere else, or to `off` to stop recording.

Countries and networks come from [DB-IP](https://db-ip.com)'s free Lite databases (CC BY 4.0), which need no account.
At startup, the process downloads this month's into `geoip/` beside the record (`LIFETRACKER_GEOIP_DIR` to change that) unless it already has them, falling back to last month's early in a month before the new ones are out, and deletes older ones.
Each IP is looked up when first seen, and keeps that location; IPs seen while the databases were missing are looked up once they're there.
At every startup, it also checks Apple's [list of iCloud Private Relay egress ranges](https://mask-api.icloud.com/egress-ip-ranges.csv), downloading it (about 12 MB) only if it's changed, and marks each IP that's in it when seen, with the rough location of the relay's users the list gives.
IPs recorded before there was a list (including before this feature) are checked once there is one.
`LIFETRACKER_GEOIP_DOWNLOAD=0` turns the download off; to download them by hand:

```bash
./flyssh.sh python manage.py update_geoip
```

Superusers can see the record in the admin, from the **Client IPs** link in the top bar: how the last DB-IP and Private Relay downloads went (with the error and traceback if it failed, as `fly logs` keeps only a short window), the networks that logged-in requests came from (with how many of their IPs were Private Relay ones), then the IPs seen most recently (marking Private Relay ones), optionally only those with logged-in requests.

To get the whole record as data, `client_ips_export` writes it as JSON Lines, one object per IP with the same fields as the record (times in ISO 8601 UTC, missing values `null`), first seen first; `--logged-in` keeps only IPs with logged-in requests:

```bash
./flyssh.sh python manage.py client_ips_export > client-ips.jsonl
```

## Home screen app

Every admin page links a web app manifest, `/manifest.webmanifest`, and an app icon, so "Add to Home Screen" on iOS (or "Install app" in Chrome) opens Life Tracker as a standalone app with its own icon, starting at `/admin/`.
Browsers fetch the manifest without cookies, so it needs no login, and the name it gives is `LIFETRACKER_UNAUTHENTICATED_SITE_NAME` (default "Life Tracker"), the same as the login page shows.
On iOS the label under the icon is the page's own site name when you add it, which you can edit then.
A home screen app on iOS has its own cookies, separate from Safari's, so you log in once inside it.

The default icon is a gold check in a box on the admin's blue.
To use your own, open **App Icon** in the admin's top bar (superusers only) and upload a square PNG of at least 512x512 pixels; the same page resets it to the default.
The icon is stored in the database, so backups include it, and like the manifest it's public, so pick one that doesn't give away what the site holds.
iOS copies the icon when you add the app, so to see a new one there, remove the app and add it again.



The `lifetracker-cli` command has a few standalone helpers for preparing files to attach, separate from the web app: extracting attachments from `.eml` files and PDFs from Fidelity `.har` files, filtering ZIPs, converting ZIPs of `.eml` files to PDF or text, and grouping lists of dates by financial year.
It also has `fly-api`, for querying the Fly Machines API on a Fly Machine (see Fly.io above).
List them with:

```bash
uv run lifetracker-cli --help
```

and run `uv run lifetracker-cli <command> --help` for each one's options.
`zip-eml-to-pdf` also needs [wkhtmltopdf](https://wkhtmltopdf.org/) installed.

## Development

The project's tasks are [just](https://just.systems/) recipes (the `Makefile` only forwards to `just`).
Run `just --list` to see them all.
The main ones:

| Command | What it does |
|---|---|
| `just test` | Runs the test suite (pytest, in parallel across CPU cores). |
| `just check` | Runs the whitespace, `screenshots/`, ruff and djlint checks, without fixing anything. |
| `just fix` | Fixes lint issues and reformats code and templates. |
| `just coverage` | Runs the tests and prints a coverage report. |
| `just screenshots` | Saves light and dark mode screenshots of admin pages to `screenshots/`, using a headless Chromium it installs with Playwright. |
| `just ci` | Runs everything CI runs, including building the Docker image. |

The tests that send requests through Envoy run it with Docker, and are skipped without it; on macOS they need Docker Desktop's host networking turned on (Settings → Resources → Network).
On a pull request, CI also takes the screenshots and posts them as a comment on it.

[`AGENTS.md`](AGENTS.md) has more detail on the code's layout, CI and conventions.
