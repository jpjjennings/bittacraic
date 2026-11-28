from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import qrcode
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError
from PIL import ImageOps
from pillow_heif import register_heif_opener


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", ROOT / "uploads"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "bittacraic.sqlite3"))
SETTINGS_PATH = Path(os.getenv("SETTINGS_FILE", ROOT / "settings.json"))
MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "15")))
MAX_PHOTOS_PER_SUBMISSION = max(1, int(os.getenv("MAX_PHOTOS_PER_SUBMISSION", "10")))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_KEY = os.getenv("ADMIN_SESSION_SECRET", ADMIN_PASSWORD).encode("utf-8")
PASSWORD_HASH_ITERATIONS = 600_000
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

with SETTINGS_PATH.open(encoding="utf-8") as settings_file:
    SETTINGS = json.load(settings_file)
register_heif_opener()

DEFAULT_PROMPTS = SETTINGS["prompts"]
TEXT = SETTINGS["text"]
DEFAULT_COUNTDOWN_HOURS = int(os.getenv("GALLERY_COUNTDOWN_HOURS", SETTINGS.get("countdown_hours", 72)))

app = FastAPI(title="Bitta Craic", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def hash_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password_hash(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_hex, digest_hex = encoded_hash.split("$", 3)
        iterations = int(iterations_text)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        salt = bytes.fromhex(salt_hex)
        expected_digest = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(digest, expected_digest)


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed', 'live')),
                go_live_at TEXT,
                countdown_hours INTEGER NOT NULL DEFAULT 72,
                wheel_enabled INTEGER NOT NULL DEFAULT 1,
                prompts_json TEXT
            );
            CREATE TABLE IF NOT EXISTS tables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                token TEXT NOT NULL UNIQUE,
                photo_limit INTEGER NOT NULL CHECK (photo_limit >= 1)
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_name TEXT,
                table_number TEXT,
                challenge TEXT NOT NULL,
                created_at TEXT NOT NULL,
                table_id INTEGER REFERENCES tables(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                approval TEXT NOT NULL DEFAULT 'pending' CHECK (approval IN ('pending', 'approved', 'rejected'))
            );
            CREATE INDEX IF NOT EXISTS photos_submission_idx ON photos(submission_id);
            CREATE INDEX IF NOT EXISTS submissions_challenge_idx ON submissions(challenge);
            CREATE TABLE IF NOT EXISTS admin_auth (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                password_hash TEXT NOT NULL,
                session_version INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        settings_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(settings)")
        }
        if "wheel_enabled" not in settings_columns:
            db.execute(
                "ALTER TABLE settings ADD COLUMN wheel_enabled INTEGER NOT NULL DEFAULT 1"
            )
        if "prompts_json" not in settings_columns:
            db.execute("ALTER TABLE settings ADD COLUMN prompts_json TEXT")
        submission_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(submissions)")
        }
        if "table_id" not in submission_columns:
            db.execute(
                "ALTER TABLE submissions ADD COLUMN table_id INTEGER REFERENCES tables(id) ON DELETE SET NULL"
            )
        db.execute(
            "CREATE INDEX IF NOT EXISTS submissions_table_idx ON submissions(table_id)"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings (id, state, countdown_hours) VALUES (1, 'open', ?)",
            (DEFAULT_COUNTDOWN_HOURS,),
        )
        if ADMIN_PASSWORD and not db.execute(
            "SELECT 1 FROM admin_auth WHERE id = 1"
        ).fetchone():
            db.execute(
                "INSERT OR IGNORE INTO admin_auth (id, password_hash) VALUES (1, ?)",
                (hash_admin_password(ADMIN_PASSWORD),),
            )


@app.on_event("startup")
def startup() -> None:
    initialize_database()


def get_app_state(db: sqlite3.Connection) -> tuple[str, datetime | None, int, bool]:
    row = db.execute(
        "SELECT state, go_live_at, countdown_hours, wheel_enabled FROM settings WHERE id = 1"
    ).fetchone()
    state = row["state"]
    go_live_at = datetime.fromisoformat(row["go_live_at"]) if row["go_live_at"] else None
    if state == "closed" and go_live_at and now_utc() >= go_live_at:
        db.execute("UPDATE settings SET state = 'live' WHERE id = 1")
        state = "live"
    return state, go_live_at, row["countdown_hours"], bool(row["wheel_enabled"])


def get_prompts(db: sqlite3.Connection) -> list[str]:
    row = db.execute("SELECT prompts_json FROM settings WHERE id = 1").fetchone()
    if row and row["prompts_json"]:
        try:
            saved_prompts = json.loads(row["prompts_json"])
        except (TypeError, json.JSONDecodeError):
            saved_prompts = None
        if (
            isinstance(saved_prompts, list)
            and saved_prompts
            and all(isinstance(prompt, str) and prompt.strip() for prompt in saved_prompts)
        ):
            return saved_prompts
    return DEFAULT_PROMPTS


def session_signature(payload: str) -> str:
    return hmac.new(SESSION_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def is_admin(request: Request) -> bool:
    if not SESSION_KEY:
        return False
    token = request.cookies.get("bitta_admin", "")
    try:
        payload, signature = token.rsplit(".", 1)
        try:
            version_text, timestamp_text, _ = payload.split(":", 2)
            session_version = int(version_text)
        except ValueError:
            # Accept pre-versioned sessions until the admin changes the password.
            timestamp_text, _ = payload.split(":", 1)
            session_version = 1
        timestamp = int(timestamp_text)
    except (ValueError, IndexError):
        return False
    if abs(time.time() - timestamp) >= 7 * 24 * 60 * 60:
        return False
    if not hmac.compare_digest(signature, session_signature(payload)):
        return False
    with database() as db:
        auth = db.execute(
            "SELECT session_version FROM admin_auth WHERE id = 1"
        ).fetchone()
    return bool(auth and session_version == auth["session_version"])


def authenticate_admin(password: str) -> int | None:
    if len(password.encode("utf-8")) > 1024:
        return None
    with database() as db:
        auth = db.execute(
            "SELECT password_hash, session_version FROM admin_auth WHERE id = 1"
        ).fetchone()
        if auth and verify_password_hash(password, auth["password_hash"]):
            return auth["session_version"]
    return None


def has_admin_password() -> bool:
    with database() as db:
        return db.execute(
            "SELECT 1 FROM admin_auth WHERE id = 1"
        ).fetchone() is not None


def require_admin(request: Request) -> RedirectResponse | None:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


def page(request: Request, name: str, **context: object) -> HTMLResponse:
    needs_prompts = name in {"guest.html", "admin.html"} and "prompts" not in context
    needs_wheel_setting = name == "guest.html" and "wheel_enabled" not in context
    if needs_prompts or needs_wheel_setting:
        with database() as db:
            if needs_prompts:
                context["prompts"] = get_prompts(db)
            if needs_wheel_setting:
                setting = db.execute(
                    "SELECT wheel_enabled FROM settings WHERE id = 1"
                ).fetchone()
                context["wheel_enabled"] = bool(setting["wheel_enabled"])
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "request": request, "text": TEXT,
            "max_photos": MAX_PHOTOS_PER_SUBMISSION, "max_upload_mb": MAX_UPLOAD_MB,
            **context,
        },
    )


@app.get("/", response_class=HTMLResponse, name="home")
def home(request: Request) -> HTMLResponse:
    with database() as db:
        state, go_live_at, _, wheel_enabled = get_app_state(db)
        if state == "open":
            state = "table_required"
        prompts = get_prompts(db)
        photos = db.execute(
            """SELECT photos.id, submissions.challenge
               FROM photos JOIN submissions ON submissions.id = photos.submission_id
               WHERE photos.approval = 'approved' ORDER BY photos.id DESC"""
        ).fetchall() if state == "live" else []
    gallery_prompts = list(dict.fromkeys([*prompts, *(photo["challenge"] for photo in photos)]))
    gallery_groups = {prompt: [] for prompt in gallery_prompts}
    for photo in photos:
        gallery_groups.setdefault(photo["challenge"], []).append(photo["id"])
    return page(
        request, "guest.html", state=state,
        go_live_at=iso_utc(go_live_at) if go_live_at else "", gallery_groups=gallery_groups,
        has_gallery=any(gallery_groups.values()), wheel_enabled=wheel_enabled,
        prompts=prompts, gallery_prompts=gallery_prompts,
    )


def table_photo_count(db: sqlite3.Connection, table_id: int) -> int:
    return db.execute(
        """SELECT COUNT(*)
           FROM photos JOIN submissions ON submissions.id = photos.submission_id
           WHERE submissions.table_id = ?""",
        (table_id,),
    ).fetchone()[0]


def render_table_upload(
    request: Request, token: str, error: str = ""
) -> Response:
    with database() as db:
        table = db.execute(
            "SELECT id, name, token, photo_limit FROM tables WHERE token = ?", (token,)
        ).fetchone()
        if not table:
            return Response(status_code=404)
        state, _, _, wheel_enabled = get_app_state(db)
        if state != "open":
            return RedirectResponse("/", status_code=303)
        remaining = max(0, table["photo_limit"] - table_photo_count(db, table["id"]))
        prompts = get_prompts(db)
    return page(
        request,
        "guest.html",
        state="open" if remaining else "table_full",
        error=error,
        table_name=table["name"],
        table_token=token,
        table_remaining=remaining,
        table_limit=table["photo_limit"],
        max_photos=min(MAX_PHOTOS_PER_SUBMISSION, remaining),
        wheel_enabled=wheel_enabled,
        prompts=prompts,
    )


@app.get("/table/{token}", response_class=HTMLResponse)
def table_guest_page(request: Request, token: str) -> Response:
    return render_table_upload(request, token)


@app.post("/table/{token}/submit")
async def submit(
    request: Request,
    token: str,
    guest_name: str = Form(default=""),
    challenge: str = Form(...),
    photos: list[UploadFile] = File(...),
) -> Response:
    with database() as db:
        table = db.execute(
            "SELECT id, photo_limit FROM tables WHERE token = ?", (token,)
        ).fetchone()
        if not table:
            return Response(status_code=404)
        state, _, _, _ = get_app_state(db)
        if state != "open":
            return RedirectResponse("/", status_code=303)
        remaining = max(0, table["photo_limit"] - table_photo_count(db, table["id"]))
        prompts = get_prompts(db)
    if not remaining:
        return render_table_upload(request, token)
    if len(photos) > min(MAX_PHOTOS_PER_SUBMISSION, remaining):
        return render_table_upload(
            request,
            token,
            error=f"Choose no more than {min(MAX_PHOTOS_PER_SUBMISSION, remaining)} photos for this submission.",
        )
    if challenge not in prompts:
        return render_table_upload(
            request, token, error="Please choose a challenge from the list."
        )

    validated: list[tuple[bytes, str, str]] = []
    for upload in photos:
        content = await upload.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
        if not content:
            continue
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            return render_table_upload(
                request, token, error=f"Each photo must be smaller than {MAX_UPLOAD_MB} MB."
            )
        try:
            image = Image.open(io.BytesIO(content))
            image.verify()
            image_format = image.format
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            return render_table_upload(
                request, token, error="One of those files is not a supported image."
            )
        if image_format == "HEIF":
            image = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGB")
            converted = io.BytesIO()
            image.save(converted, format="JPEG", quality=90)
            content = converted.getvalue()
            image_format = "JPEG"
        extensions = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
        extension = extensions.get(image_format or "")
        if not extension:
            return render_table_upload(
                request, token, error="Please upload JPG, PNG, WEBP, or GIF photos."
            )
        original_name = Path(upload.filename or "photo").name[:200]
        validated.append((content, extension, original_name))
    if not validated:
        return render_table_upload(
            request, token, error="Choose at least one photo to submit."
        )

    stored: list[str] = []
    try:
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            state, _, _, _ = get_app_state(db)
            if state != "open":
                return RedirectResponse("/", status_code=303)
            table = db.execute(
                "SELECT id, photo_limit FROM tables WHERE token = ?", (token,)
            ).fetchone()
            if not table:
                return Response(status_code=404)
            remaining = max(
                0, table["photo_limit"] - table_photo_count(db, table["id"])
            )
            if len(validated) > remaining:
                return render_table_upload(
                    request,
                    token,
                    error="This table's photo limit was reached while you were uploading. Please choose fewer photos.",
                )
            cursor = db.execute(
                """INSERT INTO submissions
                   (guest_name, challenge, created_at, table_id)
                   VALUES (?, ?, ?, ?)""",
                (guest_name.strip()[:100] or None, challenge, iso_utc(now_utc()), table["id"]),
            )
            submission_id = cursor.lastrowid
            for content, extension, original_name in validated:
                filename = f"{uuid.uuid4().hex}{extension}"
                (UPLOAD_DIR / filename).write_bytes(content)
                stored.append(filename)
                db.execute(
                    "INSERT INTO photos (submission_id, filename, original_name) VALUES (?, ?, ?)",
                    (submission_id, filename, original_name),
                )
    except Exception:
        for filename in stored:
            (UPLOAD_DIR / filename).unlink(missing_ok=True)
        raise
    return page(request, "thanks.html")


@app.get("/photos/{photo_id}")
def public_photo(photo_id: int) -> Response:
    with database() as db:
        state, _, _, _ = get_app_state(db)
        photo = db.execute(
            "SELECT filename FROM photos WHERE id = ? AND approval = 'approved'", (photo_id,)
        ).fetchone()
    if state != "live" or not photo:
        return Response(status_code=404)
    path = UPLOAD_DIR / photo["filename"]
    return FileResponse(path) if path.is_file() else Response(status_code=404)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login(
    request: Request, error: str = "", password_changed: bool = False
) -> HTMLResponse:
    if is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return page(
        request, "login.html", error=error,
        password_changed=password_changed,
        password_configured=has_admin_password(),
    )


@app.post("/admin/login")
def admin_login_submit(request: Request, password: str = Form(...)) -> Response:
    session_version = authenticate_admin(password)
    if session_version is not None:
        payload = f"{session_version}:{int(time.time())}:{secrets.token_urlsafe(12)}"
        token = f"{payload}.{session_signature(payload)}"
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            "bitta_admin", token, httponly=True,
            secure=request.url.scheme == "https" or PUBLIC_URL.startswith("https://"),
            samesite="strict", max_age=7 * 24 * 60 * 60, path="/",
        )
        return response
    return page(
        request, "login.html", error="That password didn't match.",
        password_configured=has_admin_password(),
    )


@app.post("/admin/logout")
def admin_logout() -> Response:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("bitta_admin", path="/")
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    prompt_error: str = "",
    prompts_saved: bool = False,
    table_error: str = "",
    table_saved: bool = False,
) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        state, go_live_at, countdown_hours, wheel_enabled = get_app_state(db)
        prompts = get_prompts(db)
        total_submissions = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        total_photos = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        pending_count = db.execute("SELECT COUNT(*) FROM photos WHERE approval = 'pending'").fetchone()[0]
        category_counts = {
            row["challenge"]: row["total"]
            for row in db.execute("SELECT challenge, COUNT(*) AS total FROM submissions GROUP BY challenge")
        }
        category_prompts = list(dict.fromkeys([*prompts, *category_counts]))
        queue = db.execute(
            """SELECT photos.id, photos.filename, photos.original_name, photos.approval,
                      submissions.guest_name, submissions.table_number, tables.name AS table_name,
                      submissions.challenge, submissions.created_at
               FROM photos JOIN submissions ON submissions.id = photos.submission_id
               LEFT JOIN tables ON tables.id = submissions.table_id
               ORDER BY CASE photos.approval WHEN 'pending' THEN 0 ELSE 1 END, photos.id DESC"""
        ).fetchall()
        tables = db.execute(
            """SELECT tables.id, tables.name, tables.token, tables.photo_limit,
                      COUNT(photos.id) AS photo_count
               FROM tables
               LEFT JOIN submissions ON submissions.table_id = tables.id
               LEFT JOIN photos ON photos.submission_id = submissions.id
               GROUP BY tables.id
               ORDER BY tables.id"""
        ).fetchall()
    guest_url = PUBLIC_URL or str(request.url_for("home"))
    table_qrs = []
    for table in tables:
        table_url = f"{guest_url.rstrip('/')}/table/{table['token']}"
        qr_buffer = io.BytesIO()
        qrcode.make(table_url).save(qr_buffer, format="PNG")
        table_qrs.append(
            {
                "id": table["id"],
                "name": table["name"],
                "token": table["token"],
                "photo_limit": table["photo_limit"],
                "photo_count": table["photo_count"],
                "url": table_url,
                "qr_data": "data:image/png;base64,"
                + base64.b64encode(qr_buffer.getvalue()).decode("ascii"),
            }
        )
    prompt_errors = {
        "empty": "Add at least one prompt before saving.",
        "too_many": "You can add up to 50 prompts.",
        "too_long": "Each prompt must be 200 characters or fewer.",
        "duplicate": "Prompts must be unique.",
    }
    return page(
        request, "admin.html", state=state, go_live_at=iso_utc(go_live_at) if go_live_at else "",
        countdown_hours=countdown_hours, total_submissions=total_submissions, total_photos=total_photos,
        pending_count=pending_count, category_counts=category_counts,
        category_prompts=category_prompts, queue=queue, prompts=prompts,
        guest_url=guest_url, tables=table_qrs, wheel_enabled=wheel_enabled,
        prompt_error=prompt_errors.get(prompt_error, ""), prompts_saved=prompts_saved,
        table_error={
            "invalid": "Enter a table name and a photo limit of at least 1.",
            "duplicate": "A table with that name already exists.",
            "missing": "That table could not be found.",
        }.get(table_error, ""),
        table_saved=table_saved,
    )


@app.get("/admin/password", response_class=HTMLResponse)
def admin_password_page(request: Request, error: str = "") -> Response:
    if redirect := require_admin(request):
        return redirect
    return page(request, "password.html", error=error)


@app.post("/admin/password")
def update_admin_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> Response:
    if redirect := require_admin(request):
        return redirect
    if len(new_password) < 12:
        return page(
            request, "password.html", error="The new password must be at least 12 characters."
        )
    if len(new_password.encode("utf-8")) > 1024:
        return page(
            request, "password.html", error="The new password is too long."
        )
    if not hmac.compare_digest(
        new_password.encode("utf-8"), confirm_password.encode("utf-8")
    ):
        return page(request, "password.html", error="The new passwords do not match.")

    with database() as db:
        auth = db.execute(
            "SELECT password_hash FROM admin_auth WHERE id = 1"
        ).fetchone()
        if not auth or not verify_password_hash(current_password, auth["password_hash"]):
            return page(
                request, "password.html", error="The current password is incorrect."
            )
        db.execute(
            """UPDATE admin_auth
               SET password_hash = ?, session_version = session_version + 1
               WHERE id = 1""",
            (hash_admin_password(new_password),),
        )

    response = RedirectResponse("/admin/login?password_changed=true", status_code=303)
    response.delete_cookie("bitta_admin", path="/")
    return response


@app.post("/admin/state")
def update_state(request: Request, action: str = Form(...), countdown_hours: int = Form(default=72)) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        if action == "close":
            hours = min(24 * 30, max(1, countdown_hours))
            go_live_at = iso_utc(now_utc() + timedelta(hours=hours))
            db.execute("UPDATE settings SET state = 'closed', go_live_at = ?, countdown_hours = ? WHERE id = 1", (go_live_at, hours))
        elif action == "live":
            db.execute("UPDATE settings SET state = 'live', go_live_at = NULL WHERE id = 1")
        elif action == "open":
            db.execute("UPDATE settings SET state = 'open', go_live_at = NULL WHERE id = 1")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tables")
def create_table(
    request: Request, name: str = Form(...), photo_limit: int = Form(...)
) -> Response:
    if redirect := require_admin(request):
        return redirect
    name = name.strip()
    if not name or len(name) > 80 or not 1 <= photo_limit <= 1_000_000:
        return RedirectResponse("/admin?table_error=invalid#tables", status_code=303)
    try:
        with database() as db:
            db.execute(
                "INSERT INTO tables (name, token, photo_limit) VALUES (?, ?, ?)",
                (name, secrets.token_urlsafe(24), photo_limit),
            )
    except sqlite3.IntegrityError:
        return RedirectResponse("/admin?table_error=duplicate#tables", status_code=303)
    return RedirectResponse("/admin?table_saved=true#tables", status_code=303)


@app.post("/admin/tables/{table_id}")
def update_table(
    request: Request,
    table_id: int,
    name: str = Form(...),
    photo_limit: int = Form(...),
) -> Response:
    if redirect := require_admin(request):
        return redirect
    name = name.strip()
    if not name or len(name) > 80 or not 1 <= photo_limit <= 1_000_000:
        return RedirectResponse("/admin?table_error=invalid#tables", status_code=303)
    try:
        with database() as db:
            cursor = db.execute(
                "UPDATE tables SET name = ?, photo_limit = ? WHERE id = ?",
                (name, photo_limit, table_id),
            )
            if not cursor.rowcount:
                return RedirectResponse("/admin?table_error=missing#tables", status_code=303)
    except sqlite3.IntegrityError:
        return RedirectResponse("/admin?table_error=duplicate#tables", status_code=303)
    return RedirectResponse("/admin?table_saved=true#tables", status_code=303)


@app.post("/admin/tables/{table_id}/rotate")
def rotate_table_token(request: Request, table_id: int) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        cursor = db.execute(
            "UPDATE tables SET token = ? WHERE id = ?",
            (secrets.token_urlsafe(24), table_id),
        )
        if not cursor.rowcount:
            return RedirectResponse("/admin?table_error=missing#tables", status_code=303)
    return RedirectResponse("/admin?table_saved=true#tables", status_code=303)


@app.get("/admin/tables/{table_id}/qr.png")
def download_table_qr(request: Request, table_id: int) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        table = db.execute(
            "SELECT token FROM tables WHERE id = ?", (table_id,)
        ).fetchone()
    if not table:
        return Response(status_code=404)
    guest_url = PUBLIC_URL or str(request.url_for("home"))
    qr_buffer = io.BytesIO()
    qrcode.make(
        f"{guest_url.rstrip('/')}/table/{table['token']}"
    ).save(qr_buffer, format="PNG")
    return Response(
        qr_buffer.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="table-{table_id}-qr.png"'},
    )


@app.post("/admin/wheel")
def update_wheel(request: Request, enabled: bool = Form(default=False)) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        db.execute(
            "UPDATE settings SET wheel_enabled = ? WHERE id = 1",
            (int(enabled),),
        )
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/prompts")
def update_prompts(
    request: Request, prompt_text: str = Form(..., alias="prompts")
) -> Response:
    if redirect := require_admin(request):
        return redirect
    prompts = [line.strip() for line in prompt_text.splitlines() if line.strip()]
    error = ""
    if not prompts:
        error = "empty"
    elif len(prompts) > 50:
        error = "too_many"
    elif any(len(prompt) > 200 for prompt in prompts):
        error = "too_long"
    elif len({prompt.casefold() for prompt in prompts}) != len(prompts):
        error = "duplicate"
    if error:
        return RedirectResponse(f"/admin?prompt_error={error}", status_code=303)

    with database() as db:
        db.execute(
            "UPDATE settings SET prompts_json = ? WHERE id = 1",
            (json.dumps(prompts, ensure_ascii=False),),
        )
    return RedirectResponse("/admin?prompts_saved=true", status_code=303)


@app.post("/admin/photo/{photo_id}/delete")
def delete_photo(request: Request, photo_id: int) -> Response:
    if redirect := require_admin(request):
        return redirect

    filename = None
    with database() as db:
        photo = db.execute(
            "SELECT submission_id, filename FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        if photo:
            filename = photo["filename"]
            submission_id = photo["submission_id"]
            db.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
            remaining = db.execute(
                "SELECT 1 FROM photos WHERE submission_id = ? LIMIT 1", (submission_id,)
            ).fetchone()
            if not remaining:
                db.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))

    if filename:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)
    return RedirectResponse("/admin#moderation", status_code=303)


@app.post("/admin/photo/{photo_id}/{decision}")
def moderate_photo(request: Request, photo_id: int, decision: str) -> Response:
    if redirect := require_admin(request):
        return redirect
    if decision in {"approve", "reject", "pending"}:
        approval = {"approve": "approved", "reject": "rejected", "pending": "pending"}[decision]
        with database() as db:
            db.execute("UPDATE photos SET approval = ? WHERE id = ?", (approval, photo_id))
    return RedirectResponse("/admin#moderation", status_code=303)


@app.post("/admin/bulk")
def bulk_moderate(request: Request, decision: str = Form(...)) -> Response:
    if redirect := require_admin(request):
        return redirect
    if decision in {"approve", "reject"}:
        approval = "approved" if decision == "approve" else "rejected"
        with database() as db:
            db.execute("UPDATE photos SET approval = ? WHERE approval = 'pending'", (approval,))
    return RedirectResponse("/admin#moderation", status_code=303)


@app.get("/admin/photo/{photo_id}/download")
def download_photo(request: Request, photo_id: int) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        photo = db.execute("SELECT filename, original_name FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not photo or not (UPLOAD_DIR / photo["filename"]).is_file():
        return Response(status_code=404)
    return FileResponse(UPLOAD_DIR / photo["filename"], filename=photo["original_name"])


@app.get("/admin/photo/{photo_id}")
def admin_photo(request: Request, photo_id: int) -> Response:
    if redirect := require_admin(request):
        return redirect
    with database() as db:
        photo = db.execute("SELECT filename FROM photos WHERE id = ?", (photo_id,)).fetchone()
    path = UPLOAD_DIR / photo["filename"] if photo else None
    return FileResponse(path) if path and path.is_file() else Response(status_code=404)


@app.get("/admin/download-approved")
def download_approved(request: Request) -> Response:
    if redirect := require_admin(request):
        return redirect
    buffer = io.BytesIO()
    with database() as db:
        photos = db.execute(
            "SELECT id, filename, original_name FROM photos WHERE approval = 'approved' ORDER BY id"
        ).fetchall()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for photo in photos:
            path = UPLOAD_DIR / photo["filename"]
            if path.is_file():
                archive.write(path, arcname=f"{photo['id']}-{photo['original_name']}")
    return Response(
        content=buffer.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="bitta-craic-approved-photos.zip"'},
    )


@app.get("/gallery", response_class=HTMLResponse)
def gallery(request: Request) -> Response:
    return RedirectResponse("/", status_code=303)
