# Life Tracker

Personal life admin tracker app. It started out as Tax Tracker, which is why the
repository (and so the published image) is still named `taxtracker`.

It runs on Linux (x86_64 or aarch64) and macOS. Windows isn't supported.

## What it does

The whole UI is the Django admin, and `/` redirects to `/admin/`. It has two
modules:

- **Tax tracker:** a checklist of items for each Australian financial year (ending
  30 June), arranged as a tree, with notes, a done/pending status and file
  attachments. Each year has a summary page and a ZIP download of its attachments
  with a Markdown index, and several years can be downloaded as one ZIP. **Copy to
  new year** starts next year's checklist from this year's. An item can import PDFs
  from a Fidelity `.har` file or a `.zip`, and move attachments dated outside its
  year to the matching item in the right year.
- **Task tracker:** tasks arranged as a tree, each pending or done, with notes and
  file attachments. A task's type can let it link to another record in the app.

Attachments are stored in the SQLite database rather than as separate files, so the
database (and a backup of it) holds everything.

## Running locally

You need [uv](https://docs.astral.sh/uv/), which installs the right Python version
itself. Start the app with:

```bash
./run.sh
```

and open <http://localhost:8000/admin/>. `run.sh` installs dependencies, upgrades a
database created before the app was split into `core` and `taxtracker` (a no-op
otherwise), applies migrations, creates an `admin` superuser if there isn't one,
and starts Django's development server with `DEBUG` on. When it creates `admin`, it
prints a random password, unless you've set one in the
`LIFETRACKER_INITIAL_ADMIN_PASSWORD` environment variable or in
`~/.config/lifetracker/initial_admin_password`.

The database is `db.sqlite3` in the repo root (gitignored), and the generated secret
key is `~/.config/lifetracker/secret_key`. Set `LIFETRACKER_DB_PATH` or
`LIFETRACKER_CONFIG_DIR` to put them elsewhere.

### The `.env` file

Settings such as the [Cloud backup](#cloud-backup) and
[Google sign-in](#google-sign-in) credentials are environment variables. The
easiest place for them is a `.env` file in the repo root, which `run.sh` loads
automatically:

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

CI publishes the image from every commit to `master` as
`ghcr.io/skissane/taxtracker:latest` (and `:<commit SHA>`), which anyone can pull.
It's built for x86_64 only. To build your own from your checkout instead, run:

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
the existing user's password alone, but turns its active, staff and superuser flags
back on if any have been turned off. If you miss the password (the log is gone once the
container exits), reset it with `just docker-shell python manage.py changepassword
admin` (or, without `just`, `docker exec -it -u lifetracker lifetracker python
manage.py changepassword admin`).

To choose the initial password yourself, and keep it out of the log (worth doing in
production, where logs are often shipped elsewhere), set it in the
`LIFETRACKER_INITIAL_ADMIN_PASSWORD` environment variable, put it in
`/data/initial_admin_password` on the volume before the first start, or point
`LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE` at a file containing it. The environment
variable wins over the file if both are set. `ensure_superuser` then uses it and
doesn't print it. Since `./run.sh` and `just docker-run` both read `.env`, a
`LIFETRACKER_INITIAL_ADMIN_PASSWORD` there also sets the password of a newly created
local `admin`.

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
| `LIFETRACKER_ALLOWED_HOSTS` | Required, except on Fly.io, where it defaults to `<app>.fly.dev`. Hostnames the app answers to, e.g. `lifetracker.example.com`. |
| `LIFETRACKER_CSRF_TRUSTED_ORIGINS` | Public origins, e.g. `https://lifetracker.example.com`. Needed behind a TLS-terminating reverse proxy, or the admin login fails its CSRF check. |
| `LIFETRACKER_TRUST_X_FORWARDED_PROTO` | Set to `1` instead of listing origins, if the proxy always sets `X-Forwarded-Proto` and the container is reachable only through it. |
| `LIFETRACKER_BACKUP_S3_*` | Optional. Cloud backup bucket and credentials; see [Cloud backup](#cloud-backup). |
| `LIFETRACKER_GOOGLE_SSO_*` | Optional. Google sign-in client and allowed emails; see [Google sign-in](#google-sign-in). |
| `LIFETRACKER_BACKUP_HOSTNAME` | Recommended with cloud backup. Prefix for backup object keys, also shown as the instance name in the admin header; without it the container's hostname is used, a random ID by default. On Fly.io it defaults to `<app>.fly.dev`. |
| `LIFETRACKER_UNAUTHENTICATED_SITE_NAME` | Optional. Site name shown in the admin header and page titles to visitors who aren't logged in, e.g. on the login page, instead of `Life Tracker` (the default). `just fly-setup` copies it from `.env` to a Fly secret. |
| `LIFETRACKER_COMMIT_ID` | Optional. Commit shown in the admin header. `just docker-build` and `./run.sh` set it automatically from `git rev-parse HEAD`. |
| `LIFETRACKER_COMMIT_URL_BASE` | Optional. Base URL used for the commit link in the admin header, shown only to logged-in users. Defaults to `https://github.com/skissane/taxtracker/commit/`. |

For example:

```bash
docker run -d --name=lifetracker -p 127.0.0.1:8000:8000 -v lifetracker-data:/data \
  -e LIFETRACKER_ALLOWED_HOSTS=lifetracker.example.com \
  -e LIFETRACKER_CSRF_TRUSTED_ORIGINS=https://lifetracker.example.com \
  ghcr.io/skissane/taxtracker:latest
```

For an image you built with `just docker-build`, use `lifetracker` as the image
name instead.

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
yourself, use `./flyctl.sh`, which reads `FLY_APP` from `.env`, exports it and
runs `flyctl` with its arguments (it fails if `.env` doesn't set `FLY_APP`):

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
With no arguments it opens an interactive `bash`; with arguments it runs them as
a command, directly rather than through a shell (each argument quoted, so spaces
and quotes survive; use `./flyssh.sh bash -c '...'` for shell syntax). It starts
in the app directory and keeps the Machine's environment (the database location,
the venv on `PATH` and the Fly secrets), as it switches user with `setpriv` like
the container's entrypoint does, not `su -`. It goes through `./flyctl.sh`, and
passes `--pty` only when run from a terminal:

```bash
./flyssh.sh
./flyssh.sh python manage.py showmigrations
```

The Machine auto-stops when idle, and Fly judges that by HTTP traffic through
its proxy, not SSH sessions, so a long session or command may be cut off, and SSH
fails while the Machine is stopped. Load the site first, or run
`./flyctl.sh machine start`.

`./fly-machine-id.sh` prints the Machine's ID (it needs `jq`), for `flyctl`
commands that take one:

```bash
./flyctl.sh machine status $(./fly-machine-id.sh)
```

Don't commit the name anywhere, including in issues, PRs or pasted command output.
On the Machine, `LIFETRACKER_ALLOWED_HOSTS` and `LIFETRACKER_BACKUP_HOSTNAME`
default from the name (via `FLY_APP_NAME`, which Fly sets), so they need no
configuration.

#### One-time setup

1. Pick a name that can't be guessed, e.g. `lifetracker-<random>`, and add it to
   `.env` as `FLY_APP`, along with `LIFETRACKER_INITIAL_ADMIN_PASSWORD` (the
   password `admin` gets on first boot) and the `LIFETRACKER_BACKUP_S3_*` settings
   (see [Cloud backup](#cloud-backup)). `ENDPOINT_URL` and `REGION_NAME` are
   optional, as are `LIFETRACKER_UNAUTHENTICATED_SITE_NAME` (see
   [Production deployment](#production-deployment)) and the
   `LIFETRACKER_GOOGLE_SSO_*` settings (see [Google sign-in](#google-sign-in)); `just fly-setup` leaves them unset on Fly
   if they're blank or absent.
2. Run `just fly-setup` (you need `fly`, `gh` and `jq` installed, and `fly` and
   `gh` logged in). It creates whatever's missing of: the Fly app, its volume (name and region from
   `fly.toml`), the admin password, S3 and Google sign-in settings as Fly secrets, and the
   `FLY_APP` and `FLY_API_TOKEN` GitHub Actions secrets, the latter a new deploy
   token scoped to the app. It's safe to re-run, and never changes anything that already exists; to
   change a value, use `fly secrets set` or `gh secret set` directly. It refuses
   a value in `.env` containing `#`, a quote, `\` or `$`, which Fly might store
   differently; set that one with `fly secrets set` instead.
3. If `FLY_API_TOKEN` already existed (e.g. from an earlier app, which its token
   is scoped to), run `just fly-setup --replace-token`. That always creates a new
   deploy token and overwrites the secret with it, so also use it to rotate the
   token. It doesn't revoke the old token; use `./flyctl.sh tokens list` and
   `./flyctl.sh tokens revoke`.
4. Push to `master` (e.g. merge a PR); CI's deploy creates the Machine. A volume
   attaches to only one Machine, so the app must never run more than one; CI
   passes `--ha=false`, which stops a deploy to an app with no Machine creating
   two. Pass it too on any manual `fly deploy`. Later deploys keep the existing
   Machine.
5. On first boot, `ensure_superuser` creates `admin` with the password from
   `LIFETRACKER_INITIAL_ADMIN_PASSWORD`. Changing that secret later has no effect;
   change the password in the admin instead.

A manual `fly deploy` deploys `:latest`, i.e. master's most recently published
image. To deploy local changes, merge them to `master` rather than building on Fly.

Every boot runs migrations against the only copy of the data, so run a cloud backup
before merging a risky migration. Fly's daily volume snapshots are the only
automatic fallback.

## Cloud backup

As well as downloading a backup of the database (**💾 Download DB Backup** on the
Financial Years list), you can upload one to an S3-compatible bucket: AWS S3,
Cloudflare R2, Backblaze B2 and others. Open **☁️ Cloud Backup Status** on the
Financial Years list (superusers only) to see when the last backup ran and to click
**Run Backup Now**. Each backup is uploaded as a new object named
`<hostname>/db-backup.<YYYY-MM-DD.HH.MM.SS>.sqlite3` (the timestamp is in UTC),
e.g. `my-laptop.local/db-backup.2026-10-11.01.13.53.sqlite3`, so backups from each
machine or environment sit in their own folder. Backups only run when you click the
button; nothing schedules them. **View Backups in Bucket** on the same page lists
the backups actually in the bucket (key, size and last-modified time in UTC,
newest first): this machine's folder by default, with links to each other folder
and to everything in the bucket.

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

Locally, put them in [`.env`](#the-env-file).

## Google sign-in

As well as the usual username and password, the admin login page can offer **Sign
in with Google**. It's off until it's configured, and even then it's locked down:
a Google account can sign in only if all of these hold:

- Google reports its email address as verified;
- the email is in `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS`;
- exactly one Django user has that email address (compared case-insensitively),
  and that user is active.

Google sign-in never creates users, and never changes an existing user's
permissions. The user also needs **Staff status** to get into the admin, as with
password login. Password login keeps working alongside it, and a Google sign-in
session lasts as long as a password one (Django's default, two weeks).

It's configured with these environment variables (lists are comma-separated):

| Variable | Purpose |
|---|---|
| `LIFETRACKER_GOOGLE_SSO_CLIENT_ID` | Required. The OAuth client ID from Google Cloud, ending in `.apps.googleusercontent.com`. |
| `LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET` | Required. The OAuth client's secret. Google sign-in is enabled only when both this and the client ID are set; otherwise the login page shows no Google button and `/google_sso/` returns 404. |
| `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS` | Google email addresses allowed to sign in, e.g. `me@gmail.com`. Empty (the default) means no one can, even with the client configured. |
| `LIFETRACKER_GOOGLE_SSO_PROJECT_ID` | Optional. The Google Cloud project ID; not needed to sign in. |
| `LIFETRACKER_GOOGLE_SSO_CALLBACK_DOMAIN` | Optional. Host (and port, if any) used to build the callback URL that Google redirects back to, e.g. `lifetracker.example.com`. Defaults to the host of the request, which is normally right; set it if the app is reached through a proxy that rewrites the `Host` header. |

The callback URL is `<scheme>://<host>/google_sso/callback/`, and it must exactly
match one of the client's authorized redirect URIs in Google Cloud, or Google
rejects the sign-in with `redirect_uri_mismatch`. The scheme is the request's, or
the `X-Forwarded-Proto` header's when a proxy sets one.

### Creating the Google OAuth client

You need a Google Cloud project with an OAuth client. One client serves both local
development and production: register each one's callback URL as a redirect URI
(see below for the values), and `just fly-setup` copies the same settings from
`.env` to Fly.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project (or pick an existing one).
2. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview),
   check the project picker at the top shows the project you want to use, then
   click **Get started** and fill in the form: give it an app name and your email
   as the support and developer contact, and choose **External** as the audience
   (the only option for a personal Google account).
3. Under **Audience**, leave the publishing status as **Testing** and add each
   Google account that will sign in under **Test users**. In testing mode, Google
   itself refuses sign-in by anyone else, which is an extra lock on top of
   `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS`. The app only asks for the basic
   `openid`, `email` and `profile` scopes, so it doesn't need Google's verification.
4. Under **Clients**, click **Create client**, choose **Web application**, and add
   the callback URLs under **Authorized redirect URIs** (see below for the values).
   Leave **Authorized JavaScript origins** empty.
5. Click **Create**, then copy the client ID and client secret. Google may only
   show the secret once, so store it safely (e.g. in a password manager).

### Local development

1. Create an OAuth client as above, with the redirect URI
   `http://localhost:8000/google_sso/callback/`. Google allows plain `http` only for
   `localhost` and loopback addresses. The host must match how you open the app:
   if you browse to `http://127.0.0.1:8000/` instead, register
   `http://127.0.0.1:8000/google_sso/callback/` (or both). With
   `LIFETRACKER_DOCKER_PORT` or a different `runserver` port, use that port.
2. Add the client and your email to `.env` in the repo root (see
   [The `.env` file](#the-env-file); leave values unquoted):

   ```dotenv
   LIFETRACKER_GOOGLE_SSO_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
   LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET=...
   LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=me@gmail.com
   ```

3. Start the app with `./run.sh` (or `just docker-run`; `.env` reaches the
   container too).
4. Give your Django user the same email: log in with your password, open
   **Users**, edit your user, set **Email address** to your Google email, and save.
5. Log out, open <http://localhost:8000/admin/>, and click **Sign in with Google**.
   If sign-in fails, the login page shows why (e.g. "Email address not allowed" or
   "User not found").

### Production (Fly.io)

1. In Google Cloud, add a second redirect URI to the OAuth client,
   `https://<app>.fly.dev/google_sso/callback/` (plus
   `https://<your domain>/google_sso/callback/` for each custom domain). Fly's
   proxy sets `X-Forwarded-Proto`, so the scheme is `https` without
   `LIFETRACKER_GOOGLE_SSO_CALLBACK_DOMAIN`.
2. Run `just fly-setup` (see [One-time setup](#one-time-setup)), which copies the
   other `LIFETRACKER_GOOGLE_SSO_*` settings from `.env` to Fly secrets. It refuses
   to set only one of the client ID and secret, and warns if
   `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS` is empty.
3. Set your user's **Email address** and test sign-in at
   `https://<app>.fly.dev/admin/`, as in steps 4 and 5 of local development.
   Production's database is separate, so the email has to be set there too.

To change the allowed accounts later, run `fly secrets set
LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=...` with the full new list (`just fly-setup`
won't overwrite it), and update `.env` and the test users in Google Cloud to match.
To turn Google sign-in off, run `fly secrets unset LIFETRACKER_GOOGLE_SSO_CLIENT_ID
LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET`.

For another host, pass the same variables to the container with `-e` or
`--env-file`, as in [Production deployment](#production-deployment). Serve it over
HTTPS through a proxy that sets `X-Forwarded-Proto: https`, so the callback URL is
`https`; the callback URL takes the scheme from that header regardless of
`LIFETRACKER_TRUST_X_FORWARDED_PROTO`.

## Command-line utilities

The `lifetracker-cli` command has a few standalone helpers for preparing files to
attach, separate from the web app: extracting attachments from `.eml` files and PDFs
from Fidelity `.har` files, filtering ZIPs, converting ZIPs of `.eml` files to PDF or
text, and grouping lists of dates by financial year. List them with:

```bash
uv run lifetracker-cli --help
```

and run `uv run lifetracker-cli <command> --help` for each one's options.
`zip-eml-to-pdf` also needs [wkhtmltopdf](https://wkhtmltopdf.org/) installed.

## Development

The project's tasks are [just](https://just.systems/) recipes (the `Makefile` only
forwards to `just`). Run `just --list` to see them all. The main ones:

| Command | What it does |
|---|---|
| `just test` | Runs the test suite (pytest, in parallel across CPU cores). |
| `just check` | Runs the whitespace, ruff and djlint checks, without fixing anything. |
| `just fix` | Fixes lint issues and reformats code and templates. |
| `just coverage` | Runs the tests and prints a coverage report. |
| `just screenshots` | Saves light and dark mode screenshots of admin pages to `screenshots/`, using a headless Chromium it installs with Playwright. |
| `just ci` | Runs everything CI runs, including building the Docker image. |

On a pull request, CI also takes the screenshots and posts them as a comment on it.

[`AGENTS.md`](AGENTS.md) has more detail on the code's layout, CI and conventions.
