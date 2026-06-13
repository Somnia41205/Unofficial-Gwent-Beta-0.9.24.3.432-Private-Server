"""
SQLite storage backend for Gwent Beta Private Server.

Drop-in replacement for the per-user JSON file storage. Each user's game data
is stored as a JSON blob in SQLite, providing atomic writes, no file-lock
issues, and WAL-mode concurrency for ~50 simultaneous users.

Usage in server.py:
    from db import load_data, save_data, load_users, save_user, register_user, get_user_by_id
"""

import json
import os
import sqlite3
import threading

# ---------------------------------------------------------------------------
# Database path — same directory as the data files
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("GWENT_DATA_DIR",
                          os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DATA_DIR, "gwent.db")

# Default data for new users (same as the old load_data default)
DEFAULT_USER_DATA = {
    "cards": [],
    "decks": [],
    "next_card_id": 100001,
    "currencies": [
        {"currency": {"id": 1, "name": "Gold"}, "amount": 1000, "expiration": []},
        {"currency": {"id": 2, "name": "Scraps"}, "amount": 10000, "expiration": []},
        {"currency": {"id": 3, "name": "Powder"}, "amount": 2000, "expiration": []}
    ],
    "inventory": {},
    "card_packs": {},
    "favourite_card_id": 11210101,
    "favourite_faction": "",
    "accomplishments": []
}

# ---------------------------------------------------------------------------
# Connection pool — one connection per thread (SQLite requirement)
# ---------------------------------------------------------------------------
_local = threading.local()


def _get_conn():
    """Get or create a per-thread SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def init_db():
    """Create tables if they don't exist. Call once at server startup."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY,
            username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
            avatar_image_id TEXT NOT NULL DEFAULT '',
            created_date    TEXT NOT NULL,
            data        TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS friends (
            user_id     INTEGER NOT NULL,
            friend_id   INTEGER NOT NULL,
            status      INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, friend_id)
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# User registry operations (replaces users.json)
# ---------------------------------------------------------------------------

def load_users():
    """Return list of all users (same shape as old users.json)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, username, avatar_image_id, created_date FROM users"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "username": r["username"],
            "avatar_image_id": r["avatar_image_id"],
            "created_date": r["created_date"],
        }
        for r in rows
    ]


def get_user_by_id(user_id):
    """Look up a single user by ID. Returns dict or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, username, avatar_image_id, created_date FROM users WHERE id=?",
        (int(user_id),)
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "avatar_image_id": row["avatar_image_id"],
        "created_date": row["created_date"],
    }


def get_user_by_username(username):
    """Look up a user by username (case-insensitive). Returns dict or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, username, avatar_image_id, created_date FROM users WHERE username=? COLLATE NOCASE",
        (username,)
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "avatar_image_id": row["avatar_image_id"],
        "created_date": row["created_date"],
    }


def save_user(user_dict):
    """Insert or update a user in the registry."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO users (id, username, avatar_image_id, created_date, data)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               username=excluded.username,
               avatar_image_id=excluded.avatar_image_id,
               created_date=excluded.created_date""",
        (
            int(user_dict["id"]),
            user_dict["username"],
            user_dict.get("avatar_image_id", ""),
            user_dict.get("created_date", ""),
            json.dumps(DEFAULT_USER_DATA),
        )
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Per-user game data (replaces data_{user_id}.json)
# ---------------------------------------------------------------------------

def load_data(user_id=None):
    """Load a user's game data. Creates default data if user has none.

    Returns a mutable dict — caller mutates it, then calls save_data().
    Same interface as the old JSON-file load_data().
    """
    import copy
    if not user_id:
        # Legacy fallback — shouldn't happen in multi-user mode
        return copy.deepcopy(DEFAULT_USER_DATA)

    conn = _get_conn()
    row = conn.execute(
        "SELECT data FROM users WHERE id=?", (int(user_id),)
    ).fetchone()

    if row is None:
        # User doesn't exist yet — create with defaults
        return copy.deepcopy(DEFAULT_USER_DATA)

    try:
        saved = json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        saved = copy.deepcopy(DEFAULT_USER_DATA)

    # Merge any missing default keys (forward compat)
    for key, val in DEFAULT_USER_DATA.items():
        if key not in saved:
            saved[key] = copy.deepcopy(val)

    return saved


def save_data(data, user_id=None):
    """Persist a user's game data dict to SQLite.

    Same interface as the old JSON-file save_data().
    """
    if not user_id:
        return
    conn = _get_conn()
    blob = json.dumps(data)
    conn.execute("UPDATE users SET data=? WHERE id=?", (blob, int(user_id)))
    conn.commit()


# ---------------------------------------------------------------------------
# Friends operations
# ---------------------------------------------------------------------------

def get_friends(user_id):
    """Return dict {friend_id: status} for a user."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT friend_id, status FROM friends WHERE user_id=?",
        (int(user_id),)
    ).fetchall()
    return {r["friend_id"]: r["status"] for r in rows}


def set_friend(user_id, friend_id, status):
    """Set or update a friendship status."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO friends (user_id, friend_id, status)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id, friend_id) DO UPDATE SET status=excluded.status""",
        (int(user_id), int(friend_id), int(status))
    )
    conn.commit()


def delete_friend(user_id, friend_id):
    """Remove a friendship (both directions)."""
    conn = _get_conn()
    conn.execute(
        "DELETE FROM friends WHERE user_id=? AND friend_id=?",
        (int(user_id), int(friend_id))
    )
    conn.execute(
        "DELETE FROM friends WHERE user_id=? AND friend_id=?",
        (int(friend_id), int(user_id))
    )
    conn.commit()


def accept_friend(user_id, friend_id):
    """Accept a friend request — set both directions to status=2."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO friends (user_id, friend_id, status)
           VALUES (?, ?, 2)
           ON CONFLICT(user_id, friend_id) DO UPDATE SET status=2""",
        (int(user_id), int(friend_id))
    )
    conn.execute(
        """INSERT INTO friends (user_id, friend_id, status)
           VALUES (?, ?, 2)
           ON CONFLICT(user_id, friend_id) DO UPDATE SET status=2""",
        (int(friend_id), int(user_id))
    )
    conn.commit()


def load_all_friends():
    """Load all friendships into a dict for server startup.
    Returns {user_id: {friend_id: status}}."""
    conn = _get_conn()
    rows = conn.execute("SELECT user_id, friend_id, status FROM friends").fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["user_id"], {})[r["friend_id"]] = r["status"]
    return result


# ---------------------------------------------------------------------------
# Migration helper — import existing JSON files into SQLite
# ---------------------------------------------------------------------------

def migrate_from_json(data_dir, users_json_path=None):
    """Import users.json + data_*.json files into SQLite.

    Safe to run multiple times — uses INSERT OR REPLACE.
    """
    import glob
    import copy

    init_db()
    conn = _get_conn()

    # 1. Import users.json
    users = []
    ujp = users_json_path or os.path.join(data_dir, "users.json")
    if os.path.exists(ujp):
        with open(ujp, "r") as f:
            users = json.load(f)
        print(f"[MIGRATE] Loaded {len(users)} users from {ujp}")
    else:
        print(f"[MIGRATE] No users.json found at {ujp}")

    # 2. For each user, load their data file and insert
    for u in users:
        uid = u["id"]
        data_file = os.path.join(data_dir, f"data_{uid}.json")
        if os.path.exists(data_file):
            with open(data_file, "r") as f:
                user_data = json.load(f)
        else:
            user_data = copy.deepcopy(DEFAULT_USER_DATA)
            print(f"[MIGRATE] No data file for user {uid}, using defaults")

        conn.execute(
            """INSERT OR REPLACE INTO users (id, username, avatar_image_id, created_date, data)
               VALUES (?, ?, ?, ?, ?)""",
            (
                int(uid),
                u.get("username", f"User_{uid}"),
                u.get("avatar_image_id", ""),
                u.get("created_date", ""),
                json.dumps(user_data),
            )
        )
        print(f"[MIGRATE] Imported user {uid} ({u.get('username', '?')})")

    # 3. Check for orphaned data files (no users.json entry)
    for data_file in glob.glob(os.path.join(data_dir, "data_*.json")):
        fname = os.path.basename(data_file)
        uid_str = fname.replace("data_", "").replace(".json", "")
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        if not any(u["id"] == uid for u in users):
            with open(data_file, "r") as f:
                user_data = json.load(f)
            conn.execute(
                """INSERT OR IGNORE INTO users (id, username, avatar_image_id, created_date, data)
                   VALUES (?, ?, ?, ?, ?)""",
                (uid, f"User_{uid}", "", "", json.dumps(user_data))
            )
            print(f"[MIGRATE] Imported orphaned data file for user {uid}")

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"[MIGRATE] Done. {total} users in database.")


# ---------------------------------------------------------------------------
# CLI: run as script to migrate
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    data_dir = sys.argv[1] if len(sys.argv) > 1 else DATA_DIR
    print(f"[MIGRATE] Migrating from {data_dir} into {DB_PATH}")
    migrate_from_json(data_dir)
