import os
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, session, has_request_context
import sqlite3
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
from pathlib import Path
from datetime import datetime, date
import csv
import io
import json
import re
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "swimpro-dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("DATABASE_URL")),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)
DB = Path(__file__).with_name("swimtracker.db")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

STYLES = ["Libre", "Espalda", "Pecho", "Mariposa", "Combinado"]

OFFICIAL_EVENTS = {
    "Libre": {25: [50, 100, 200, 400, 800, 1500], 50: [50, 100, 200, 400, 800, 1500]},
    "Espalda": {25: [50, 100, 200], 50: [50, 100, 200]},
    "Pecho": {25: [50, 100, 200], 50: [50, 100, 200]},
    "Mariposa": {25: [50, 100, 200], 50: [50, 100, 200]},
    "Combinado": {25: [100, 200, 400], 50: [200, 400]},
}

class CursorResult:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid
    def fetchone(self): return self._cursor.fetchone()
    def fetchall(self): return self._cursor.fetchall()
    def __iter__(self): return iter(self._cursor)


class PostgresConnection:
    """Small compatibility layer so the existing SQLite-style queries work on PostgreSQL."""
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _sql(sql):
        sql = sql.replace("?", "%s")
        sql = sql.replace(
            "INSERT OR IGNORE INTO profile (id, name) VALUES (1, 'Mi perfil')",
            "INSERT INTO profile (id, name) VALUES (1, 'Mi perfil') ON CONFLICT (id) DO NOTHING",
        )
        if "INSERT OR REPLACE INTO splits" in sql:
            sql = sql.replace("INSERT OR REPLACE INTO splits", "INSERT INTO splits")
            sql += " ON CONFLICT (swim_id, split_distance) DO UPDATE SET split_cs=EXCLUDED.split_cs"

        # V41: goals are unique per user, not globally.
        sql = sql.replace(
            "ON CONFLICT(distance, stroke, pool_length)",
            "ON CONFLICT(user_id, distance, stroke, pool_length)"
        )
        return sql

    def execute(self, sql, params=()):
        sql = self._sql(sql)
        # The app uses lastrowid for these two inserts. PostgreSQL uses RETURNING.
        stripped = sql.lstrip().upper()
        needs_id = (stripped.startswith("INSERT INTO SWIMS") or stripped.startswith("INSERT INTO COMPETITIONS")) and " RETURNING " not in stripped
        if needs_id:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        cur = self._conn.execute(sql, params)
        lastrowid = None
        if needs_id:
            row = cur.fetchone()
            lastrowid = row["id"] if row else None
        return CursorResult(cur, lastrowid)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


def get_db():
    """User-scoped DB connection.

    On PostgreSQL/Neon every protected request sets app.user_id in the
    PostgreSQL session. Row Level Security then guarantees that every query
    can only see the authenticated user's rows.
    """
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL está configurada pero falta psycopg. Ejecuta pip install -r requirements.txt")

        raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)

        if has_request_context() and session.get("user_id"):
            raw.execute(
                "SELECT set_config('app.user_id', %s, false)",
                (str(session["user_id"]),)
            )

        return PostgresConnection(raw)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_admin_db():
    """Administrative connection used only for authentication and migrations."""
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("Falta psycopg para conectar a Neon.")
        raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        raw.execute("SELECT set_config('app.admin', '1', false)")
        return PostgresConnection(raw)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def available_seasons(conn):
    return [
        int(r["y"]) for r in conn.execute("""
            SELECT DISTINCT substr(swim_date,1,4) AS y
            FROM swims
            WHERE swim_date IS NOT NULL
              AND length(swim_date) >= 4
            ORDER BY y DESC
        """).fetchall()
        if r["y"]
    ]

def ensure_column(conn, table, column, definition):
    if DATABASE_URL:
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=? AND column_name=?",
            (table, column),
        ).fetchone()
        if not row:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    else:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_db():
    conn = get_db()
    if DATABASE_URL:
        statements = [
            """CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL DEFAULT 'Mi perfil', birth_date TEXT, sex TEXT)""",
            """CREATE TABLE IF NOT EXISTS competitions (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, competition_date TEXT NOT NULL, location TEXT, pool_length INTEGER NOT NULL, notes TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS swims (id BIGSERIAL PRIMARY KEY, distance INTEGER NOT NULL, stroke TEXT NOT NULL, pool_length INTEGER NOT NULL, time_cs INTEGER NOT NULL, swim_date TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'Competencia', event_name TEXT, notes TEXT, competition_id BIGINT REFERENCES competitions(id) ON DELETE SET NULL, planned_event_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS competition_events (id BIGSERIAL PRIMARY KEY, competition_id BIGINT NOT NULL REFERENCES competitions(id) ON DELETE CASCADE, distance INTEGER NOT NULL, stroke TEXT NOT NULL, target_cs INTEGER, status TEXT NOT NULL DEFAULT 'Pendiente', position TEXT, notes TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS goals (id BIGSERIAL PRIMARY KEY, distance INTEGER NOT NULL, stroke TEXT NOT NULL, pool_length INTEGER NOT NULL, target_cs INTEGER NOT NULL, UNIQUE(distance, stroke, pool_length))""",
            """CREATE TABLE IF NOT EXISTS splits (id BIGSERIAL PRIMARY KEY, swim_id BIGINT NOT NULL REFERENCES swims(id) ON DELETE CASCADE, split_distance INTEGER NOT NULL, split_cs INTEGER NOT NULL, UNIQUE(swim_id, split_distance))""",
        ]
        for statement in statements:
            conn.execute(statement)
    else:
        conn._conn if False else None
        # Keep the original SQLite schema for local/offline development.
        script = """
        CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL DEFAULT 'Mi perfil', birth_date TEXT, sex TEXT);
        CREATE TABLE IF NOT EXISTS competitions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, competition_date TEXT NOT NULL, location TEXT, pool_length INTEGER NOT NULL, notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS swims (id INTEGER PRIMARY KEY AUTOINCREMENT, distance INTEGER NOT NULL, stroke TEXT NOT NULL, pool_length INTEGER NOT NULL, time_cs INTEGER NOT NULL, swim_date TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'Competencia', event_name TEXT, notes TEXT, competition_id INTEGER, planned_event_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(competition_id) REFERENCES competitions(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS competition_events (id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id INTEGER NOT NULL, distance INTEGER NOT NULL, stroke TEXT NOT NULL, target_cs INTEGER, status TEXT NOT NULL DEFAULT 'Pendiente', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(competition_id) REFERENCES competitions(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS goals (id INTEGER PRIMARY KEY AUTOINCREMENT, distance INTEGER NOT NULL, stroke TEXT NOT NULL, pool_length INTEGER NOT NULL, target_cs INTEGER NOT NULL, UNIQUE(distance, stroke, pool_length));
        CREATE TABLE IF NOT EXISTS splits (id INTEGER PRIMARY KEY AUTOINCREMENT, swim_id INTEGER NOT NULL, split_distance INTEGER NOT NULL, split_cs INTEGER NOT NULL, FOREIGN KEY(swim_id) REFERENCES swims(id) ON DELETE CASCADE, UNIQUE(swim_id, split_distance));
        INSERT OR IGNORE INTO profile (id, name) VALUES (1, 'Mi perfil');
        """
        conn.executescript(script)
    ensure_column(conn, "swims", "competition_id", "BIGINT" if DATABASE_URL else "INTEGER")
    ensure_column(conn, "swims", "planned_event_id", "BIGINT" if DATABASE_URL else "INTEGER")
    ensure_column(conn, "competition_events", "position", "TEXT")
    ensure_column(conn, "competition_events", "notes", "TEXT")
    conn.commit()
    conn.close()


def migrate_bundled_sqlite_to_postgres():
    """One-time seed of Neon from the bundled SQLite DB when Neon is still empty."""
    if not DATABASE_URL or not DB.exists():
        return
    pg = get_db()
    try:
        existing = pg.execute("SELECT COUNT(*) AS n FROM swims").fetchone()["n"]
        existing_comp = pg.execute("SELECT COUNT(*) AS n FROM competitions").fetchone()["n"]
        if existing or existing_comp:
            return
        src = sqlite3.connect(DB)
        src.row_factory = sqlite3.Row
        for table in ("profile", "competitions", "competition_events", "swims", "splits", "goals"):
            rows = src.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for row in rows:
                data = dict(row)
                cols = list(data.keys())
                placeholders = ",".join(["?"] * len(cols))
                col_sql = ",".join(cols)
                pg.execute(
                    f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    tuple(data[c] for c in cols),
                )
        src.close()
        # Explicit IDs were copied, so advance PostgreSQL sequences.
        for table in ("competitions", "competition_events", "swims", "splits", "goals"):
            pg.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}','id'), COALESCE((SELECT MAX(id) FROM {table}), 1), true)"
            )
        pg.commit()
        print("✅ Datos existentes migrados de SQLite a Neon PostgreSQL")
    except Exception:
        pg.rollback()
        raise
    finally:
        pg.close()



def init_auth_db():
    """V41 schema migration: users + per-user ownership + PostgreSQL RLS."""
    if not DATABASE_URL:
        # Production multi-user isolation is implemented with PostgreSQL RLS.
        # Local SQLite remains available for single-user development.
        return

    conn = get_admin_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username VARCHAR(40) NOT NULL,
                email VARCHAR(255) NOT NULL,
                password_hash TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMPTZ
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_lower ON users (LOWER(username))")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (LOWER(email))")

        owned_tables = ("profile", "competitions", "swims", "competition_events", "goals", "splits")

        for table in owned_tables:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(id) ON DELETE CASCADE"
            )
            conn.execute(
                f"""ALTER TABLE {table}
                    ALTER COLUMN user_id
                    SET DEFAULT (NULLIF(current_setting('app.user_id', true), '')::BIGINT)"""
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_user_id ON {table}(user_id)")

        # profile previously had id=1 as a global PK. Each user needs their own id=1 row.
        conn.execute("ALTER TABLE profile DROP CONSTRAINT IF EXISTS profile_pkey")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_profile_user_row ON profile(user_id, id)")

        # Goals were globally unique in V40; V41 makes them unique per user.
        conn.execute("ALTER TABLE goals DROP CONSTRAINT IF EXISTS goals_distance_stroke_pool_length_key")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_goals_user_event "
            "ON goals(user_id, distance, stroke, pool_length)"
        )

        # PostgreSQL Row Level Security is the final protection layer.
        policy_expr = (
            "(current_setting('app.admin', true) = '1' "
            "OR user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT)"
        )

        for table in owned_tables:
            conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            conn.execute(f"DROP POLICY IF EXISTS swimpro_user_isolation ON {table}")
            conn.execute(
                f"CREATE POLICY swimpro_user_isolation ON {table} "
                f"USING {policy_expr} WITH CHECK {policy_expr}"
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_legacy_data(conn, user_id):
    """Assign all pre-V41 rows to the first account that registers."""
    for table in ("profile", "competitions", "swims", "competition_events", "goals", "splits"):
        conn.execute(
            f"UPDATE {table} SET user_id=? WHERE user_id IS NULL",
            (user_id,)
        )


def _ensure_user_profile(conn, user_id, username):
    row = conn.execute(
        "SELECT id FROM profile WHERE user_id=? AND id=1",
        (user_id,)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO profile (id, name, user_id) VALUES (1, ?, ?)",
            (username, user_id)
        )


# Gunicorn imports app.py; initialize the schema and seed Neon before serving requests.
init_db()
migrate_bundled_sqlite_to_postgres()
init_auth_db()

def parse_time(value: str) -> int:
    value = value.strip().replace(",", ".")
    if not value:
        raise ValueError("Tiempo vacío")
    parts = value.split(":")
    if len(parts) == 1:
        seconds = float(parts[0])
    elif len(parts) == 2:
        seconds = int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    else:
        raise ValueError("Formato inválido")
    if seconds <= 0:
        raise ValueError("El tiempo debe ser mayor a cero")
    return round(seconds * 100)

def fmt_time(cs: int) -> str:
    total_seconds = cs / 100
    minutes = int(total_seconds // 60)
    seconds = total_seconds - minutes * 60
    return f"{minutes}:{seconds:05.2f}" if minutes else f"{seconds:.2f}"

@app.template_filter("swimtime")
def swimtime_filter(cs):
    return fmt_time(cs)

@app.template_filter("signed_seconds")
def signed_seconds_filter(cs):
    if cs is None:
        return ""
    sign = "+" if cs > 0 else "−" if cs < 0 else ""
    return f"{sign}{abs(cs)/100:.2f}"

def is_valid_event(stroke, pool_length, distance):
    return stroke in OFFICIAL_EVENTS and pool_length in OFFICIAL_EVENTS[stroke] and distance in OFFICIAL_EVENTS[stroke][pool_length]

def age_on_date(birth_date_str, on_date=None):
    if not birth_date_str:
        return None
    try:
        born = datetime.strptime(birth_date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    on_date = on_date or date.today()
    return on_date.year - born.year - ((on_date.month, on_date.day) < (born.month, born.day))

def masters_category(age):
    if age is None:
        return None
    if age < 25:
        return "Open"
    start = 25 + ((age - 25) // 5) * 5
    return f"{start}-{start + 4}"

def pb_for(conn, distance, stroke, pool_length):
    row = conn.execute("""
        SELECT MIN(time_cs) AS pb FROM swims
        WHERE distance=? AND stroke=? AND pool_length=?
    """, (distance, stroke, pool_length)).fetchone()
    return row["pb"] if row else None


AUTH_PUBLIC_ENDPOINTS = {"login", "register", "offline", "static"}


@app.before_request
def require_login():
    endpoint = request.endpoint or ""

    if endpoint in AUTH_PUBLIC_ENDPOINTS or endpoint.startswith("static"):
        return None

    if not session.get("user_id"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "authentication_required"}), 401

        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    return None


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("index"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
            error = "El usuario debe tener 3–40 caracteres y usar solo letras, números, punto, guion o guion bajo."
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Introduce un correo electrónico válido."
        elif len(password) < 8:
            error = "La contraseña debe tener al menos 8 caracteres."
        elif password != confirm:
            error = "Las contraseñas no coinciden."

        if not error:
            conn = get_admin_db()
            try:
                exists = conn.execute(
                    "SELECT id FROM users WHERE LOWER(username)=LOWER(?) OR LOWER(email)=LOWER(?)",
                    (username, email)
                ).fetchone()

                if exists:
                    error = "Ese usuario o correo ya está registrado."
                else:
                    count_before = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

                    row = conn.execute("""
                        INSERT INTO users (username, email, password_hash)
                        VALUES (?, ?, ?)
                        RETURNING id
                    """, (username, email, generate_password_hash(password))).fetchone()

                    user_id = row["id"]

                    # The first account receives every existing V40 record.
                    if count_before == 0:
                        _claim_legacy_data(conn, user_id)

                    _ensure_user_profile(conn, user_id, username)

                    conn.commit()

                    session.clear()
                    session.permanent = True
                    session["user_id"] = user_id
                    session["username"] = username
                    session["email"] = email

                    return redirect(url_for("index"))
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    return render_template("register.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))

    error = None

    if request.method == "POST":
        identity = request.form.get("identity", "").strip()
        password = request.form.get("password", "")

        conn = get_admin_db()
        try:
            user = conn.execute("""
                SELECT *
                FROM users
                WHERE is_active=TRUE
                  AND (LOWER(email)=LOWER(?) OR LOWER(username)=LOWER(?))
                LIMIT 1
            """, (identity, identity)).fetchone()

            if not user or not check_password_hash(user["password_hash"], password):
                error = "Usuario/correo o contraseña incorrectos."
            else:
                conn.execute(
                    "UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=?",
                    (user["id"],)
                )
                conn.commit()

                session.clear()
                session.permanent = True
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["email"] = user["email"]

                next_url = request.args.get("next", "")
                if not next_url.startswith("/") or next_url.startswith("//"):
                    next_url = url_for("index")

                return redirect(next_url)
        finally:
            conn.close()

    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))



@app.route("/offline")
def offline():
    return render_template("offline.html")

@app.route("/")
def index():
    conn = get_db()
    profile = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
    age = age_on_date(profile["birth_date"]) if profile else None
    category = masters_category(age)

    pbs = conn.execute("""
        SELECT distance, stroke, pool_length, MIN(time_cs) AS best_cs, MAX(swim_date) AS last_date
        FROM swims
        GROUP BY distance, stroke, pool_length
        ORDER BY last_date DESC
        LIMIT 4
    """).fetchall()

    recent = conn.execute("""
        SELECT s.*,
               (
                 SELECT MIN(s2.time_cs)
                 FROM swims s2
                 WHERE s2.distance=s.distance AND s2.stroke=s.stroke
                   AND s2.pool_length=s.pool_length
                   AND (s2.swim_date < s.swim_date OR (s2.swim_date=s.swim_date AND s2.id < s.id))
               ) AS prior_pb
        FROM swims s
        ORDER BY s.swim_date DESC, s.id DESC
        LIMIT 3
    """).fetchall()

    upcoming = conn.execute("""
        SELECT c.*,
               COUNT(ce.id) AS event_count
        FROM competitions c
        LEFT JOIN competition_events ce ON ce.competition_id=c.id
        WHERE c.competition_date >= ?
        GROUP BY c.id
        ORDER BY c.competition_date ASC, c.id ASC
        LIMIT 1
    """, (date.today().isoformat(),)).fetchone()

    conn.close()
    return render_template("index.html", profile=profile, age=age, category=category,
                           pbs=pbs, recent=recent, upcoming=upcoming)

@app.route("/marks")
def marks():
    selected_pool = request.args.get("pool", type=int) or 50
    if selected_pool not in (25, 50):
        selected_pool = 50

    conn = get_db()
    seasons = available_seasons(conn)
    selected_year = request.args.get("year", type=int)
    if selected_year and selected_year not in seasons:
        selected_year = None

    rows = conn.execute("""
        SELECT stroke, distance, pool_length,
               MIN(time_cs) AS historical_pb,
               COUNT(*) AS attempts,
               MAX(swim_date) AS last_date
        FROM swims
        WHERE pool_length=?
        GROUP BY stroke, distance, pool_length
        ORDER BY stroke, CAST(distance AS INTEGER)
    """, (selected_pool,)).fetchall()

    stroke_order = ["Libre", "Espalda", "Pecho", "Mariposa", "Combinado"]
    grouped = {}

    for stroke in stroke_order:
        stroke_rows = []

        for r in rows:
            if r["stroke"] != stroke:
                continue

            item = dict(r)
            item["season_pb"] = None
            item["season_attempts"] = 0

            if selected_year:
                season = conn.execute("""
                    SELECT MIN(time_cs) AS season_pb,
                           COUNT(*) AS season_attempts,
                           MAX(swim_date) AS season_last_date
                    FROM swims
                    WHERE distance=?
                      AND stroke=?
                      AND pool_length=?
                      AND substr(swim_date,1,4)=?
                """, (
                    r["distance"], r["stroke"], r["pool_length"],
                    str(selected_year)
                )).fetchone()

                if season:
                    item["season_pb"] = season["season_pb"]
                    item["season_attempts"] = season["season_attempts"]
                    item["season_last_date"] = season["season_last_date"]

            stroke_rows.append(item)

        if stroke_rows:
            grouped[stroke] = stroke_rows

    conn.close()

    return render_template(
        "marks.html",
        grouped=grouped,
        selected_pool=selected_pool,
        seasons=seasons,
        selected_year=selected_year
    )


@app.route("/api/time-preview")
def time_preview():
    try:
        distance = int(request.args.get("distance", ""))
        stroke = request.args.get("stroke", "")
        pool_length = int(request.args.get("pool_length", ""))
        swim_date = request.args.get("swim_date", "")
        time_cs = parse_time(request.args.get("time", ""))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Tiempo inválido"}), 400

    if not is_valid_event(stroke, pool_length, distance):
        return jsonify({"ok": False, "message": "Prueba no válida"}), 400

    conn = get_db()

    duplicate = conn.execute("""
        SELECT id
        FROM swims
        WHERE distance=?
          AND stroke=?
          AND pool_length=?
          AND swim_date=?
          AND time_cs=?
        LIMIT 1
    """, (distance, stroke, pool_length, swim_date, time_cs)).fetchone()

    pb_row = conn.execute("""
        SELECT MIN(time_cs) AS pb
        FROM swims
        WHERE distance=? AND stroke=? AND pool_length=?
    """, (distance, stroke, pool_length)).fetchone()

    pb = pb_row["pb"] if pb_row and pb_row["pb"] is not None else None
    conn.close()

    result = {
        "ok": True,
        "duplicate": bool(duplicate),
        "pb": fmt_time(pb) if pb is not None else None,
        "time": fmt_time(time_cs),
        "status": "first",
        "delta_seconds": None
    }

    if duplicate:
        result["status"] = "duplicate"
    elif pb is None:
        result["status"] = "first"
    else:
        delta = time_cs - pb
        result["delta_seconds"] = round(abs(delta) / 100, 2)
        if delta < 0:
            result["status"] = "new_pb"
        elif delta == 0:
            result["status"] = "equal_pb"
        else:
            result["status"] = "slower"

    return jsonify(result)


@app.route("/add", methods=["GET", "POST"])
def add_swim():
    conn = get_db()
    competitions = conn.execute("SELECT * FROM competitions ORDER BY competition_date DESC, id DESC").fetchall()
    competition_id = request.args.get("competition_id", type=int)
    planned_event_id = request.args.get("planned_event_id", type=int)

    planned_event = None
    if planned_event_id:
        planned_event = conn.execute("""
            SELECT ce.*, c.pool_length, c.competition_date, c.name AS competition_name
            FROM competition_events ce
            JOIN competitions c ON c.id=ce.competition_id
            WHERE ce.id=?
        """, (planned_event_id,)).fetchone()
        if planned_event:
            competition_id = planned_event["competition_id"]

    if request.method == "POST":
        try:
            distance = int(request.form["distance"])
            stroke = request.form["stroke"]
            pool_length = int(request.form["pool_length"])
            time_cs = parse_time(request.form["time"])
            swim_date = request.form["swim_date"]
            kind = request.form.get("kind", "Competencia")
            notes = request.form.get("notes", "").strip()
            selected_competition_id = request.form.get("competition_id", type=int)
            selected_planned_event_id = request.form.get("planned_event_id", type=int)

            if selected_competition_id:
                comp = conn.execute("SELECT * FROM competitions WHERE id=?", (selected_competition_id,)).fetchone()
                if not comp:
                    raise ValueError("Competencia no encontrada.")
                pool_length = comp["pool_length"]
                swim_date = comp["competition_date"]
                kind = "Competencia"

            if not is_valid_event(stroke, pool_length, distance):
                raise ValueError("Prueba no válida para esa piscina.")

            event_name = ""
            if selected_competition_id:
                event_name = conn.execute("SELECT name FROM competitions WHERE id=?", (selected_competition_id,)).fetchone()["name"]

        except (ValueError, KeyError) as e:
            conn.close()
            return render_template("add.html", styles=STYLES, official_events=OFFICIAL_EVENTS,
                                   competitions=competitions, competition_id=competition_id,
                                   planned_event=planned_event, error=str(e),
                                   today=date.today().isoformat())

        duplicate = conn.execute("""
            SELECT id
            FROM swims
            WHERE distance=?
              AND stroke=?
              AND pool_length=?
              AND swim_date=?
              AND time_cs=?
            LIMIT 1
        """, (distance, stroke, pool_length, swim_date, time_cs)).fetchone()

        if duplicate:
            conn.close()
            return render_template(
                "add.html",
                styles=STYLES,
                official_events=OFFICIAL_EVENTS,
                competitions=competitions,
                competition_id=competition_id,
                planned_event=planned_event,
                error="Registro duplicado: este mismo tiempo ya existe para la misma prueba, piscina y fecha.",
                today=swim_date
            )

        cur = conn.execute("""
            INSERT INTO swims
            (distance, stroke, pool_length, time_cs, swim_date, kind, event_name, notes, competition_id, planned_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (distance, stroke, pool_length, time_cs, swim_date, kind, event_name, notes,
              selected_competition_id, selected_planned_event_id))
        swim_id = cur.lastrowid

        for d, t in zip(request.form.getlist("split_distance"), request.form.getlist("split_time")):
            if not d.strip() or not t.strip():
                continue
            try:
                split_distance = int(d)
                split_cs = parse_time(t)
            except ValueError:
                continue
            if 0 < split_distance <= distance:
                conn.execute("""
                    INSERT OR REPLACE INTO splits(swim_id, split_distance, split_cs)
                    VALUES (?, ?, ?)
                """, (swim_id, split_distance, split_cs))

        if selected_planned_event_id:
            conn.execute("UPDATE competition_events SET status='Completada' WHERE id=?", (selected_planned_event_id,))

        conn.commit()
        conn.close()
        return redirect(url_for("swim_detail", swim_id=swim_id))

    conn.close()
    return render_template("add.html", styles=STYLES, official_events=OFFICIAL_EVENTS,
                           competitions=competitions, competition_id=competition_id,
                           planned_event=planned_event, today=date.today().isoformat())

@app.route("/competitions")
def competitions():
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*,
               COUNT(DISTINCT ce.id) AS planned_count,
               SUM(CASE WHEN ce.status='Completada' THEN 1 ELSE 0 END) AS completed_count
        FROM competitions c
        LEFT JOIN competition_events ce ON ce.competition_id=c.id
        GROUP BY c.id
        ORDER BY c.competition_date DESC, c.id DESC
    """).fetchall()
    conn.close()
    return render_template("competitions.html", rows=rows)

@app.route("/competitions/new", methods=["GET", "POST"])
def new_competition():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        competition_date = request.form.get("competition_date", "")
        location = request.form.get("location", "").strip()
        pool_length = request.form.get("pool_length", type=int)
        notes = request.form.get("notes", "").strip()
        if not name or not competition_date or pool_length not in (25, 50):
            return render_template("competition_form.html", error="Completa nombre, fecha y piscina.",
                                   today=date.today().isoformat())
        conn = get_db()
        cur = conn.execute("""
            INSERT INTO competitions(name, competition_date, location, pool_length, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (name, competition_date, location, pool_length, notes))
        cid = cur.lastrowid
        conn.commit()
        conn.close()
        return redirect(url_for("competition_detail", competition_id=cid))
    return render_template("competition_form.html", today=date.today().isoformat())

@app.route("/competitions/<int:competition_id>")
def competition_detail(competition_id):
    conn = get_db()

    comp = conn.execute(
        "SELECT * FROM competitions WHERE id=?", (competition_id,)
    ).fetchone()
    if not comp:
        conn.close()
        return "Competencia no encontrada", 404

    events = conn.execute("""
        SELECT ce.*,
               s.id AS swim_id,
               s.time_cs,
               s.swim_date,
               s.kind,
               s.notes AS swim_notes,
               (
                   SELECT MIN(s2.time_cs)
                   FROM swims s2
                   WHERE s2.distance = ce.distance
                     AND s2.stroke = ce.stroke
                     AND s2.pool_length = c.pool_length
                     AND (
                       s.id IS NULL OR
                       (s2.swim_date < s.swim_date OR
                       (s2.swim_date = s.swim_date AND s2.id < s.id))
                     )
               ) AS prior_pb
        FROM competition_events ce
        JOIN competitions c ON c.id=ce.competition_id
        LEFT JOIN swims s ON s.planned_event_id=ce.id
        WHERE ce.competition_id=?
        ORDER BY ce.id
    """, (competition_id,)).fetchall()

    enriched = []
    completed = 0
    new_pbs = 0
    first_marks = 0

    for e in events:
        item = dict(e)
        item["splits"] = []
        item["is_pb"] = False
        item["is_first"] = False
        item["improvement_cs"] = None

        if e["swim_id"]:
            completed += 1
            splits = conn.execute("""
                SELECT *
                FROM splits
                WHERE swim_id=?
                ORDER BY split_distance ASC, id ASC
            """, (e["swim_id"],)).fetchall()
            item["splits"] = [dict(x) for x in splits]

            if e["prior_pb"] is None:
                first_marks += 1
                item["is_first"] = True
            elif e["time_cs"] <= e["prior_pb"]:
                new_pbs += 1
                item["is_pb"] = True
                item["improvement_cs"] = e["prior_pb"] - e["time_cs"]

        enriched.append(item)

    conn.close()

    return render_template(
        "competition_detail.html",
        comp=comp,
        events=enriched,
        total_races=len(events),
        completed=completed,
        new_pbs=new_pbs,
        first_marks=first_marks
    )


@app.route("/competitions/<int:competition_id>/events/new", methods=["GET", "POST"])
def new_competition_event(competition_id):
    conn = get_db()
    comp = conn.execute("SELECT * FROM competitions WHERE id=?", (competition_id,)).fetchone()
    if not comp:
        conn.close()
        return "Competencia no encontrada", 404

    if request.method == "POST":
        stroke = request.form["stroke"]
        distance = int(request.form["distance"])
        target = request.form.get("target_time", "").strip()
        target_cs = parse_time(target) if target else None

        if not is_valid_event(stroke, comp["pool_length"], distance):
            conn.close()
            return render_template("competition_event_form.html", comp=comp, styles=STYLES,
                                   official_events=OFFICIAL_EVENTS, error="Prueba no válida.")

        conn.execute("""
            INSERT INTO competition_events(competition_id, distance, stroke, target_cs)
            VALUES (?, ?, ?, ?)
        """, (competition_id, distance, stroke, target_cs))
        conn.commit()
        conn.close()
        return redirect(url_for("competition_detail", competition_id=competition_id))

    conn.close()
    return render_template("competition_event_form.html", comp=comp, styles=STYLES, official_events=OFFICIAL_EVENTS)

@app.route("/swim/<int:swim_id>")
def swim_detail(swim_id):
    conn = get_db()
    swim = conn.execute("""
        SELECT s.*, c.name AS competition_name, c.location AS competition_location
        FROM swims s
        LEFT JOIN competitions c ON c.id=s.competition_id
        WHERE s.id=?
    """, (swim_id,)).fetchone()
    if not swim:
        conn.close()
        return "Registro no encontrado", 404

    splits = conn.execute("SELECT * FROM splits WHERE swim_id=? ORDER BY split_distance", (swim_id,)).fetchall()
    prior = conn.execute("""
        SELECT MIN(time_cs) AS prior_pb
        FROM swims
        WHERE distance=? AND stroke=? AND pool_length=?
          AND (swim_date < ? OR (swim_date=? AND id < ?))
    """, (swim["distance"], swim["stroke"], swim["pool_length"],
          swim["swim_date"], swim["swim_date"], swim["id"])).fetchone()

    conn.close()
    return render_template("swim_detail.html", swim=swim, splits=splits, prior=prior)



@app.route("/achievements")
def achievements():
    conn = get_db()

    swims = conn.execute("""
        SELECT *
        FROM swims
        ORDER BY swim_date ASC, id ASC
    """).fetchall()

    competitions_count = conn.execute("""
        SELECT COUNT(*) AS c
        FROM competitions
    """).fetchone()["c"]

    goals = conn.execute("""
        SELECT *
        FROM goals
    """).fetchall()

    unlocked = []
    locked = []

    def add_achievement(code, title, description, icon, unlocked_flag,
                        progress=None, progress_max=None, detail=None):
        item = {
            "code": code,
            "title": title,
            "description": description,
            "icon": icon,
            "progress": progress,
            "progress_max": progress_max,
            "detail": detail
        }
        (unlocked if unlocked_flag else locked).append(item)

    # 1. First mark
    add_achievement(
        "first_mark",
        "Primera marca",
        "Registraste tu primer tiempo en SwimPro.",
        "⭐",
        len(swims) >= 1,
        min(len(swims), 1),
        1,
        f"{len(swims)} tiempo(s) registrado(s)"
    )

    # 2. Number of registered swims
    for milestone in (10, 25, 50, 100):
        add_achievement(
            f"swims_{milestone}",
            f"{milestone} tiempos",
            f"Registraste {milestone} tiempos en tu historial.",
            "⏱️",
            len(swims) >= milestone,
            min(len(swims), milestone),
            milestone,
            f"{len(swims)}/{milestone}"
        )

    # 3. Competition milestones
    for milestone in (1, 5, 10, 25):
        title = "Primera competencia" if milestone == 1 else f"{milestone} competencias"
        add_achievement(
            f"competitions_{milestone}",
            title,
            f"Alcanzaste {milestone} competencia{'s' if milestone != 1 else ''} registrada{'s' if milestone != 1 else ''}.",
            "🏆",
            competitions_count >= milestone,
            min(competitions_count, milestone),
            milestone,
            f"{competitions_count}/{milestone}"
        )

    # 4. PB achievements from progression
    event_history = {}
    total_pbs = 0
    first_marks = 0

    for s in swims:
        key = (s["distance"], s["stroke"], s["pool_length"])
        prior = event_history.get(key)

        if prior is None:
            first_marks += 1
            event_history[key] = s["time_cs"]
        elif s["time_cs"] < prior:
            total_pbs += 1
            event_history[key] = s["time_cs"]

    add_achievement(
        "first_pb",
        "Nuevo PB",
        "Mejoraste una marca personal por primera vez.",
        "🥇",
        total_pbs >= 1,
        min(total_pbs, 1),
        1,
        f"{total_pbs} mejora(s) de PB"
    )

    for milestone in (5, 10, 25):
        add_achievement(
            f"pbs_{milestone}",
            f"{milestone} nuevos PB",
            f"Conseguiste {milestone} mejoras de marca personal.",
            "🔥",
            total_pbs >= milestone,
            min(total_pbs, milestone),
            milestone,
            f"{total_pbs}/{milestone}"
        )

    # 5. Distinct events
    distinct_events = len({
        (s["distance"], s["stroke"], s["pool_length"]) for s in swims
    })

    for milestone in (5, 10, 15):
        add_achievement(
            f"events_{milestone}",
            f"{milestone} pruebas distintas",
            f"Tienes marcas registradas en {milestone} pruebas diferentes.",
            "🌊",
            distinct_events >= milestone,
            min(distinct_events, milestone),
            milestone,
            f"{distinct_events}/{milestone}"
        )

    # 6. Goal achievements
    goals_achieved = 0
    for g in goals:
        pb_row = conn.execute("""
            SELECT MIN(time_cs) AS pb
            FROM swims
            WHERE distance=? AND stroke=? AND pool_length=?
        """, (g["distance"], g["stroke"], g["pool_length"])).fetchone()

        if pb_row and pb_row["pb"] is not None and pb_row["pb"] <= g["target_cs"]:
            goals_achieved += 1

    add_achievement(
        "first_goal",
        "Objetivo cumplido",
        "Alcanzaste uno de tus objetivos personales.",
        "🎯",
        goals_achieved >= 1,
        min(goals_achieved, 1),
        1,
        f"{goals_achieved} objetivo(s) logrado(s)"
    )

    for milestone in (3, 5):
        add_achievement(
            f"goals_{milestone}",
            f"{milestone} objetivos cumplidos",
            f"Alcanzaste {milestone} metas personales.",
            "🎯",
            goals_achieved >= milestone,
            min(goals_achieved, milestone),
            milestone,
            f"{goals_achieved}/{milestone}"
        )

    # 7. Time threshold achievements.
    # General-purpose notable thresholds for 100 m races.
    threshold_specs = [
        ("100_any_sub_90", "Sub 1:30", 100, 9000, "⚡"),
        ("100_any_sub_80", "Sub 1:20", 100, 8000, "⚡"),
        ("100_any_sub_75", "Sub 1:15", 100, 7500, "🚀"),
        ("100_any_sub_70", "Sub 1:10", 100, 7000, "🚀"),
        ("50_any_sub_30", "50 m Sub 30", 50, 3000, "💨"),
    ]

    for code, title, distance, threshold_cs, icon in threshold_specs:
        matching = [s for s in swims if s["distance"] == distance]
        best = min((s["time_cs"] for s in matching), default=None)
        unlocked_flag = best is not None and best < threshold_cs

        detail = None
        if best is not None:
            detail = f"Mejor: {fmt_time(best)}"

        add_achievement(
            code,
            title,
            f"Registra una marca por debajo de {fmt_time(threshold_cs)} en {distance} m.",
            icon,
            unlocked_flag,
            None,
            None,
            detail
        )

    # 8. Specialty thresholds based on known event categories
    butterfly_100 = [
        s for s in swims
        if s["distance"] == 100 and s["stroke"] == "Mariposa"
    ]
    butterfly_pb = min((s["time_cs"] for s in butterfly_100), default=None)

    for threshold_cs, label in [(7600, "Sub 1:16 Mariposa"), (7500, "Sub 1:15 Mariposa"), (7400, "Sub 1:14 Mariposa")]:
        add_achievement(
            f"fly100_{threshold_cs}",
            label,
            f"Completa 100 m Mariposa por debajo de {fmt_time(threshold_cs)}.",
            "🦋",
            butterfly_pb is not None and butterfly_pb < threshold_cs,
            None,
            None,
            f"PB actual: {fmt_time(butterfly_pb)}" if butterfly_pb is not None else None
        )

    # 9. Improvement streak: consecutive chronological PB improvements
    longest_streak = 0
    current_streak = 0
    best_by_event = {}

    for s in swims:
        key = (s["distance"], s["stroke"], s["pool_length"])
        old = best_by_event.get(key)

        if old is None:
            best_by_event[key] = s["time_cs"]
            current_streak = 0
        elif s["time_cs"] < old:
            best_by_event[key] = s["time_cs"]
            current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            current_streak = 0

    for milestone in (3, 5):
        add_achievement(
            f"streak_{milestone}",
            f"Racha de {milestone} mejoras",
            f"Consigue {milestone} nuevos PB consecutivos.",
            "🔥",
            longest_streak >= milestone,
            min(longest_streak, milestone),
            milestone,
            f"Mejor racha: {longest_streak}"
        )

    # Sort unlocked first by code grouping; locked by closeness to progress if available.
    unlocked.sort(key=lambda x: x["title"])
    locked.sort(
        key=lambda x: (
            -((x["progress"] / x["progress_max"]) if x["progress"] is not None and x["progress_max"] else 0),
            x["title"]
        )
    )

    conn.close()

    total = len(unlocked) + len(locked)
    completion = round((len(unlocked) / total) * 100) if total else 0

    return render_template(
        "achievements.html",
        unlocked=unlocked,
        locked=locked,
        total=total,
        completion=completion,
        competitions_count=competitions_count,
        total_swims=len(swims),
        total_pbs=total_pbs
    )


@app.route("/goals")
def goals_dashboard():
    conn = get_db()
    seasons = available_seasons(conn)
    selected_year = request.args.get("year", type=int)
    if selected_year and selected_year not in seasons:
        selected_year = None

    pb_rows = conn.execute("""
        SELECT distance, stroke, pool_length,
               MIN(time_cs) AS historical_pb,
               COUNT(*) AS historical_attempts
        FROM swims
        GROUP BY distance, stroke, pool_length
        ORDER BY stroke, pool_length, distance
    """).fetchall()

    goals = conn.execute("""
        SELECT distance, stroke, pool_length, target_cs
        FROM goals
    """).fetchall()

    goal_map = {
        (g["distance"], g["stroke"], g["pool_length"]): g["target_cs"]
        for g in goals
    }

    items = []

    for r in pb_rows:
        key = (r["distance"], r["stroke"], r["pool_length"])
        target = goal_map.get(key)

        season_pb = None
        season_attempts = 0

        if selected_year:
            sr = conn.execute("""
                SELECT MIN(time_cs) AS season_pb,
                       COUNT(*) AS season_attempts
                FROM swims
                WHERE distance=? AND stroke=? AND pool_length=?
                  AND substr(swim_date,1,4)=?
            """, (
                r["distance"], r["stroke"], r["pool_length"],
                str(selected_year)
            )).fetchone()
            if sr:
                season_pb = sr["season_pb"]
                season_attempts = sr["season_attempts"]

        display_pb = season_pb if selected_year and season_pb is not None else r["historical_pb"]

        first = conn.execute("""
            SELECT time_cs
            FROM swims
            WHERE distance=? AND stroke=? AND pool_length=?
            ORDER BY swim_date ASC, id ASC
            LIMIT 1
        """, key).fetchone()

        first_cs = first["time_cs"] if first else r["historical_pb"]

        gap_cs = None
        progress = None
        achieved = False

        if target is not None:
            gap_cs = r["historical_pb"] - target
            achieved = r["historical_pb"] <= target

            if achieved:
                progress = 100
            elif first_cs > target:
                total_needed = first_cs - target
                improvement = first_cs - r["historical_pb"]
                progress = max(0, min(99, round((improvement / total_needed) * 100)))
            else:
                progress = 0

        items.append({
            "distance": r["distance"],
            "stroke": r["stroke"],
            "pool_length": r["pool_length"],
            "historical_pb": r["historical_pb"],
            "historical_attempts": r["historical_attempts"],
            "season_pb": season_pb,
            "season_attempts": season_attempts,
            "display_pb": display_pb,
            "target_cs": target,
            "gap_cs": gap_cs,
            "progress": progress,
            "achieved": achieved
        })

    existing_keys = {
        (x["distance"], x["stroke"], x["pool_length"]) for x in items
    }

    for g in goals:
        key = (g["distance"], g["stroke"], g["pool_length"])
        if key not in existing_keys:
            items.append({
                "distance": g["distance"],
                "stroke": g["stroke"],
                "pool_length": g["pool_length"],
                "historical_pb": None,
                "historical_attempts": 0,
                "season_pb": None,
                "season_attempts": 0,
                "display_pb": None,
                "target_cs": g["target_cs"],
                "gap_cs": None,
                "progress": 0,
                "achieved": False
            })

    conn.close()

    stroke_order = {"Libre": 0, "Espalda": 1, "Pecho": 2, "Mariposa": 3, "Combinado": 4}
    items.sort(key=lambda x: (
        stroke_order.get(x["stroke"], 99),
        x["pool_length"],
        x["distance"]
    ))

    return render_template(
        "goals.html",
        items=items,
        seasons=seasons,
        selected_year=selected_year
    )


@app.route("/goals/new", methods=["GET", "POST"])
def new_goal():
    error = None

    if request.method == "POST":
        try:
            distance = int(request.form["distance"])
            stroke = request.form["stroke"]
            pool_length = int(request.form["pool_length"])
            target_cs = parse_time(request.form["target_time"])

            if not is_valid_event(stroke, pool_length, distance):
                raise ValueError("Prueba no válida para esa piscina.")

            conn = get_db()
            conn.execute("""
                INSERT INTO goals(distance, stroke, pool_length, target_cs)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(distance, stroke, pool_length)
                DO UPDATE SET target_cs=excluded.target_cs
            """, (distance, stroke, pool_length, target_cs))
            conn.commit()
            conn.close()

            return redirect(url_for("goals_dashboard"))

        except (ValueError, KeyError) as e:
            error = str(e)

    return render_template(
        "goal_form.html",
        styles=STYLES,
        official_events=OFFICIAL_EVENTS,
        error=error
    )


@app.route("/goals/<int:distance>/<stroke>/<int:pool_length>/edit", methods=["GET", "POST"])
def edit_goal(distance, stroke, pool_length):
    conn = get_db()
    goal = conn.execute("""
        SELECT *
        FROM goals
        WHERE distance=? AND stroke=? AND pool_length=?
    """, (distance, stroke, pool_length)).fetchone()

    if not goal:
        conn.close()
        return redirect(url_for("new_goal"))

    if request.method == "POST":
        try:
            target_cs = parse_time(request.form["target_time"])
        except ValueError:
            conn.close()
            return render_template(
                "goal_edit.html",
                goal=goal,
                error="Tiempo objetivo inválido."
            )

        conn.execute("""
            UPDATE goals
            SET target_cs=?
            WHERE distance=? AND stroke=? AND pool_length=?
        """, (target_cs, distance, stroke, pool_length))
        conn.commit()
        conn.close()
        return redirect(url_for("goals_dashboard"))

    conn.close()
    return render_template("goal_edit.html", goal=goal)


@app.route("/goals/<int:distance>/<stroke>/<int:pool_length>/delete", methods=["POST"])
def delete_goal(distance, stroke, pool_length):
    conn = get_db()
    conn.execute("""
        DELETE FROM goals
        WHERE distance=? AND stroke=? AND pool_length=?
    """, (distance, stroke, pool_length))
    conn.commit()
    conn.close()
    return redirect(url_for("goals_dashboard"))


@app.route("/stats")
def stats():
    conn = get_db()

    pool = request.args.get("pool", type=int)
    stroke = request.args.get("stroke", type=str)
    year = request.args.get("year", type=int)
    seasons = available_seasons(conn)

    if year and year not in seasons:
        year = None

    base_filters = []
    base_params = []

    if pool in (25, 50):
        base_filters.append("pool_length=?")
        base_params.append(pool)

    if stroke in STYLES:
        base_filters.append("stroke=?")
        base_params.append(stroke)

    historical_where = (" WHERE " + " AND ".join(base_filters)) if base_filters else ""

    season_filters = list(base_filters)
    season_params = list(base_params)

    if year:
        season_filters.append("substr(swim_date,1,4)=?")
        season_params.append(str(year))

    season_where = (" WHERE " + " AND ".join(season_filters)) if season_filters else ""

    total_swims = conn.execute(
        f"SELECT COUNT(*) AS c FROM swims{season_where}",
        season_params
    ).fetchone()["c"]

    total_competitions = conn.execute(
        "SELECT COUNT(*) AS c FROM competitions"
    ).fetchone()["c"]

    historical_rows = conn.execute(f"""
        SELECT distance, stroke, pool_length,
               MIN(time_cs) AS historical_pb,
               COUNT(*) AS historical_attempts
        FROM swims
        {historical_where}
        GROUP BY distance, stroke, pool_length
        ORDER BY stroke, distance, pool_length
    """, base_params).fetchall()

    progression = []
    for r in historical_rows:
        season_pb = None
        season_attempts = 0

        if year:
            sr = conn.execute("""
                SELECT MIN(time_cs) AS season_pb,
                       COUNT(*) AS season_attempts
                FROM swims
                WHERE distance=? AND stroke=? AND pool_length=?
                  AND substr(swim_date,1,4)=?
            """, (
                r["distance"], r["stroke"], r["pool_length"],
                str(year)
            )).fetchone()
            if sr:
                season_pb = sr["season_pb"]
                season_attempts = sr["season_attempts"]

        first = conn.execute("""
            SELECT time_cs
            FROM swims
            WHERE distance=? AND stroke=? AND pool_length=?
            ORDER BY swim_date ASC, id ASC
            LIMIT 1
        """, (r["distance"], r["stroke"], r["pool_length"])).fetchone()

        progression.append({
            "distance": r["distance"],
            "stroke": r["stroke"],
            "pool_length": r["pool_length"],
            "historical_pb": r["historical_pb"],
            "historical_attempts": r["historical_attempts"],
            "season_pb": season_pb,
            "season_attempts": season_attempts,
            "improvement": (first["time_cs"] - r["historical_pb"]) if first else 0
        })

    conn.close()

    return render_template(
        "stats.html",
        total_swims=total_swims,
        total_competitions=total_competitions,
        event_count=len(historical_rows),
        progression=progression,
        pool=pool,
        stroke=stroke,
        year=year,
        seasons=seasons,
        styles=STYLES
    )


@app.route("/swim/<int:swim_id>/edit", methods=["GET", "POST"])
def edit_swim(swim_id):
    conn = get_db()
    swim = conn.execute("SELECT * FROM swims WHERE id=?", (swim_id,)).fetchone()
    if not swim:
        conn.close()
        return "Registro no encontrado", 404
    competitions = conn.execute("SELECT * FROM competitions ORDER BY competition_date DESC,id DESC").fetchall()
    if request.method == "POST":
        try:
            distance = int(request.form["distance"])
            stroke = request.form["stroke"]
            pool_length = int(request.form["pool_length"])
            time_cs = parse_time(request.form["time"])
            swim_date = request.form["swim_date"]
            kind = request.form.get("kind", "Competencia")
            notes = request.form.get("notes", "").strip()
            competition_id = request.form.get("competition_id", type=int)
            if competition_id:
                comp = conn.execute("SELECT * FROM competitions WHERE id=?", (competition_id,)).fetchone()
                if comp:
                    pool_length = comp["pool_length"]
                    swim_date = comp["competition_date"]
            if not is_valid_event(stroke, pool_length, distance):
                raise ValueError("Prueba no válida para esa piscina.")
        except (ValueError, KeyError) as e:
            conn.close()
            return render_template("edit_swim.html", swim=swim, competitions=competitions, styles=STYLES, official_events=OFFICIAL_EVENTS, error=str(e))
        conn.execute("UPDATE swims SET distance=?,stroke=?,pool_length=?,time_cs=?,swim_date=?,kind=?,notes=?,competition_id=? WHERE id=?", (distance,stroke,pool_length,time_cs,swim_date,kind,notes,competition_id,swim_id))
        conn.commit()
        conn.close()
        return redirect(url_for("swim_detail", swim_id=swim_id))
    conn.close()
    return render_template("edit_swim.html", swim=swim, competitions=competitions, styles=STYLES, official_events=OFFICIAL_EVENTS)

@app.route("/swim/<int:swim_id>/delete", methods=["POST"])
def delete_swim(swim_id):
    conn = get_db()
    swim = conn.execute("SELECT planned_event_id,competition_id FROM swims WHERE id=?", (swim_id,)).fetchone()
    if swim:
        if swim["planned_event_id"]:
            conn.execute("UPDATE competition_events SET status='Pendiente' WHERE id=?", (swim["planned_event_id"],))
        conn.execute("DELETE FROM splits WHERE swim_id=?", (swim_id,))
        conn.execute("DELETE FROM swims WHERE id=?", (swim_id,))
        conn.commit()
    cid = swim["competition_id"] if swim else None
    conn.close()
    return redirect(url_for("competition_detail", competition_id=cid) if cid else url_for("index"))

@app.route("/competitions/<int:competition_id>/edit", methods=["GET", "POST"])
def edit_competition(competition_id):
    conn = get_db()
    comp = conn.execute("SELECT * FROM competitions WHERE id=?", (competition_id,)).fetchone()
    if not comp:
        conn.close()
        return "Competencia no encontrada", 404
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        competition_date = request.form.get("competition_date", "")
        location = request.form.get("location", "").strip()
        pool_length = request.form.get("pool_length", type=int)
        notes = request.form.get("notes", "").strip()
        if not name or not competition_date or pool_length not in (25, 50):
            conn.close()
            return render_template("edit_competition.html", comp=comp, error="Completa nombre, fecha y piscina.")
        conn.execute("UPDATE competitions SET name=?,competition_date=?,location=?,pool_length=?,notes=? WHERE id=?", (name,competition_date,location,pool_length,notes,competition_id))
        conn.execute("UPDATE swims SET swim_date=?,pool_length=?,event_name=? WHERE competition_id=?", (competition_date,pool_length,name,competition_id))
        conn.commit()
        conn.close()
        return redirect(url_for("competition_detail", competition_id=competition_id))
    conn.close()
    return render_template("edit_competition.html", comp=comp)

@app.route("/competitions/events/<int:event_id>/edit", methods=["GET", "POST"])
def edit_competition_event(event_id):
    conn = get_db()
    event = conn.execute("""
        SELECT ce.*, c.name AS competition_name, c.pool_length
        FROM competition_events ce
        JOIN competitions c ON c.id=ce.competition_id
        WHERE ce.id=?
    """, (event_id,)).fetchone()

    if not event:
        conn.close()
        return "Prueba no encontrada", 404

    if request.method == "POST":
        target_raw = request.form.get("target_time", "").strip()
        try:
            target_cs = parse_time(target_raw) if target_raw else None
        except ValueError:
            conn.close()
            return render_template(
                "competition_event_edit.html",
                event=event,
                error="Tiempo objetivo inválido."
            )

        position = request.form.get("position", "").strip() or None
        notes = request.form.get("notes", "").strip() or None

        conn.execute("""
            UPDATE competition_events
            SET target_cs=?, position=?, notes=?
            WHERE id=?
        """, (target_cs, position, notes, event_id))
        conn.commit()

        competition_id = event["competition_id"]
        conn.close()
        return redirect(url_for("competition_detail", competition_id=competition_id))

    conn.close()
    return render_template("competition_event_edit.html", event=event)


@app.route("/competitions/events/<int:event_id>/delete", methods=["POST"])
def delete_competition_event(event_id):
    conn = get_db()
    event = conn.execute("SELECT competition_id FROM competition_events WHERE id=?", (event_id,)).fetchone()
    if event:
        linked = conn.execute("SELECT id FROM swims WHERE planned_event_id=?", (event_id,)).fetchone()
        if not linked:
            conn.execute("DELETE FROM competition_events WHERE id=?", (event_id,))
            conn.commit()
    cid = event["competition_id"] if event else None
    conn.close()
    return redirect(url_for("competition_detail", competition_id=cid) if cid else url_for("competitions"))

@app.route("/export/json")
def export_json():
    conn = get_db()
    payload = {
        "profile": dict(conn.execute("SELECT * FROM profile WHERE id=1").fetchone() or {}),
        "competitions": [dict(r) for r in conn.execute("SELECT * FROM competitions ORDER BY id").fetchall()],
        "competition_events": [dict(r) for r in conn.execute("SELECT * FROM competition_events ORDER BY id").fetchall()],
        "swims": [dict(r) for r in conn.execute("SELECT * FROM swims ORDER BY id").fetchall()],
        "splits": [dict(r) for r in conn.execute("SELECT * FROM splits ORDER BY id").fetchall()],
        "goals": [dict(r) for r in conn.execute("SELECT * FROM goals ORDER BY id").fetchall()],
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "app": "SwimPro V10"
    }
    conn.close()
    return Response(json.dumps(payload, ensure_ascii=False, indent=2), mimetype="application/json", headers={"Content-Disposition": "attachment; filename=swimpro_backup.json"})

@app.route("/export/csv")
def export_csv():
    conn = get_db()
    rows = conn.execute("SELECT s.swim_date,s.distance,s.stroke,s.pool_length,s.time_cs,s.kind,c.name AS competition,c.location,s.notes FROM swims s LEFT JOIN competitions c ON c.id=s.competition_id ORDER BY s.swim_date,s.id").fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["fecha","distancia_m","estilo","piscina_m","tiempo","tipo","competencia","lugar","notas"])
    for r in rows:
        writer.writerow([r["swim_date"],r["distance"],r["stroke"],r["pool_length"],fmt_time(r["time_cs"]),r["kind"],r["competition"] or "",r["location"] or "",r["notes"] or ""])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=swimpro_times.csv"})



@app.route("/goals", methods=["POST"])
def set_goal():
    try:
        distance = int(request.form["distance"])
        stroke = request.form["stroke"]
        pool_length = int(request.form["pool_length"])
        target_cs = parse_time(request.form["target_time"])
    except (ValueError, KeyError):
        return "Datos de objetivo inválidos", 400

    if not is_valid_event(stroke, pool_length, distance):
        return "Prueba no válida", 400

    conn = get_db()
    conn.execute("""
        INSERT INTO goals(distance, stroke, pool_length, target_cs)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(distance, stroke, pool_length)
        DO UPDATE SET target_cs=excluded.target_cs
    """, (distance, stroke, pool_length, target_cs))
    conn.commit()
    conn.close()

    return redirect(url_for(
        "history",
        distance=distance,
        stroke=stroke,
        pool_length=pool_length
    ))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    conn = get_db()
    if request.method == "POST":
        conn.execute("UPDATE profile SET name=?, birth_date=?, sex=? WHERE id=1", (
            request.form.get("name", "").strip() or "Mi perfil",
            request.form.get("birth_date", ""),
            request.form.get("sex", "")
        ))
        conn.commit()
        conn.close()
        return redirect(url_for("profile"))

    data = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
    age = age_on_date(data["birth_date"]) if data else None
    category = masters_category(age)
    conn.close()
    return render_template("profile.html", profile=data, age=age, category=category)

@app.route("/history/<int:distance>/<stroke>/<int:pool_length>")
def history(distance, stroke, pool_length):
    conn = get_db()
    seasons = available_seasons(conn)
    selected_year = request.args.get("year", type=int)
    if selected_year and selected_year not in seasons:
        selected_year = None

    all_rows = conn.execute("""
        SELECT s.*, c.name AS competition_name
        FROM swims s
        LEFT JOIN competitions c ON c.id=s.competition_id
        WHERE s.distance=? AND s.stroke=? AND s.pool_length=?
        ORDER BY s.swim_date ASC, s.id ASC
    """, (distance, stroke, pool_length)).fetchall()

    if selected_year:
        rows = [
            r for r in all_rows
            if r["swim_date"].startswith(str(selected_year))
        ]
    else:
        rows = all_rows

    goal = conn.execute("""
        SELECT *
        FROM goals
        WHERE distance=? AND stroke=? AND pool_length=?
    """, (distance, stroke, pool_length)).fetchone()

    historical_pb = min((r["time_cs"] for r in all_rows), default=None)
    season_pb = min((r["time_cs"] for r in rows), default=None)

    pb = season_pb if selected_year else historical_pb

    pb_row = next((r for r in rows if pb is not None and r["time_cs"] == pb), None)

    previous_pb = None
    if pb_row is not None:
        candidates = [
            r["time_cs"] for r in rows
            if (r["swim_date"], r["id"]) < (pb_row["swim_date"], pb_row["id"])
        ]
        if candidates:
            previous_pb = min(candidates)

    first_time = rows[0]["time_cs"] if rows else None
    total_improvement = (first_time - pb) if first_time is not None and pb is not None else None
    previous_pb_improvement = (previous_pb - pb) if previous_pb is not None and pb is not None else None

    last3 = [r["time_cs"] for r in rows[-3:]]
    avg3 = round(sum(last3) / len(last3)) if last3 else None
    latest = rows[-1]["time_cs"] if rows else None

    goal_gap = None
    goal_progress = None
    if goal and historical_pb is not None:
        goal_gap = historical_pb - goal["target_cs"]

        first_all = all_rows[0]["time_cs"] if all_rows else historical_pb
        if historical_pb <= goal["target_cs"]:
            goal_progress = 100
        elif first_all > goal["target_cs"]:
            total_needed = first_all - goal["target_cs"]
            improvement = first_all - historical_pb
            goal_progress = max(0, min(99, round((improvement / total_needed) * 100)))
        else:
            goal_progress = 0

    conn.close()

    return render_template(
        "history.html",
        rows=rows,
        all_rows=all_rows,
        distance=distance,
        stroke=stroke,
        pool_length=pool_length,
        goal=goal,
        pb=pb,
        historical_pb=historical_pb,
        season_pb=season_pb,
        previous_pb=previous_pb,
        first_time=first_time,
        total_improvement=total_improvement,
        previous_pb_improvement=previous_pb_improvement,
        avg3=avg3,
        latest=latest,
        goal_gap=goal_gap,
        goal_progress=goal_progress,
        seasons=seasons,
        selected_year=selected_year
    )


@app.route("/api/history/<int:distance>/<stroke>/<int:pool_length>")
def api_history(distance, stroke, pool_length):
    conn = get_db()
    rows = conn.execute("""
        SELECT swim_date, time_cs FROM swims
        WHERE distance=? AND stroke=? AND pool_length=?
        ORDER BY swim_date ASC, id ASC
    """, (distance, stroke, pool_length)).fetchall()
    conn.close()
    return jsonify([{"date": r["swim_date"], "seconds": r["time_cs"]/100} for r in rows])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
