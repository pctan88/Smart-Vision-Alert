import os
import sys
import subprocess
import requests
from pathlib import Path
from functools import wraps
from datetime import date, datetime, timedelta

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

sys.path.insert(0, os.path.dirname(__file__))

from config.settings import settings
from core.notifier import TelegramNotifier
from core.database import EventDB
from core.models import AnalysisResult

app = Flask(__name__)
app.secret_key = (
    os.getenv("PORTAL_SECRET_KEY")
    or settings.WEBHOOK_SECRET
    or "sva-portal-fallback-key-set-PORTAL_SECRET_KEY-in-env"
)
notifier = TelegramNotifier(settings)

PROJECT_ROOT = Path(__file__).resolve().parent
CAPTURES_ROOT = PROJECT_ROOT / "captures"

PORTAL_ADMIN_USERNAME = os.getenv("PORTAL_ADMIN_USERNAME", "admin")
PORTAL_ADMIN_PASSWORD = os.getenv("PORTAL_ADMIN_PASSWORD", "admin123")
RISK_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "safe": 0}


def _nice_chart_max(value):
    """Round chart max upward so event-count ticks are easy to read."""
    value = int(value or 0)
    if value <= 5:
        return 5
    if value <= 10:
        return 10
    if value <= 20:
        return ((value + 4) // 5) * 5
    return ((value + 9) // 10) * 10


def register_webhook():
    """Register the Telegram webhook on startup."""
    if not settings.WEBHOOK_URL or not settings.TELEGRAM_BOT_TOKEN:
        print("WEBHOOK_URL or TELEGRAM_BOT_TOKEN not set. Webhook registration skipped.")
        return

    url     = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook"
    payload = {
        "url":                  settings.WEBHOOK_URL,
        "secret_token":         settings.WEBHOOK_SECRET,
        "drop_pending_updates": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200 and response.json().get("ok"):
            print(f"Webhook registered: {settings.WEBHOOK_URL}")
        else:
            print(f"Webhook registration failed: {response.text}")
    except Exception as e:
        print(f"Error registering webhook: {e}")


register_webhook()


# ── Portal auth and data helpers ──────────────────────────────────────────────

def _db():
    return EventDB(settings)


def _format_dt(value):
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _risk_class(risk_level):
    return (risk_level or "safe").lower()


def _event_kind(event_type):
    event_type = event_type or ""
    if "ObjectMotion" in event_type and "PeopleMotion" in event_type:
        return "Object + Motion"
    if "ObjectMotion" in event_type:
        return "Object"
    if "PeopleMotion" in event_type:
        return "Motion"
    return event_type or "Unknown"


def _ensure_portal_users():
    """Create portal user table and seed one admin if empty."""
    db = _db()
    try:
        conn = db._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS portal_users (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    username      VARCHAR(64) UNIQUE NOT NULL,
                    display_name  VARCHAR(100),
                    email         VARCHAR(120),
                    role          VARCHAR(16) NOT NULL DEFAULT 'viewer',
                    password_hash VARCHAR(255) NOT NULL,
                    is_active     BOOLEAN DEFAULT TRUE,
                    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_login    DATETIME NULL,
                    INDEX idx_role (role)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("SELECT COUNT(*) AS cnt FROM portal_users")
            if cur.fetchone()["cnt"] == 0:
                cur.execute(
                    """INSERT INTO portal_users
                       (username, display_name, role, password_hash)
                       VALUES (%s, %s, 'admin', %s)""",
                    (
                        PORTAL_ADMIN_USERNAME,
                        "Portal Admin",
                        generate_password_hash(PORTAL_ADMIN_PASSWORD),
                    ),
                )

            cur.execute("SHOW COLUMNS FROM portal_users")
            columns = {row["Field"] for row in cur.fetchall()}
            migrations = [
                ("display_name", "ADD COLUMN display_name VARCHAR(100) NULL AFTER username"),
                ("email", "ADD COLUMN email VARCHAR(120) NULL AFTER display_name"),
                ("role", "ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'viewer' AFTER email"),
                ("is_active", "ADD COLUMN is_active BOOLEAN DEFAULT TRUE AFTER password_hash"),
                ("created_at", "ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP AFTER is_active"),
                ("last_login", "ADD COLUMN last_login DATETIME NULL AFTER created_at"),
            ]
            for column, statement in migrations:
                if column not in columns:
                    cur.execute(f"ALTER TABLE portal_users {statement}")

            cur.execute(
                """UPDATE portal_users
                   SET role = COALESCE(NULLIF(role, ''), 'admin'),
                       is_active = COALESCE(is_active, TRUE),
                       display_name = COALESCE(NULLIF(display_name, ''), username)
                   WHERE username = %s""",
                (PORTAL_ADMIN_USERNAME,),
            )
    finally:
        db.close()


def current_user():
    return session.get("user")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("portal_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("portal_login", next=request.path))
        if current_user().get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _query_one(sql, params=()):
    db = _db()
    try:
        conn = db._get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        db.close()


def _query_all(sql, params=()):
    db = _db()
    try:
        conn = db._get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        db.close()


def _dashboard_data():
    today = datetime.now().strftime("%Y-%m-%d")

    summary = _query_one("""
        SELECT
            (SELECT COUNT(*) FROM processed_events WHERE DATE(processed_at) = %s) AS events_today,
            (SELECT COUNT(*) FROM alert_history WHERE DATE(alerted_at) = %s) AS alerts_today,
            (SELECT COUNT(*) FROM analysis_results
             WHERE DATE(analyzed_at) = %s
             AND risk_level IN ('medium', 'high', 'critical')) AS unsafe_today,
            (SELECT COUNT(*) FROM analysis_results
             WHERE DATE(analyzed_at) = %s
             AND (confidence = 0 OR description LIKE 'Could not parse%%')) AS ai_parse_failures,
            (SELECT COUNT(*) FROM analysis_results) AS total_analyses
    """, (today, today, today, today)) or {}

    risk_counts = _query_all("""
        SELECT risk_level, COUNT(*) AS count
        FROM analysis_results
        GROUP BY risk_level
    """)
    risks = {row["risk_level"] or "safe": row["count"] for row in risk_counts}

    events_by_day_rows = _query_all("""
        SELECT DATE(pe.processed_at) AS day,
               COUNT(*) AS total,
               SUM(CASE WHEN ar.risk_level IN ('medium', 'high', 'critical')
                        THEN 1 ELSE 0 END) AS at_risk
        FROM processed_events pe
        LEFT JOIN analysis_results ar ON ar.file_id = pe.file_id
        GROUP BY DATE(pe.processed_at)
        ORDER BY day DESC
        LIMIT 7
    """)
    raw_by_day = {}
    for row in events_by_day_rows:
        day_value = row["day"]
        if isinstance(day_value, datetime):
            day_key = day_value.date()
        elif isinstance(day_value, date):
            day_key = day_value
        else:
            day_key = datetime.fromisoformat(str(day_value)).date()
        raw_by_day[day_key] = {
            "total": int(row["total"] or 0),
            "at_risk": int(row["at_risk"] or 0),
        }

    today_date = datetime.now().date()
    events_by_day = []
    for offset in range(6, -1, -1):
        day_key = today_date - timedelta(days=offset)
        values = raw_by_day.get(day_key, {"total": 0, "at_risk": 0})
        at_risk = min(values["at_risk"], values["total"])
        safe = max(values["total"] - at_risk, 0)
        events_by_day.append({
            "day": day_key,
            "label": day_key.strftime("%a"),
            "date_label": day_key.strftime("%Y-%m-%d"),
            "total": values["total"],
            "safe": safe,
            "at_risk": at_risk,
        })

    chart_max = _nice_chart_max(max((row["total"] for row in events_by_day), default=0))
    tick_step = max(chart_max // 4, 1)
    chart_ticks = [chart_max, chart_max - tick_step, chart_max - tick_step * 2,
                   chart_max - tick_step * 3, 0]
    chart_ticks = sorted({max(0, tick) for tick in chart_ticks}, reverse=True)
    for row in events_by_day:
        row["safe_pct"] = (row["safe"] / chart_max) * 100 if chart_max else 0
        row["risk_pct"] = (row["at_risk"] / chart_max) * 100 if chart_max else 0

    camera_events = _query_all("""
        SELECT base.camera_did,
               COALESCE(MAX(base.camera_name), base.camera_did) AS camera_name,
               MAX(CASE WHEN base.event_type LIKE '%%PeopleMotion%%'
                        THEN base.event_time END) AS last_motion_time,
               MAX(CASE WHEN base.event_type LIKE '%%ObjectMotion%%'
                        THEN base.event_time END) AS last_object_time,
               MAX(base.event_time) AS last_any_time,
               COUNT(*) AS total_events
        FROM processed_events base
        GROUP BY base.camera_did
        ORDER BY camera_name
    """)

    recent_events = _query_all("""
        SELECT pe.file_id, pe.camera_name, pe.camera_did, pe.event_type,
               pe.event_time, pe.duration_sec, pe.frames_saved, pe.capture_dir,
               ar.risk_level, ar.is_safe, ar.confidence,
               ar.description, ar.stillness_warn
        FROM processed_events pe
        LEFT JOIN analysis_results ar ON ar.file_id = pe.file_id
        ORDER BY pe.event_time DESC
        LIMIT 6
    """)

    for row in camera_events:
        row["last_motion_time"] = _format_dt(row["last_motion_time"])
        row["last_object_time"] = _format_dt(row["last_object_time"])
        row["last_any_time"] = _format_dt(row["last_any_time"])

    for row in recent_events:
        row["event_time_display"] = _format_dt(row["event_time"])
        row["risk_class"] = _risk_class(row["risk_level"])
        row["event_kind"] = _event_kind(row["event_type"])

    return {
        "summary": summary,
        "risks": risks,
        "events_by_day": events_by_day,
        "chart_max": chart_max,
        "chart_ticks": chart_ticks,
        "camera_events": camera_events,
        "recent_events": recent_events,
    }


def _event_rows(filters, page=1, per_page=12):
    """Fetch paginated events with filters."""
    where = []
    params = []

    if filters.get("camera"):
        where.append("pe.camera_did = %s")
        params.append(filters["camera"])
    if filters.get("risk"):
        where.append("ar.risk_level = %s")
        params.append(filters["risk"])
    if filters.get("q"):
        where.append("(pe.file_id LIKE %s OR pe.camera_name LIKE %s OR ar.description LIKE %s)")
        q = f"%{filters['q']}%"
        params.extend([q, q, q])
    if filters.get("window") == "24h":
        where.append("pe.event_time >= NOW() - INTERVAL 1 DAY")
    elif filters.get("window") == "7d":
        where.append("pe.event_time >= NOW() - INTERVAL 7 DAY")
    elif filters.get("window") == "30d":
        where.append("pe.event_time >= NOW() - INTERVAL 30 DAY")

    clause = "WHERE " + " AND ".join(where) if where else ""
    total_result = _query_one(f"""
        SELECT COUNT(*) AS total
        FROM processed_events pe
        LEFT JOIN analysis_results ar ON ar.file_id = pe.file_id
        {clause}
    """, tuple(params))
    total = int(total_result["total"] if total_result else 0)
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = max(1, min(int(page or 1), total_pages))
    offset = (page - 1) * per_page

    rows = _query_all(f"""
        SELECT pe.file_id, pe.camera_name, pe.camera_did, pe.event_type,
               pe.event_time, pe.duration_sec, pe.frames_saved, pe.capture_dir,
               ar.risk_level, ar.is_safe, ar.confidence, ar.description,
               ar.hazards, ar.motion_detected, ar.stillness_warn,
               ar.model_used, ar.analyzed_at,
               ah.telegram_ok, ah.alerted_at
        FROM processed_events pe
        LEFT JOIN analysis_results ar ON ar.file_id = pe.file_id
        LEFT JOIN (
            SELECT h1.file_id, h1.telegram_ok, h1.alerted_at
            FROM alert_history h1
            INNER JOIN (
                SELECT file_id, MAX(alerted_at) AS latest_alert
                FROM alert_history
                GROUP BY file_id
            ) h2 ON h2.file_id = h1.file_id AND h2.latest_alert = h1.alerted_at
        ) ah ON ah.file_id = pe.file_id
        {clause}
        ORDER BY pe.event_time DESC
        LIMIT %s OFFSET %s
    """, tuple(params) + (per_page, offset))

    for row in rows:
        row["event_time_display"] = _format_dt(row["event_time"])
        row["analyzed_at_display"] = _format_dt(row["analyzed_at"])
        row["alerted_at_display"] = _format_dt(row["alerted_at"])
        row["risk_class"] = _risk_class(row["risk_level"])
        row["event_kind"] = _event_kind(row["event_type"])
        row["image_url"] = url_for("portal_capture_image", file_id=row["file_id"])

    return rows, total, page, total_pages


def _event_options():
    cameras = _query_all("""
        SELECT camera_did, COALESCE(MAX(camera_name), camera_did) AS camera_name
        FROM processed_events
        GROUP BY camera_did
        ORDER BY camera_name
    """)
    return cameras


def _capture_file_for_event(file_id):
     """Get the first/latest capture frame for an event (for backwards compatibility)."""
     row = _query_one(
         "SELECT capture_dir FROM processed_events WHERE file_id = %s LIMIT 1",
         (file_id,),
     )
     if not row or not row.get("capture_dir"):
         return None
 
     capture_dir = (PROJECT_ROOT / row["capture_dir"]).resolve()
     try:
         capture_dir.relative_to(CAPTURES_ROOT.resolve())
     except ValueError:
         return None
 
     if not capture_dir.exists():
         return None
 
     image_files = sorted(
         [
             p for p in capture_dir.rglob("*")
             if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
         ],
         key=lambda p: p.stat().st_mtime,
         reverse=True,
     )
     return image_files[0] if image_files else None


def _capture_files_for_event(file_id):
    """Get all capture frames for an event, sorted by frame label/time."""
    row = _query_one(
        "SELECT capture_dir FROM processed_events WHERE file_id = %s LIMIT 1",
        (file_id,),
    )
    if not row or not row.get("capture_dir"):
        return []

    capture_dir = (PROJECT_ROOT / row["capture_dir"]).resolve()
    try:
        capture_dir.relative_to(CAPTURES_ROOT.resolve())
    except ValueError:
        return []

    if not capture_dir.exists():
        return []

    def sort_key(path):
        relative = str(path.relative_to(capture_dir))
        return (relative.replace("thumb", "0000_thumb")
                        .replace("first", "0001_first")
                        .replace("t", "1_t"))

    return sorted(
        [
            p for p in capture_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        ],
        key=sort_key,
    )


@app.context_processor
def inject_portal_context():
    return {
        "current_user": current_user(),
        "risk_order": RISK_ORDER,
    }


# ── Portal pages ──────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def portal_root():
    if current_user():
        return redirect(url_for("portal_dashboard"))
    return redirect(url_for("portal_login"))


@app.route("/login", methods=["GET", "POST"])
def portal_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        try:
            _ensure_portal_users()
            user = _query_one(
                "SELECT * FROM portal_users WHERE username = %s AND is_active = 1 LIMIT 1",
                (username,),
            )
        except Exception as exc:
            flash(f"Database unavailable: {exc}", "error")
            return render_template("login.html")

        pwhash = user.get("password_hash") if user else None
        try:
            password_ok = bool(user and pwhash and check_password_hash(pwhash, password))
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Password check error: {e}", exc_info=True)
            password_ok = False

        if password_ok:
            try:
                db = _db()
                try:
                    conn = db._get_conn()
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE portal_users SET last_login = NOW() WHERE id = %s",
                            (user["id"],),
                        )
                finally:
                    db.close()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to update last_login: {e}")

            try:
                session.permanent = remember
                app.permanent_session_lifetime = timedelta(days=14)
                session["user"] = {
                    "id": user.get("id"),
                    "username": user.get("username"),
                    "display_name": user.get("display_name") or user.get("username"),
                    "role": user.get("role", "viewer"),
                }
                return redirect(request.args.get("next") or url_for("portal_dashboard"))
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Session/redirect error: {e}", exc_info=True)
                flash(f"Login error ({type(e).__name__}): {e}", "error")
                return render_template("login.html")

        flash("Invalid operator ID or authentication key.", "error")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def portal_logout():
    session.clear()
    return redirect(url_for("portal_login"))


@app.route("/dashboard", methods=["GET"])
@login_required
def portal_dashboard():
    try:
        data = _dashboard_data()
    except Exception as exc:
        data = None
        flash(f"Dashboard data unavailable: {exc}", "error")
    return render_template("dashboard.html", active="overview", data=data)


@app.route("/events", methods=["GET"])
@login_required
def portal_events():
    filters = {
        "window": request.args.get("window", "24h"),
        "camera": request.args.get("camera", ""),
        "risk": request.args.get("risk", ""),
        "q": request.args.get("q", "").strip(),
    }
    try:
        requested_page = int(request.args.get("page", 1))
    except ValueError:
        requested_page = 1

    per_page = 12
    try:
        rows, total, current_page, total_pages = _event_rows(
            filters,
            page=requested_page,
            per_page=per_page,
        )
        cameras = _event_options()
    except Exception as exc:
        rows = []
        cameras = []
        total = 0
        current_page = 1
        total_pages = 1
        flash(f"Event data unavailable: {exc}", "error")

    selected = request.args.get("event") or (rows[0]["file_id"] if rows else "")
    selected_row = next((r for r in rows if r["file_id"] == selected), rows[0] if rows else None)

    frame_urls = []
    available_frames = 0
    if selected_row:
        frame_files = _capture_files_for_event(selected_row["file_id"])
        available_frames = len(frame_files)
        frame_urls = [
            url_for("portal_capture_image", file_id=selected_row["file_id"], frame=i)
            for i in range(available_frames)
        ]

    page_start = ((current_page - 1) * per_page + 1) if total else 0
    page_end = min(current_page * per_page, total)
    page_numbers = [
        num for num in range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1)
    ]

    return render_template(
        "events.html",
        active="events",
        events=rows,
        selected=selected_row,
        filters=filters,
        cameras=cameras,
        frame_urls=frame_urls,
        available_frames=available_frames,
        page_start=page_start,
        page_end=page_end,
        page_numbers=page_numbers,
        current_page=current_page,
        total_pages=total_pages,
        total_events=total,
    )


@app.route("/users", methods=["GET", "POST"])
@admin_required
def portal_users():
    _ensure_portal_users()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        display_name = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip()
        role = request.form.get("role", "viewer")
        password = request.form.get("password", "")

        if role not in {"admin", "viewer"}:
            flash("Role must be admin or viewer.", "error")
        elif not username or not password:
            flash("Username and password are required.", "error")
        else:
            db = _db()
            try:
                conn = db._get_conn()
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO portal_users
                           (username, display_name, email, role, password_hash)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (
                            username,
                            display_name or username,
                            email,
                            role,
                            generate_password_hash(password),
                        ),
                    )
                flash("User created.", "success")
            except Exception as exc:
                flash(f"Could not create user: {exc}", "error")
            finally:
                db.close()
        return redirect(url_for("portal_users"))

    users = _query_all("""
        SELECT id, username, display_name, email, role, is_active,
               created_at, last_login
        FROM portal_users
        ORDER BY role = 'admin' DESC, username
    """)
    for user in users:
        user["created_at_display"] = _format_dt(user["created_at"])
        user["last_login_display"] = _format_dt(user["last_login"])
    return render_template("users.html", active="users", users=users)


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def portal_user_toggle(user_id):
    if user_id == current_user().get("id"):
        flash("You cannot disable your own active session.", "error")
        return redirect(url_for("portal_users"))
    db = _db()
    try:
        conn = db._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal_users SET is_active = NOT is_active WHERE id = %s",
                (user_id,),
            )
        flash("User status updated.", "success")
    finally:
        db.close()
    return redirect(url_for("portal_users"))


@app.route("/users/<int:user_id>/role", methods=["POST"])
@admin_required
def portal_user_role(user_id):
    role = request.form.get("role", "viewer")
    if role not in {"admin", "viewer"}:
        flash("Invalid role.", "error")
        return redirect(url_for("portal_users"))
    db = _db()
    try:
        conn = db._get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE portal_users SET role = %s WHERE id = %s", (role, user_id))
        flash("Role updated.", "success")
    finally:
        db.close()
    return redirect(url_for("portal_users"))


@app.route("/system", methods=["GET"])
@login_required
def portal_system():
    try:
        stats = {
            "database": _query_one("SELECT DATABASE() AS name, VERSION() AS version"),
            "tables": _query_all("""
                SELECT table_name, table_rows
                FROM information_schema.tables
                WHERE table_schema = %s
                ORDER BY table_name
            """, (settings.DB_NAME,)),
        }
    except Exception as exc:
        stats = None
        flash(f"System data unavailable: {exc}", "error")
    return render_template("system.html", active="system", stats=stats)


@app.route("/capture-image/<file_id>", methods=["GET"])
@login_required
def portal_capture_image(file_id):
    try:
        frame_index = max(0, int(request.args.get("frame", 0)))
    except ValueError:
        frame_index = 0

    frame_files = _capture_files_for_event(file_id)
    path = frame_files[frame_index] if frame_index < len(frame_files) else None
    if path is None:
        path = _capture_file_for_event(file_id)
    if not path:
        abort(404)
    return send_file(path)


# ── Telegram webhook ───────────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
@app.route('/', methods=['POST'])
def telegram_webhook():
    """Handle incoming Telegram commands."""
    secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_token != settings.WEBHOOK_SECRET:
        return jsonify({"status": "unauthorized"}), 401

    try:
        update = request.get_json()
        if update and "message" in update and "text" in update["message"]:
            text      = update["message"]["text"]
            from_user = update["message"].get("from", {})
            username   = from_user.get("username", "")
            first_name = from_user.get("first_name", "User")

            if text.startswith("/check"):
                print(f"Manual check triggered by {first_name} (@{username})")

                try:
                    db = EventDB(settings)
                    db.log_manual_trigger(username, first_name, text)
                    db.close()
                except Exception as db_err:
                    print(f"Failed to log trigger to DB: {db_err}")

                display_name = f"@{username}" if username else first_name
                notifier.send_text(
                    f"🔍 Manual check triggered by {display_name}. "
                    f"Fetching latest CCTV status..."
                )

                # Delegate to monitor_studio.py which triggers Cloud Run
                python_exe  = sys.executable
                script_path = str(PROJECT_ROOT / "monitor_studio.py")
                subprocess.Popen([python_exe, script_path, "--manual-check"])

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500


# ── Internal API (called by Cloud Run) ────────────────────────────────────────

def _verify_internal(req) -> bool:
    """Validate X-Internal-Secret header for Cloud Run callbacks."""
    return req.headers.get("X-Internal-Secret") == settings.INTERNAL_SECRET


@app.route('/api/is-processed', methods=['POST'])
def api_is_processed():
    """Check if an event has already been processed."""
    if not _verify_internal(request):
        return jsonify({"error": "unauthorized"}), 401

    data    = request.get_json(silent=True) or {}
    file_id = data.get("file_id", "")
    if not file_id:
        return jsonify({"error": "file_id required"}), 400

    try:
        db        = EventDB(settings)
        processed = db.is_processed(file_id)
        db.close()
        return jsonify({"processed": processed}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/save-result', methods=['POST'])
def api_save_result():
    """
    Receive event result from Cloud Run.
    Saves image to captures/studio/<did>/<fid>/first/,
    writes processed_events + analysis_results + alert_history to MySQL.
    """
    if not _verify_internal(request):
        return jsonify({"error": "unauthorized"}), 401

    # Payload arrives as JSON body OR as multipart form field when image is attached
    if request.content_type and "multipart" in request.content_type:
        payload = request.form.get("payload")
        if not payload:
            return jsonify({"error": "payload field missing"}), 400
        data = json_loads(payload)
    else:
        data = request.get_json(silent=True) or {}

    if not data.get("file_id"):
        return jsonify({"error": "file_id required"}), 400

    # ── save images ────────────────────────────────────────────────────────
    capture_dir = data.get("capture_dir", "")  # e.g. captures/studio/<did>/<fid>
    saved_images = 0
    if capture_dir and request.files:
        save_dir = PROJECT_ROOT / capture_dir / "frames"
        save_dir.mkdir(parents=True, exist_ok=True)

        img_files = request.files.getlist("images")
        if "image" in request.files:
            img_files.extend(request.files.getlist("image"))

        for idx, img_file in enumerate(img_files):
            filename = secure_filename(img_file.filename or f"frame_{idx:03d}.jpg")
            if not filename:
                filename = f"frame_{idx:03d}.jpg"
            img_path = save_dir / filename
            if img_path.exists():
                stem = img_path.stem
                suffix = img_path.suffix or ".jpg"
                img_path = save_dir / f"{stem}_{idx:03d}{suffix}"
            img_file.save(str(img_path))
            saved_images += 1

    # ── write to MySQL ─────────────────────────────────────────────────────
    try:
        import datetime as _dt

        db         = EventDB(settings)
        file_id    = data["file_id"]
        camera_did = data.get("camera_did", "")
        event_time = _dt.datetime.fromisoformat(data["event_time"])

        db.mark_processed(
            file_id      = file_id,
            camera_did   = camera_did,
            camera_name  = data.get("camera_name", ""),
            event_type   = data.get("event_type", ""),
            event_time   = event_time,
            duration_sec = data.get("duration_sec", 0),
            frames_saved = data.get("frames_saved", 0),
            capture_dir  = capture_dir,
        )

        analysis = data.get("analysis")
        if analysis:
            result = AnalysisResult.from_dict({
                "is_safe":             analysis.get("is_safe", True),
                "risk_level":          analysis.get("risk_level", "safe"),
                "description":         analysis.get("description", ""),
                "detected_hazards":    analysis.get("hazards", []),
                "confidence":          analysis.get("confidence", 0.0),
                "motion_detected":     analysis.get("motion_detected", True),
                "stillness_warning":   analysis.get("stillness_warn", False),
                "temporal_description": "",
            })
            db.save_analysis(
                file_id,
                camera_did,
                result,
                segment_label=analysis.get("segment_label", "first"),
            )

            if data.get("alert_sent"):
                db.save_alert(file_id, camera_did, result,
                              telegram_ok=data.get("telegram_ok", False))

        db.close()
        return jsonify({"status": "ok", "images_saved": saved_images}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def json_loads(s):
    import json
    return json.loads(s)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)
