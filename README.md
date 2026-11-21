# Bitta Craic

A self-hosted wedding photo challenge with guest uploads, a timed gallery launch, and password-protected photo moderation.

## Run locally

Python 3.11+ is recommended.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
ADMIN_PASSWORD='choose-a-long-password' ./start.sh
```

Open `http://localhost:3000`. The default port is `3000`; set `PORT` to use another port. The admin login is at `/admin/login`. Set `ADMIN_PASSWORD` before starting the app. `ADMIN_SESSION_SECRET` can optionally provide a separate signing secret for admin sessions.

## Docker

```sh
docker build -t bitta-craic .
docker run -d --name bitta-craic \
  -p 3000:3000 \
  -e ADMIN_PASSWORD='choose-a-long-password' \
  -e PUBLIC_URL='https://photos.example.com' \
  -v bitta-data:/app/data \
  -v bitta-uploads:/app/uploads \
  bitta-craic
```

Put the service behind Nginx or another reverse proxy and forward the original `Host` and `X-Forwarded-Proto` headers. Set `PUBLIC_URL` to the externally reachable guest URL so the admin QR code is correct behind a proxy. The app listens on `0.0.0.0` and honors the `PORT` environment variable when using `start.sh`.

## Configuration

Edit `settings.json` to change the challenge `prompts`, guest-facing `text`, and default `countdown_hours`. Keep prompts as a JSON array of strings. Environment variables override deployment-specific values:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `3000` | HTTP listen port when using `start.sh` |
| `ADMIN_PASSWORD` | unset | Required password for admin access |
| `ADMIN_SESSION_SECRET` | `ADMIN_PASSWORD` | Secret used to sign admin session cookies |
| `PUBLIC_URL` | inferred from request | Canonical guest URL used by the QR code |
| `DATA_DIR` | `./data` | Directory containing the SQLite database |
| `DB_PATH` | `$DATA_DIR/bittacraic.sqlite3` | SQLite database path |
| `UPLOAD_DIR` | `./uploads` | Local photo storage directory |
| `SETTINGS_FILE` | `./settings.json` | JSON application settings file |
| `GALLERY_COUNTDOWN_HOURS` | settings value, `72` | Initial countdown duration in hours |
| `MAX_UPLOAD_MB` | `15` | Maximum size per uploaded photo |
| `MAX_PHOTOS_PER_SUBMISSION` | `10` | Maximum photos per guest submission |

Back up both the database and uploads directory. JPG, PNG, WEBP, GIF, and HEIC uploads are accepted; HEIC images are converted to JPEG for browser compatibility. Photo files are stored with random names; the original filename is retained only as download metadata.

## Admin workflow

The app starts with submissions open. From `/admin`, close submissions and choose the gallery countdown, manually open the gallery early, or reopen submissions. Approve and reject individual photos or all pending photos. Only approved photos are visible in the live guest gallery. The QR code points to the configured public guest URL.
