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

## Google sign-in

As well as the usual username and password, the admin login page can offer **Sign
in with Google**. It's off until it's configured, and even then it's locked down:
a Google account can sign in only if all of these hold:

- Google reports its email address as verified;
- the email is in `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS`;
- an existing, active Django user has that email address (compared
  case-insensitively).

Google sign-in never creates users, and never changes an existing user's
permissions. The user also needs **Staff status** to get into the admin, as with
password login. Password login keeps working alongside it.

It's configured with these environment variables (lists are comma-separated):

| Variable | Purpose |
|---|---|
| `LIFETRACKER_GOOGLE_SSO_CLIENT_ID` | Required. The OAuth client ID from Google Cloud, ending in `.apps.googleusercontent.com`. |
| `LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET` | Required. The OAuth client's secret. Google sign-in is enabled only when both this and the client ID are set; otherwise the login page shows no Google button and `/google_sso/` returns 404. |
| `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS` | Google email addresses allowed to sign in, e.g. `me@gmail.com`. Empty (the default) means no one can, even with the client configured. |
| `LIFETRACKER_GOOGLE_SSO_PROJECT_ID` | Optional. The Google Cloud project ID; not needed to sign in. |
| `LIFETRACKER_GOOGLE_SSO_CALLBACK_DOMAIN` | Optional. Host (and port, if any) used to build the callback URL that Google redirects back to, e.g. `tax.example.com`. Defaults to the host of the request, which is normally right; set it if the app is reached through a proxy that rewrites the `Host` header. |

The callback URL is `<scheme>://<host>/google_sso/callback/`, and it must exactly
match one of the client's authorized redirect URIs in Google Cloud, or Google
rejects the sign-in with `redirect_uri_mismatch`. The scheme is the request's, or
the `X-Forwarded-Proto` header's when a proxy sets one.

### Creating the Google OAuth client

You need a Google Cloud project with an OAuth client. Do this once for local
development and once for production (a separate client for each keeps the
production secret off your laptop); the steps are the same except for the redirect
URI.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project (or pick an existing one).
2. Open **Google Auth Platform** (**APIs & Services → OAuth consent screen**) and
   click **Get started** if the project doesn't have one yet. Give it an app name
   and your email as the support and developer contact, and choose **External** as
   the audience (the only option for a personal Google account).
3. Under **Audience**, leave the publishing status as **Testing** and add each
   Google account that will sign in under **Test users**. In testing mode, Google
   itself refuses sign-in by anyone else, which is an extra lock on top of
   `LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS`. The app only asks for the basic
   `openid`, `email` and `profile` scopes, so it doesn't need Google's verification.
4. Under **Clients**, click **Create client**, choose **Web application**, and add
   the callback URL under **Authorized redirect URIs** (see below for the value).
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
   [Cloud backup](#cloud-backup) for how `.env` is loaded; leave values unquoted):

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

1. Create a separate OAuth client as above, with the redirect URI
   `https://<app>.fly.dev/google_sso/callback/` (or
   `https://<your domain>/google_sso/callback/` if you've attached a custom
   domain; register each hostname you use). The `https` comes from Fly's proxy,
   which sets `X-Forwarded-Proto`; `LIFETRACKER_GOOGLE_SSO_CALLBACK_DOMAIN` isn't
   needed.
2. Set the variables as Fly secrets, not in `fly.toml`, which is in this public
   repo. Setting secrets restarts the Machine with them:

   ```bash
   fly secrets set \
     LIFETRACKER_GOOGLE_SSO_CLIENT_ID=1234567890-abc.apps.googleusercontent.com \
     LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET=... \
     LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=me@gmail.com
   ```

   To keep the secret out of your shell history, put the `NAME=value` lines in a
   file and run `fly secrets import < that-file` instead, then delete the file.
3. Log in to production with your password and set your user's **Email address**
   to your Google email, as in local development. The database is separate, so this
   has to be done on each instance.
4. Log out and test **Sign in with Google** at
   `https://<app>.fly.dev/admin/`.

To add or remove an allowed account later, run `fly secrets set
LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=...` with the full new list (and update the
test users in Google Cloud). To turn Google sign-in off, run `fly secrets unset
LIFETRACKER_GOOGLE_SSO_CLIENT_ID LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET`.

For another host, pass the same variables to the container with `-e` or
`--env-file`, as in [Production deployment](#production-deployment). Serve it over
HTTPS through a proxy that sets `X-Forwarded-Proto: https`, so the callback URL is
`https`; the callback URL takes the scheme from that header regardless of
`LIFETRACKER_TRUST_X_FORWARDED_PROTO`.

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
| `LIFETRACKER_ALLOWED_HOSTS` | Required, except on Fly.io, where it defaults to `<app>.fly.dev`. Hostnames the app answers to, e.g. `tax.example.com`. |
| `LIFETRACKER_CSRF_TRUSTED_ORIGINS` | Public origins, e.g. `https://tax.example.com`. Needed behind a TLS-terminating reverse proxy, or the admin login fails its CSRF check. |
| `LIFETRACKER_TRUST_X_FORWARDED_PROTO` | Set to `1` instead of listing origins, if the proxy always sets `X-Forwarded-Proto` and the container is reachable only through it. |
| `LIFETRACKER_BACKUP_S3_*` | Optional. Cloud backup bucket and credentials; see [Cloud backup](#cloud-backup). |
| `LIFETRACKER_GOOGLE_SSO_*` | Optional. Google sign-in client and allowed emails; see [Google sign-in](#google-sign-in). |
| `LIFETRACKER_BACKUP_HOSTNAME` | Recommended with cloud backup. Prefix for backup object keys, also shown as the instance name in the admin header; without it the container's hostname is used, a random ID by default. On Fly.io it defaults to `<app>.fly.dev`. |
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
   `.env` as `FLY_APP`, along with `LIFETRACKER_INITIAL_ADMIN_PASSWORD` (the
   password `admin` gets on first boot) and the `LIFETRACKER_BACKUP_S3_*` settings
   (see [Cloud backup](#cloud-backup)). `ENDPOINT_URL` and `REGION_NAME` are
   optional; `just fly-setup` leaves them unset on Fly if they're blank or
   absent.
2. Run `just fly-setup` (you need `fly`, `gh` and `jq` installed, and `fly` and
   `gh` logged in). It creates whatever's missing of: the Fly app, its volume (name and region from
   `fly.toml`), the admin password and S3 settings as Fly secrets, and the
   `FLY_APP` and `FLY_API_TOKEN` GitHub Actions secrets, the latter a new deploy
   token scoped to the app. It's safe to re-run, and never changes anything that already exists; to
   change a value, use `fly secrets set` or `gh secret set` directly.
3. If `FLY_API_TOKEN` already existed (e.g. from an earlier app, which its token
   is scoped to), run `just fly-setup --replace-token`. That always creates a new
   deploy token and overwrites the secret with it, so also use it to rotate the
   token. It doesn't revoke the old token: export `FLY_APP` (`fly` doesn't read
   `.env`), then use `fly tokens list` and `fly tokens revoke`.
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
