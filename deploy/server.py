import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver
import ssl
import re
import time
import random
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

# Server data dir (gwent.db / users.json / per-user data / Templates.xml).
# Override with GWENT_DATA_DIR; defaults to a "data" folder next to this script.
DATA_DIR = os.environ.get(
    "GWENT_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
DATA_FILE = os.path.join(DATA_DIR, "data.json")  # Legacy single-user fallback
USERS_FILE = os.path.join(DATA_DIR, "users.json")

# ── SQLite backend ─────────────────────────────────────────────────────────
# Set GWENT_USE_SQLITE=1 to use SQLite instead of JSON files.
# The db module is a drop-in replacement for load_data/save_data/_load_users_json.
USE_SQLITE = os.environ.get("GWENT_USE_SQLITE", "0").strip() == "1"
if USE_SQLITE:
    import db as _db
    _db.DATA_DIR = DATA_DIR
    _db.DB_PATH = os.path.join(DATA_DIR, "gwent.db")
    _db.init_db()
    print(f"[SERVER] Using SQLite backend: {_db.DB_PATH}")

# ── Online/remote play configuration ────────────────────────────────────────
# SERVER_IP: the public IP or hostname remote clients will connect to.
# Set via environment variable, or defaults to 127.0.0.1 for localhost play.
SERVER_IP = os.environ.get("GWENT_SERVER_IP", "127.0.0.1").strip()
# Path to the game's card templates XML — update this to your data_definitions file path
CARD_TEMPLATES_FILE = os.path.join(DATA_DIR, "Data_definitions/Templates.xml")

# In-memory session deltas, now per-user: { user_id_str: {1: 0, 2: 0, 3: 0} }
session_currency_deltas = {}
_user_data_locks = {}  # Per-user file locks: { user_id_str: threading.Lock() }
_locks_lock = __import__("threading").Lock()

def _get_user_lock(user_id):
    """Get or create a threading lock for a specific user's data file."""
    uid = str(user_id)
    if uid not in _user_data_locks:
        with _locks_lock:
            if uid not in _user_data_locks:
                _user_data_locks[uid] = __import__("threading").Lock()
    return _user_data_locks[uid]

def _load_users_json():
    """Load the user registry from users.json (or SQLite if enabled)."""
    if USE_SQLITE:
        return _db.load_users()
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def _user_data_path(user_id):
    """Return the path to a user's data file."""
    return os.path.join(DATA_DIR, f"data_{user_id}.json")

# ---------------------------------------------------------------------------
# Matchmaking state (in-memory)
# ---------------------------------------------------------------------------
_mm_tickets = {}   # { ticket_id: {id, game_version, type, platform, phase, lobby_id, user_id} }
_mm_lobbies = {}   # { lobby_id: {id, endpoint, state, users:[{id,access_key,ip},...]} }
_mm_next_id = 1
_mm_lock = __import__("threading").Lock()

_friends = {}  # { user_id: { friend_id: status } }  status: 1=pending_sent, 2=friends, 3=pending_received
if USE_SQLITE:
    # Load persisted friendships from SQLite. This MUST happen after the
    # declaration above, or the empty-dict assignment wipes the loaded data.
    _friends = _db.load_all_friends()
    print(f"[SERVER] Loaded {sum(len(v) for v in _friends.values())} friendship records from DB")
    # Heal asymmetric rows left by older code that persisted only one
    # direction of an accepted friendship (symptom: one side sees the friend,
    # the other side's list is empty and re-inviting is blocked).
    _healed = 0
    for _a, _fmap in list(_friends.items()):
        for _b, _st in list(_fmap.items()):
            if _st == 2 and _friends.get(_b, {}).get(_a) != 2:
                _friends.setdefault(_b, {})[_a] = 2
                _db.set_friend(_b, _a, 2)
                _healed += 1
    if _healed:
        print(f"[SERVER] Healed {_healed} asymmetric friendship rows (set reverse direction to friends)")
_friends_rev = {}  # { user_id: int } — bumped whenever a friends row for that user changes;
                   # the mod polls /internal/friends_rev/{uid} (port 8447) and refreshes
                   # the in-game contact list on change (no restart needed).

def _bump_friends_rev(*user_ids):
    for u in user_ids:
        try:
            u = int(u)
        except (TypeError, ValueError):
            continue
        _friends_rev[u] = _friends_rev.get(u, 0) + 1

_presence = {}  # { user_id: {"data": {"metadata": "..."}} }  — last presence POST per user
_last_seen = {}  # { user_id: unix_ts } — updated by the mod's 2s invite poll; a user
                 # is reported online iff seen within PRESENCE_ONLINE_WINDOW seconds.
PRESENCE_ONLINE_WINDOW = 10
_recent_opponents = {}  # { user_id: [ {user_id, username, platform, player_result}, ... ] } (most recent first, max 20)
_game_finish_pairs = {}  # { game_id: [{user_id, won}, ...] } — correlate opponents
_mm_invitations = {}  # { inv_id: {id, game_version, user_id, invited_user_id, lobby_id, access_key, confirmation_owner, confirmation_invited, declined_by, endpoint, date_created} }
_game_invitations_pending = {}  # { target_user_id: [{"inv_id": str, "sender_id": str, "sender_name": str}] }
# Port the HOST client listens on for the direct socket game connection
MM_GAME_PORT = 7777
MM_HOST_IP = SERVER_IP

# ── XP / Level constants ─────────────────────────────────────────────────────
# The client's real level curve (decompiled from
# GwentUnity.Progression.HardcodedTable.ExperienceForLevel + ExperienceHelper).
#
# CRITICAL: ExperienceForLevel[L] is the XP REQUIRED TO COMPLETE level L (the
# WIDTH of that level's bar), NOT a cumulative running total. GetLevelInfo(L,
# experience) throws if `experience > ExperienceForLevel[L]`, which only makes
# sense if the client tracks XP *within the current level* (0..band width) and
# resets it to 0 on each level-up. For a level-L player the outcome bar's max is
# GetLevelInfo(L+1).NecessaryExperience == ExperienceForLevel[L+1].
#
# The band widths are therefore: L1=400, L2=500, L3=600, ... (each +100).
#   band(L) = 400 + 100*(L-1)
# So the bar a player fills grows every level; XP gained in a match is shown as a
# WITHIN-LEVEL delta. We MUST send experience_change as within-level values, or
# the client's ExperienceToDistribute() (= (next-from)+to on level-up) massively
# overshoots and the player appears to gain ~a full level every match.
CELLS_PER_LEVEL = 5
MAX_LEVEL       = 100

# Per-match XP rewards (base + performance):
#   base: a win is worth more than a loss
#   performance bonus: extra XP for each round the player actually won
XP_BASE_WIN       = 100   # awarded when the player wins the match
XP_BASE_LOSS      = 25    # awarded when the player loses the match
XP_PER_ROUND_WON  = 25    # performance bonus per round won (0..2)

CROWN_TIERS = [6, 18, 42, 66]

def xp_band_width(level):
    """XP required to COMPLETE `level` (the width of that level's bar).
    L1=400, L2=500, L3=600, ... (+100 each). Matches the client's
    ExperienceForLevel[level+1] used as the level-`level` bar maximum."""
    if level < 1:
        level = 1
    if level >= MAX_LEVEL:
        return 0
    return 400 + 100 * (level - 1)

# Cumulative lifetime XP required to *reach* the start of each level.
# reach[1] = 0, reach[2] = 400, reach[3] = 900, reach[4] = 1500, ...
def _build_reach_table():
    table = {1: 0}
    total = 0
    for lvl in range(1, MAX_LEVEL):
        total += xp_band_width(lvl)
        table[lvl + 1] = total
    return table

XP_TO_REACH_LEVEL = _build_reach_table()
XP_MAX_TOTAL      = XP_TO_REACH_LEVEL[MAX_LEVEL]  # lifetime XP at max level

def get_crown_cap(crowns_today):
    """Return the current daily crown cap based on crowns already earned."""
    for tier in CROWN_TIERS:
        if crowns_today < tier:
            return tier
    return CROWN_TIERS[-1]  # absolute max

def compute_level_and_cells(experience):
    """Map a LIFETIME-TOTAL XP value to (level, filled_cells) using the real
    band-width curve. `experience` is the running lifetime total stored in
    data['experience']."""
    experience = max(0, int(experience))
    if experience >= XP_MAX_TOTAL:
        return MAX_LEVEL, CELLS_PER_LEVEL
    level = 1
    for lvl in range(1, MAX_LEVEL):
        if experience >= XP_TO_REACH_LEVEL[lvl + 1]:
            level = lvl + 1
        else:
            level = lvl
            break
    band = xp_band_width(level)
    into = experience - XP_TO_REACH_LEVEL[level]
    if band <= 0:
        return level, CELLS_PER_LEVEL
    filled_cells = (into * CELLS_PER_LEVEL) // band
    return level, filled_cells

def xp_within_level(experience):
    """Return XP earned *within* the current level (0 .. band width) from a
    LIFETIME-TOTAL XP value. This is what the client's experience_change
    from/to fields expect."""
    experience = max(0, int(experience))
    if experience >= XP_MAX_TOTAL:
        return 0
    level, _ = compute_level_and_cells(experience)
    return experience - XP_TO_REACH_LEVEL[level]

def compute_match_xp(won, rounds_won):
    """Base XP (win/loss) plus a per-round-won performance bonus."""
    base = XP_BASE_WIN if won else XP_BASE_LOSS
    return base + XP_PER_ROUND_WON * max(0, int(rounds_won))

def maybe_reset_daily(data):
    """Reset daily counters if the calendar day has rolled over (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("crown_pieces_date", "") != today:
        data["crown_pieces_today"] = 0
        data["wins_today"]         = 0
        data["crown_pieces_date"]  = today

_gg_sent_games = set()

# Card pool cache: {rarity: [template_id, ...]}
_card_pool = None

# Faction lookup: {template_id: faction_int}
_faction_map = {}

# Maps FactionId integer values (from XML) to GOG wire strings used in API responses.
# These are NOT the C# enum names — Monsters → "Monster", NorthernRealms → "NorthernKingdom"
FACTION_INT_TO_GOG = {
    1:  "Neutral",
    2:  "Monster",
    4:  "Nilfgaard",
    8:  "NorthernKingdom",
    16: "Scoiatael",
    32: "Skellige",
}

# Auto-generated by scrape_vanity.py — paste into server.py

VANITY_ITEMS = [
    # Avatars
    {"id": 40000, "category": "Avatar"},
    {"id": 40001, "category": "Avatar"},
    {"id": 40002, "category": "Avatar"},
    {"id": 40003, "category": "Avatar"},
    {"id": 40004, "category": "Avatar"},
    {"id": 40005, "category": "Avatar"},
    {"id": 40006, "category": "Avatar"},
    {"id": 40007, "category": "Avatar"},
    {"id": 40008, "category": "Avatar"},  # default
    {"id": 40110, "category": "Avatar"},
    {"id": 40111, "category": "Avatar"},
    {"id": 40112, "category": "Avatar"},
    {"id": 40120, "category": "Avatar"},
    {"id": 40121, "category": "Avatar"},
    {"id": 40122, "category": "Avatar"},
    {"id": 40130, "category": "Avatar"},
    {"id": 40131, "category": "Avatar"},
    {"id": 40132, "category": "Avatar"},
    {"id": 40140, "category": "Avatar"},
    {"id": 40141, "category": "Avatar"},
    {"id": 40142, "category": "Avatar"},
    {"id": 40150, "category": "Avatar"},
    {"id": 40151, "category": "Avatar"},
    {"id": 40152, "category": "Avatar"},
    {"id": 40160, "category": "Avatar"},
    {"id": 40161, "category": "Avatar"},
    {"id": 40162, "category": "Avatar"},
    {"id": 40170, "category": "Avatar"},
    {"id": 45000, "category": "Avatar"},
    {"id": 45001, "category": "Avatar"},
    {"id": 45002, "category": "Avatar"},
    {"id": 45003, "category": "Avatar"},
    {"id": 45004, "category": "Avatar"},
    {"id": 45005, "category": "Avatar"},
    {"id": 45010, "category": "Avatar"},
    {"id": 45011, "category": "Avatar"},
    {"id": 45012, "category": "Avatar"},
    {"id": 45013, "category": "Avatar"},
    {"id": 45014, "category": "Avatar"},
    {"id": 45015, "category": "Avatar"},
    {"id": 45020, "category": "Avatar"},
    {"id": 45021, "category": "Avatar"},
    {"id": 45022, "category": "Avatar"},
    {"id": 46000, "category": "Avatar"},
    {"id": 46001, "category": "Avatar"},
    {"id": 46002, "category": "Avatar"},
    {"id": 46003, "category": "Avatar"},
    {"id": 46004, "category": "Avatar"},
    {"id": 48000, "category": "Avatar"},
    {"id": 49000, "category": "Avatar"},
    {"id": 49001, "category": "Avatar"},
    {"id": 49002, "category": "Avatar"},
    {"id": 49003, "category": "Avatar"},
    {"id": 49005, "category": "Avatar"},
    {"id": 49006, "category": "Avatar"},
    {"id": 49007, "category": "Avatar"},
    {"id": 49008, "category": "Avatar"},
    {"id": 49009, "category": "Avatar"},
    {"id": 49010, "category": "Avatar"},
    {"id": 49011, "category": "Avatar"},
    {"id": 49012, "category": "Avatar"},
    {"id": 49013, "category": "Avatar"},
    {"id": 49014, "category": "Avatar"},
    {"id": 49015, "category": "Avatar"},
    {"id": 49016, "category": "Avatar"},
    {"id": 49017, "category": "Avatar"},
    {"id": 49018, "category": "Avatar"},
    # Borders
    {"id": 30000, "category": "Border"},
    {"id": 30001, "category": "Border"},
    {"id": 30002, "category": "Border"},
    {"id": 30003, "category": "Border"},
    {"id": 30004, "category": "Border"},
    {"id": 30005, "category": "Border"},
    {"id": 30006, "category": "Border"},
    {"id": 30100, "category": "Border"},
    {"id": 30101, "category": "Border"},
    {"id": 30102, "category": "Border"},
    {"id": 30103, "category": "Border"},
    {"id": 30104, "category": "Border"},
    {"id": 30105, "category": "Border"},
    {"id": 30110, "category": "Border"},
    {"id": 30111, "category": "Border"},
    {"id": 30112, "category": "Border"},
    {"id": 30113, "category": "Border"},
    {"id": 30114, "category": "Border"},
    {"id": 30115, "category": "Border"},
    {"id": 30120, "category": "Border"},
    {"id": 30121, "category": "Border"},
    {"id": 30122, "category": "Border"},
    {"id": 30123, "category": "Border"},
    {"id": 30124, "category": "Border"},
    {"id": 30125, "category": "Border"},
    {"id": 30130, "category": "Border"},
    {"id": 30131, "category": "Border"},
    {"id": 30132, "category": "Border"},
    {"id": 30133, "category": "Border"},
    {"id": 30134, "category": "Border"},
    {"id": 30135, "category": "Border"},
    {"id": 30140, "category": "Border"},
    {"id": 30141, "category": "Border"},
    {"id": 30142, "category": "Border"},
    {"id": 30143, "category": "Border"},
    {"id": 30144, "category": "Border"},
    {"id": 30145, "category": "Border"},
    {"id": 30150, "category": "Border"},
    {"id": 30151, "category": "Border"},
    {"id": 30152, "category": "Border"},
    {"id": 30153, "category": "Border"},
    {"id": 30154, "category": "Border"},
    {"id": 30155, "category": "Border"},
    {"id": 30160, "category": "Border"},
    {"id": 30161, "category": "Border"},
    {"id": 30162, "category": "Border"},
    {"id": 30163, "category": "Border"},
    {"id": 30164, "category": "Border"},
    {"id": 30165, "category": "Border"},
    {"id": 30170, "category": "Border"},
    {"id": 30171, "category": "Border"},
    {"id": 30172, "category": "Border"},
    {"id": 30173, "category": "Border"},
    {"id": 30174, "category": "Border"},
    {"id": 30175, "category": "Border"},
    {"id": 30180, "category": "Border"},
    {"id": 30181, "category": "Border"},
    {"id": 30182, "category": "Border"},
    {"id": 30183, "category": "Border"},
    {"id": 30184, "category": "Border"},
    {"id": 30185, "category": "Border"},
    {"id": 30190, "category": "Border"},
    {"id": 30191, "category": "Border"},
    {"id": 30192, "category": "Border"},
    {"id": 30193, "category": "Border"},
    {"id": 30194, "category": "Border"},
    {"id": 30195, "category": "Border"},
    {"id": 30200, "category": "Border"},
    {"id": 30201, "category": "Border"},
    {"id": 30202, "category": "Border"},
    {"id": 30203, "category": "Border"},
    {"id": 30204, "category": "Border"},
    {"id": 30205, "category": "Border"},
    {"id": 30210, "category": "Border"},
    {"id": 30211, "category": "Border"},
    {"id": 30212, "category": "Border"},
    {"id": 30213, "category": "Border"},
    {"id": 30214, "category": "Border"},
    {"id": 30215, "category": "Border"},
    {"id": 35000, "category": "Border"},
    {"id": 35010, "category": "Border"},
    {"id": 35020, "category": "Border"},
    {"id": 35030, "category": "Border"},
    {"id": 36000, "category": "Border"},
    {"id": 36001, "category": "Border"},
    {"id": 36002, "category": "Border"},
    {"id": 36003, "category": "Border"},
    {"id": 36004, "category": "Border"},
    {"id": 36005, "category": "Border"},
    {"id": 36006, "category": "Border"},
    {"id": 36007, "category": "Border"},
    {"id": 36008, "category": "Border"},
    {"id": 36009, "category": "Border"},
    {"id": 38000, "category": "Border"},
    {"id": 39999, "category": "Border"},  # default
    # Titles
    {"id": 20000, "category": "Title"},
    {"id": 20001, "category": "Title"},
    {"id": 20002, "category": "Title"},
    {"id": 20003, "category": "Title"},
    {"id": 20004, "category": "Title"},
    {"id": 20005, "category": "Title"},
    {"id": 20006, "category": "Title"},
    {"id": 20007, "category": "Title"},
    {"id": 20100, "category": "Title"},
    {"id": 20101, "category": "Title"},
    {"id": 20102, "category": "Title"},
    {"id": 20103, "category": "Title"},
    {"id": 20104, "category": "Title"},
    {"id": 20105, "category": "Title"},
    {"id": 20110, "category": "Title"},
    {"id": 20111, "category": "Title"},
    {"id": 20112, "category": "Title"},
    {"id": 20113, "category": "Title"},
    {"id": 20114, "category": "Title"},
    {"id": 20115, "category": "Title"},
    {"id": 20120, "category": "Title"},
    {"id": 20121, "category": "Title"},
    {"id": 20122, "category": "Title"},
    {"id": 20123, "category": "Title"},
    {"id": 20124, "category": "Title"},
    {"id": 20125, "category": "Title"},
    {"id": 20130, "category": "Title"},
    {"id": 20131, "category": "Title"},
    {"id": 20132, "category": "Title"},
    {"id": 20133, "category": "Title"},
    {"id": 20134, "category": "Title"},
    {"id": 20135, "category": "Title"},
    {"id": 20140, "category": "Title"},
    {"id": 20141, "category": "Title"},
    {"id": 20142, "category": "Title"},
    {"id": 20143, "category": "Title"},
    {"id": 20144, "category": "Title"},
    {"id": 20145, "category": "Title"},
    {"id": 20150, "category": "Title"},
    {"id": 20151, "category": "Title"},
    {"id": 20152, "category": "Title"},
    {"id": 20153, "category": "Title"},
    {"id": 20154, "category": "Title"},
    {"id": 20155, "category": "Title"},
    {"id": 20160, "category": "Title"},
    {"id": 20161, "category": "Title"},
    {"id": 20162, "category": "Title"},
    {"id": 20163, "category": "Title"},
    {"id": 20164, "category": "Title"},
    {"id": 20165, "category": "Title"},
    {"id": 20170, "category": "Title"},
    {"id": 20171, "category": "Title"},
    {"id": 20172, "category": "Title"},
    {"id": 20173, "category": "Title"},
    {"id": 20174, "category": "Title"},
    {"id": 20175, "category": "Title"},
    {"id": 20180, "category": "Title"},
    {"id": 20181, "category": "Title"},
    {"id": 20182, "category": "Title"},
    {"id": 20183, "category": "Title"},
    {"id": 20184, "category": "Title"},
    {"id": 20185, "category": "Title"},
    {"id": 20190, "category": "Title"},
    {"id": 20191, "category": "Title"},
    {"id": 20192, "category": "Title"},
    {"id": 20193, "category": "Title"},
    {"id": 20194, "category": "Title"},
    {"id": 20195, "category": "Title"},
    {"id": 20200, "category": "Title"},
    {"id": 20201, "category": "Title"},
    {"id": 20202, "category": "Title"},
    {"id": 20203, "category": "Title"},
    {"id": 20204, "category": "Title"},
    {"id": 20205, "category": "Title"},
    {"id": 20210, "category": "Title"},
    {"id": 20211, "category": "Title"},
    {"id": 20212, "category": "Title"},
    {"id": 20213, "category": "Title"},
    {"id": 20214, "category": "Title"},
    {"id": 20215, "category": "Title"},
    {"id": 25000, "category": "Title"},
    {"id": 25010, "category": "Title"},
    {"id": 25020, "category": "Title"},
    {"id": 25030, "category": "Title"},
    {"id": 26000, "category": "Title"},
    {"id": 26001, "category": "Title"},
    {"id": 26002, "category": "Title"},
    {"id": 26003, "category": "Title"},
    {"id": 26004, "category": "Title"},
    {"id": 27000, "category": "Title"},
    {"id": 27001, "category": "Title"},
    {"id": 28000, "category": "Title"},
    {"id": 29999, "category": "Title"},  # default
]


def find_card_templates_file():
    """Try to locate data_definitions file with card templates."""
    _here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        CARD_TEMPLATES_FILE,
        os.path.join(DATA_DIR, "Data_definitions", "Templates.xml"),
        os.path.join(_here, "Data_definitions", "Templates.xml"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None

STARTER_DECKS = [   {   'id': 1,
        'name': 'Mulligan Elves',
        'faction': 'Scoiatael',
        'is_current': True,
        'user_cards': [   {'id': 101660, 'card_definition': None, 'state': 'New'},
                          {'id': 101674, 'card_definition': None, 'state': 'New'},
                          {'id': 100220, 'card_definition': None, 'state': 'New'},
                          {'id': 100221, 'card_definition': None, 'state': 'New'},
                          {'id': 100222, 'card_definition': None, 'state': 'New'},
                          {'id': 101120, 'card_definition': None, 'state': 'New'},
                          {'id': 101121, 'card_definition': None, 'state': 'New'},
                          {'id': 101122, 'card_definition': None, 'state': 'New'},
                          {'id': 100580, 'card_definition': None, 'state': 'New'},
                          {'id': 100581, 'card_definition': None, 'state': 'New'},
                          {'id': 100582, 'card_definition': None, 'state': 'New'},
                          {'id': 100196, 'card_definition': None, 'state': 'New'},
                          {'id': 100197, 'card_definition': None, 'state': 'New'},
                          {'id': 100198, 'card_definition': None, 'state': 'New'},
                          {'id': 101326, 'card_definition': None, 'state': 'New'},
                          {'id': 101570, 'card_definition': None, 'state': 'New'},
                          {'id': 101320, 'card_definition': None, 'state': 'New'},
                          {'id': 101318, 'card_definition': None, 'state': 'New'},
                          {'id': 101716, 'card_definition': None, 'state': 'New'},
                          {'id': 101568, 'card_definition': None, 'state': 'New'},
                          {'id': 101308, 'card_definition': None, 'state': 'New'},
                          {'id': 101236, 'card_definition': None, 'state': 'New'},
                          {'id': 101306, 'card_definition': None, 'state': 'New'},
                          {'id': 101114, 'card_definition': None, 'state': 'New'},
                          {'id': 101115, 'card_definition': None, 'state': 'New'},
                          {'id': 101116, 'card_definition': None, 'state': 'New'}]},
    {   'id': 2,
        'name': 'Cursed Adda',
        'faction': 'NorthernKingdom',
        'is_current': False,
        'user_cards': [   {'id': 101704, 'card_definition': None, 'state': 'New'},
                          {'id': 101624, 'card_definition': None, 'state': 'New'},
                          {'id': 101390, 'card_definition': None, 'state': 'New'},
                          {'id': 101466, 'card_definition': None, 'state': 'New'},
                          {'id': 101084, 'card_definition': None, 'state': 'New'},
                          {'id': 101085, 'card_definition': None, 'state': 'New'},
                          {'id': 101086, 'card_definition': None, 'state': 'New'},
                          {'id': 101096, 'card_definition': None, 'state': 'New'},
                          {'id': 101097, 'card_definition': None, 'state': 'New'},
                          {'id': 101098, 'card_definition': None, 'state': 'New'},
                          {'id': 101078, 'card_definition': None, 'state': 'New'},
                          {'id': 101079, 'card_definition': None, 'state': 'New'},
                          {'id': 101080, 'card_definition': None, 'state': 'New'},
                          {'id': 101090, 'card_definition': None, 'state': 'New'},
                          {'id': 101091, 'card_definition': None, 'state': 'New'},
                          {'id': 101092, 'card_definition': None, 'state': 'New'},
                          {'id': 101108, 'card_definition': None, 'state': 'New'},
                          {'id': 101109, 'card_definition': None, 'state': 'New'},
                          {'id': 101110, 'card_definition': None, 'state': 'New'},
                          {'id': 101252, 'card_definition': None, 'state': 'New'},
                          {'id': 101248, 'card_definition': None, 'state': 'New'},
                          {'id': 101244, 'card_definition': None, 'state': 'New'},
                          {'id': 101718, 'card_definition': None, 'state': 'New'},
                          {'id': 101720, 'card_definition': None, 'state': 'New'},
                          {'id': 101250, 'card_definition': None, 'state': 'New'},
                          {'id': 101526, 'card_definition': None, 'state': 'New'}]},
    {   'id': 3,
        'name': 'Wild Hunt Frost',
        'faction': 'Monster',
        'is_current': False,
        'user_cards': [   {'id': 101540, 'card_definition': None, 'state': 'New'},
                          {'id': 101554, 'card_definition': None, 'state': 'New'},
                          {'id': 101542, 'card_definition': None, 'state': 'New'},
                          {'id': 101550, 'card_definition': None, 'state': 'New'},
                          {'id': 101240, 'card_definition': None, 'state': 'New'},
                          {'id': 101546, 'card_definition': None, 'state': 'New'},
                          {'id': 101292, 'card_definition': None, 'state': 'New'},
                          {'id': 100736, 'card_definition': None, 'state': 'New'},
                          {'id': 100737, 'card_definition': None, 'state': 'New'},
                          {'id': 100738, 'card_definition': None, 'state': 'New'},
                          {'id': 100526, 'card_definition': None, 'state': 'New'},
                          {'id': 100527, 'card_definition': None, 'state': 'New'},
                          {'id': 100528, 'card_definition': None, 'state': 'New'},
                          {'id': 100160, 'card_definition': None, 'state': 'New'},
                          {'id': 100161, 'card_definition': None, 'state': 'New'},
                          {'id': 100958, 'card_definition': None, 'state': 'New'},
                          {'id': 100959, 'card_definition': None, 'state': 'New'},
                          {'id': 100166, 'card_definition': None, 'state': 'New'},
                          {'id': 100167, 'card_definition': None, 'state': 'New'},
                          {'id': 100016, 'card_definition': None, 'state': 'New'},
                          {'id': 100017, 'card_definition': None, 'state': 'New'},
                          {'id': 100018, 'card_definition': None, 'state': 'New'},
                          {'id': 101230, 'card_definition': None, 'state': 'New'},
                          {'id': 101234, 'card_definition': None, 'state': 'New'},
                          {'id': 101278, 'card_definition': None, 'state': 'New'},
                          {'id': 101420, 'card_definition': None, 'state': 'New'}]},
    {   'id': 4,
        'name': 'Emhyr Spies',
        'faction': 'Nilfgaard',
        'is_current': False,
        'user_cards': [   {'id': 101654, 'card_definition': None, 'state': 'New'},
                          {'id': 101620, 'card_definition': None, 'state': 'New'},
                          {'id': 101738, 'card_definition': None, 'state': 'New'},
                          {'id': 101598, 'card_definition': None, 'state': 'New'},
                          {'id': 101604, 'card_definition': None, 'state': 'New'},
                          {'id': 101380, 'card_definition': None, 'state': 'New'},
                          {'id': 101374, 'card_definition': None, 'state': 'New'},
                          {'id': 101218, 'card_definition': None, 'state': 'New'},
                          {'id': 101376, 'card_definition': None, 'state': 'New'},
                          {'id': 101378, 'card_definition': None, 'state': 'New'},
                          {'id': 101358, 'card_definition': None, 'state': 'New'},
                          {'id': 100682, 'card_definition': None, 'state': 'New'},
                          {'id': 100683, 'card_definition': None, 'state': 'New'},
                          {'id': 100712, 'card_definition': None, 'state': 'New'},
                          {'id': 100713, 'card_definition': None, 'state': 'New'},
                          {'id': 100714, 'card_definition': None, 'state': 'New'},
                          {'id': 100322, 'card_definition': None, 'state': 'New'},
                          {'id': 100688, 'card_definition': None, 'state': 'New'},
                          {'id': 100689, 'card_definition': None, 'state': 'New'},
                          {'id': 100316, 'card_definition': None, 'state': 'New'},
                          {'id': 100317, 'card_definition': None, 'state': 'New'},
                          {'id': 100298, 'card_definition': None, 'state': 'New'},
                          {'id': 100299, 'card_definition': None, 'state': 'New'},
                          {'id': 100844, 'card_definition': None, 'state': 'New'},
                          {'id': 100838, 'card_definition': None, 'state': 'New'},
                          {'id': 100334, 'card_definition': None, 'state': 'New'}]},
    {   'id': 5,
        'name': 'Bran Discard',
        'faction': 'Skellige',
        'is_current': False,
        'user_cards': [   {'id': 101648, 'card_definition': None, 'state': 'New'},
                          {'id': 101684, 'card_definition': None, 'state': 'New'},
                          {'id': 101672, 'card_definition': None, 'state': 'New'},
                          {'id': 101590, 'card_definition': None, 'state': 'New'},
                          {'id': 101584, 'card_definition': None, 'state': 'New'},
                          {'id': 101354, 'card_definition': None, 'state': 'New'},
                          {'id': 101348, 'card_definition': None, 'state': 'New'},
                          {'id': 101340, 'card_definition': None, 'state': 'New'},
                          {'id': 101350, 'card_definition': None, 'state': 'New'},
                          {'id': 101346, 'card_definition': None, 'state': 'New'},
                          {'id': 101352, 'card_definition': None, 'state': 'New'},
                          {'id': 100892, 'card_definition': None, 'state': 'New'},
                          {'id': 100652, 'card_definition': None, 'state': 'New'},
                          {'id': 100653, 'card_definition': None, 'state': 'New'},
                          {'id': 100654, 'card_definition': None, 'state': 'New'},
                          {'id': 100646, 'card_definition': None, 'state': 'New'},
                          {'id': 100647, 'card_definition': None, 'state': 'New'},
                          {'id': 100648, 'card_definition': None, 'state': 'New'},
                          {'id': 100748, 'card_definition': None, 'state': 'New'},
                          {'id': 100749, 'card_definition': None, 'state': 'New'},
                          {'id': 100750, 'card_definition': None, 'state': 'New'},
                          {'id': 100898, 'card_definition': None, 'state': 'New'},
                          {'id': 100899, 'card_definition': None, 'state': 'New'},
                          {'id': 100256, 'card_definition': None, 'state': 'New'},
                          {'id': 100257, 'card_definition': None, 'state': 'New'},
                          {'id': 100258, 'card_definition': None, 'state': 'New'}]}]

def load_card_pool():
    """Parse card templates and group by rarity. Returns {rarity: [templateId, ...]}"""
    global _card_pool, CARD_TEMPLATES_FILE
    if _card_pool is not None:
        return _card_pool

    _card_pool = {1: [], 2: [], 4: [], 8: []}

    if CARD_TEMPLATES_FILE is None:
        CARD_TEMPLATES_FILE = find_card_templates_file()

    if CARD_TEMPLATES_FILE and os.path.exists(CARD_TEMPLATES_FILE):
        try:
            with open(CARD_TEMPLATES_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            wrapped = "<root>" + content + "</root>"
            root = ET.fromstring(wrapped)
            for tmpl in root.findall(".//Template[@Type='CardTemplate']"):
                tid = int(tmpl.get("Id", "0"))
                avail = tmpl.get("Availability", "0")
                if avail != "1":
                    continue
                rarity_el = tmpl.find("Rarity")
                kind_el = tmpl.find("Kind")
                if rarity_el is None or kind_el is None:
                    continue
                rarity = int(rarity_el.text)
                kind = int(kind_el.text)
                if kind != 1:
                    continue
                if rarity in _card_pool:
                    _card_pool[rarity].append(tid)
                faction_el = tmpl.find("FactionId")
                if faction_el is not None:
                    _faction_map[tid] = int(faction_el.text)
            total = sum(len(v) for v in _card_pool.values())
            print(f"Loaded card pool: {total} cards (Common:{len(_card_pool[1])}, Rare:{len(_card_pool[2])}, Epic:{len(_card_pool[4])}, Legendary:{len(_card_pool[8])})")
        except Exception as e:
            print(f"Warning: Could not parse card templates from {CARD_TEMPLATES_FILE}: {e}")
            _card_pool = None
            return get_fallback_card_pool()
    else:
        print("Warning: Card templates file not found. Using fallback card pool.")
        return get_fallback_card_pool()

    if not _card_pool[1] and not _card_pool[2]:
        print("Warning: No common/rare cards found. Using fallback.")
        _card_pool = None
        return get_fallback_card_pool()

    return _card_pool

def generate_keg_cards():
    """
    Generate a keg following Gwent 0.9.24 rules:
    - 4 non-choosable cards: base 80% common / 20% rare, with small upgrade chances:
        each card has a 3% chance to upgrade to epic, 1% to legendary
        each card (after rarity) has a 1% chance to be premium
    - 3 choosable cards: ALL the same rarity, rolled once:
        70% rare, 20% epic, 10% legendary (no commons)
        each card has a 1% chance to be premium
    Returns (non_choosable_full_ids, choosable_full_ids) where full_id = templateId * 100 (+1 if premium)
    """
    pool = load_card_pool()

    def pick_card(rarity):
        """Pick a random template from the given rarity, fall back up if empty."""
        for r in [rarity, 2, 4, 8, 1]:
            if pool.get(r):
                return random.choice(pool[r]), r
        return None, rarity

    def full_id(template_id, premium):
        return template_id * 100 + (1 if premium else 0)

    # Non-choosable: 4 cards, individual rarity rolls with small upgrade chances
    non_choosable_full = []
    for _ in range(4):
        roll = random.random()
        if roll < 0.01:
            rarity = 8       # 1% legendary
        elif roll < 0.04:
            rarity = 4       # 3% epic
        elif roll < 0.24:
            rarity = 2       # 20% rare
        else:
            rarity = 1       # 76% common
        tmpl, _ = pick_card(rarity)
        premium = random.random() < 0.01
        non_choosable_full.append(full_id(tmpl, premium))

    # Choosable: roll rarity ONCE, all 3 cards share it
    roll = random.random()
    if roll < 0.10:
        choosable_rarity = 8    # 10% legendary
    elif roll < 0.30:
        choosable_rarity = 4    # 20% epic
    else:
        choosable_rarity = 2    # 70% rare

    choosable_full = []
    for _ in range(3):
        tmpl, _ = pick_card(choosable_rarity)
        premium = random.random() < 0.01
        choosable_full.append(full_id(tmpl, premium))

    return non_choosable_full, choosable_full


def _extract_user_id(path):
    """Extract the user ID from a /users/{id}/... URL path."""
    m = re.search(r"/users/(\d+)", path)
    return m.group(1) if m else None

def _extract_uid_from_jwt(auth_header):
    """Extract user_id (sub claim) from Bearer JWT token."""
    try:
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            import base64, json as _json
            payload = token.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            data = _json.loads(base64.urlsafe_b64decode(payload))
            return int(data.get("sub", 0))
    except Exception:
        pass
    return 0


def load_data(user_id=None):
    if USE_SQLITE:
        return _db.load_data(user_id)
    default = {
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
    # Determine which file to load
    if user_id:
        filepath = _user_data_path(user_id)
    else:
        filepath = DATA_FILE  # fallback for non-user-specific routes
    lock = _get_user_lock(user_id or "default")
    with lock:
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                saved = json.load(f)
            # Merge: fill in any missing keys from default without overwriting saved data
            changed = False
            for key, val in default.items():
                if key not in saved:
                    saved[key] = val
                    changed = True
            if changed:
                with open(filepath, "w") as f:
                    json.dump(saved, f, indent=2)
            return saved
        # Create new data file from defaults
        with open(filepath, "w") as f:
            json.dump(default, f, indent=2)
    return default

def save_data(data, user_id=None):
    if USE_SQLITE:
        _db.save_data(data, user_id)
        return
    if user_id:
        filepath = _user_data_path(user_id)
    else:
        filepath = DATA_FILE
    lock = _get_user_lock(user_id or "default")
    with lock:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

def get_currency(data, currency_id):
    for c in data["currencies"]:
        if c["currency"]["id"] == currency_id:
            return c
    return None

def _get_user_deltas(user_id):
    """Get or create per-user session currency deltas."""
    uid = str(user_id) if user_id else "default"
    if uid not in session_currency_deltas:
        session_currency_deltas[uid] = {1: 0, 2: 0, 3: 0}
    return session_currency_deltas[uid]

def apply_currency_modifications(modifications, user_id=None):
    deltas = _get_user_deltas(user_id)
    for mod in modifications:
        cid = mod["id"]
        if cid in deltas:
            deltas[cid] += mod["amount"]


# ── Match reward definitions ─────────────────────────────────────────────────
# Reward definitions live in rewards.json next to this script — that's the
# verbatim GOG-server `/rewards` response captured from the live game. The
# file has 975 entries spanning level_up, rank_up, season_end, accomplishment,
# quest_*, registration, etc. We serve it as-is on GET /rewards, and we look
# up accomplishment entries by their `event_params.accomplishment` string
# when granting rewards after a match.
REWARDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rewards.json")

_rewards_dump_cache = None

def load_rewards_dump():
    """Lazy-load the rewards.json dump, caching it for the process lifetime."""
    global _rewards_dump_cache
    if _rewards_dump_cache is None:
        try:
            with open(REWARDS_FILE, "r", encoding="utf-8") as f:
                _rewards_dump_cache = json.load(f)
        except FileNotFoundError:
            print(f"[REWARDS] {REWARDS_FILE} not found — serving empty collection")
            _rewards_dump_cache = {"items": [], "total_count": 0,
                                   "limit": 2000, "page_token": "1",
                                   "next_page_token": None}
        except Exception as e:
            print(f"[REWARDS] Failed to load {REWARDS_FILE}: {e}")
            _rewards_dump_cache = {"items": [], "total_count": 0,
                                   "limit": 2000, "page_token": "1",
                                   "next_page_token": None}
    return _rewards_dump_cache


def find_accomplishment_reward(acc_type):
    """Return the rewards block for the named accomplishment, or None."""
    for entry in load_rewards_dump().get("items", []):
        if entry.get("event_type") != "accomplishment":
            continue
        if entry.get("event_params", {}).get("accomplishment") != acc_type:
            continue
        opts = entry.get("rewards", {}).get("options", [])
        if not opts:
            return None
        return opts[0].get("rewards") or {}
    return None


def find_level_up_reward(level):
    """Return the rewards block for the level_up event at `level`, or None."""
    for entry in load_rewards_dump().get("items", []):
        if entry.get("event_type") != "level_up":
            continue
        if entry.get("event_params", {}).get("level") != level:
            continue
        opts = entry.get("rewards", {}).get("options", [])
        if not opts:
            return None
        return opts[0].get("rewards") or {}
    return None


def grant_level_up_reward(data, level):
    """Apply the level_up reward for `level` to `data` and return granted goods.

    Returns dict: {"currencies": [...], "items": [...], "cards": [...],
                   "currency_totals": {"1": gold, "2": scraps, "3": powder}}
    Does NOT touch XP or crown pieces -- those are already handled by the
    caller before grant_level_up_reward is invoked.
    """
    reward = find_level_up_reward(level)
    granted_currencies = []
    granted_items      = []
    granted_cards      = []

    if reward is None:
        currency_totals = {str(c["currency"]["id"]): c["amount"]
                           for c in data.get("currencies", [])}
        return {"currencies": [], "items": [], "cards": [],
                "currency_totals": currency_totals}

    # Currencies
    for curr in (reward.get("currencies") or []):
        cid    = curr["id"]
        amount = curr.get("amount") or curr.get("min_amount") or 0
        for c in data.setdefault("currencies", []):
            if c["currency"]["id"] == cid:
                c["amount"] = c.get("amount", 0) + amount
                granted_currencies.append({"id": cid, "amount": amount})
                break

    # Items (kegs, vanity, etc.)
    for it in (reward.get("item_definitions") or []):
        item_def_id = it["id"]
        if item_def_id == 1:
            inv = data.setdefault("inventory", {})
            inv["1"] = inv.get("1", 0) + 1
        user_item_id = int(time.time() * 1000) + len(granted_items)
        granted_items.append({"id": user_item_id, "item_def_id": item_def_id})

    # Cards
    pool = load_card_pool() if (reward.get("cards") or []) else None
    for card in (reward.get("cards") or []):
        try:
            full_id = int(card["card_definition_id"])
        except (KeyError, TypeError, ValueError):
            continue
        template_id = full_id // 100
        is_premium  = (full_id % 100) == 1
        rarity = 1
        if pool:
            for r in (8, 4, 2, 1):
                if template_id in pool.get(r, []):
                    rarity = r
                    break
        amount = int(card.get("amount", 1) or 1)
        for _ in range(amount):
            new_id = data.setdefault("next_card_id", 100001)
            data["next_card_id"] = new_id + 1
            data.setdefault("cards", []).append({
                "id": new_id,
                "card_definition": {
                    "id":               full_id,
                    "card_template_id": template_id,
                    "rarity":           rarity,
                    "premium":          is_premium,
                    "is_deleted":       False,
                },
                "state": "New",
            })
            granted_cards.append({
                "id":                new_id,
                "card_definition_id": full_id,
                "rarity":            rarity,
                "premium":           is_premium,
            })

    currency_totals = {str(c["currency"]["id"]): c["amount"]
                       for c in data.get("currencies", [])}
    return {
        "currencies":      granted_currencies,
        "items":           granted_items,
        "cards":           granted_cards,
        "currency_totals": currency_totals,
    }


def grant_reward(data, acc_type, rounds_won=1):
    """Apply the reward for `acc_type` from rewards.json to `data` (currencies,
    inventory, cards, experience) and return the granted goods so the broker
    can announce them to the client.

    Returns dict: { "currencies": [...], "items": [...], "cards": [...],
                    "currency_totals": {"1": <gold>, "2": <scraps>, "3": <powder>} }
    """
    reward = find_accomplishment_reward(acc_type)
    granted_currencies = []
    granted_items      = []
    granted_cards      = []

    if reward is None:
        currency_totals = {str(c["currency"]["id"]): c["amount"]
                           for c in data.get("currencies", [])}
        return {"currencies": [], "items": [], "cards": [],
                "currency_totals": currency_totals}

    # ── Currencies ──
    for curr in (reward.get("currencies") or []):
        cid    = curr["id"]
        amount = curr.get("amount") or curr.get("min_amount") or 0
        for c in data.setdefault("currencies", []):
            if c["currency"]["id"] == cid:
                c["amount"] = c.get("amount", 0) + amount
                granted_currencies.append({"id": cid, "amount": amount})
                break

    # ── Items (kegs, vanity drops, etc.) ──
    for it in (reward.get("item_definitions") or []):
        item_def_id = it["id"]
        # Card Keg (item def 1) → bump the keg count in inventory
        if item_def_id == 1:
            inv = data.setdefault("inventory", {})
            inv["1"] = inv.get("1", 0) + 1
        else:
            # Vanity / other items → add to equipped_vanity-style "owned" tracking
            # NB: the game pulls the full owned-vanity list from GET /items, which
            # uses the static VANITY_ITEMS table. Items granted by rewards.json
            # that aren't in VANITY_ITEMS yet won't show up in the trinket menu
            # without extending that list.
            pass
        # Each granted item needs a unique user-item id so the client treats it
        # as a distinct UserItem.
        user_item_id = int(time.time() * 1000) + len(granted_items)
        granted_items.append({"id": user_item_id, "item_def_id": item_def_id})

    # ── Cards ──
    # Dump shape per card: {"amount": int, "card_definition_id": "<full id as string>"}
    # We need to:
    #   - normalise card_definition_id to int (it ships as a string in the dump)
    #   - grant `amount` copies
    #   - look up rarity from the card pool so the granted UserCard has the right
    #     metadata, and so the rewards screen shows the correct rarity glow
    #   - append the card to data["cards"] so the player actually owns it
    pool = load_card_pool() if (reward.get("cards") or []) else None
    for card in (reward.get("cards") or []):
        try:
            full_id = int(card["card_definition_id"])
        except (KeyError, TypeError, ValueError):
            continue
        template_id = full_id // 100
        is_premium  = (full_id % 100) == 1
        rarity = 1
        if pool:
            for r in (8, 4, 2, 1):
                if template_id in pool.get(r, []):
                    rarity = r
                    break
        amount = int(card.get("amount", 1) or 1)
        for _ in range(amount):
            new_id = data.setdefault("next_card_id", 100001)
            data["next_card_id"] = new_id + 1
            data.setdefault("cards", []).append({
                "id": new_id,
                "card_definition": {
                    "id":               full_id,
                    "card_template_id": template_id,
                    "rarity":           rarity,
                    "premium":          is_premium,
                    "is_deleted":       False,
                },
                "state": "New",
            })
            granted_cards.append({
                "id": new_id,
                "card_definition_id": full_id,
                "rarity": rarity,
                "premium": is_premium,
            })

    # ── XP / level ──
    # Accomplishment/challenge completions are wins; award base-win XP plus the
    # per-round performance bonus for the rounds actually won this match.
    xp_before = data.get("experience", 0)
    data["experience"] = xp_before + compute_match_xp(True, rounds_won)
    level_before, cells_before = compute_level_and_cells(xp_before)
    level_after,  cells_after  = compute_level_and_cells(data["experience"])
    data["level"]        = level_after
    data["filled_cells"] = cells_after

    # ── Daily crown pieces (one per round won, up to daily cap) ──
    maybe_reset_daily(data)
    crowns_before  = data.get("crown_pieces_today", 0)
    crowns_to_add = min(rounds_won, get_crown_cap(crowns_before) - crowns_before)
    if crowns_to_add > 0:
        data["crown_pieces_today"] = crowns_before + crowns_to_add
    data["wins_today"] = data.get("wins_today", 0) + 1
    crowns_after = data["crown_pieces_today"]
    wins_today   = data["wins_today"]

    currency_totals = {str(c["currency"]["id"]): c["amount"]
                       for c in data.get("currencies", [])}

    return {
        "currencies":      granted_currencies,
        "items":           granted_items,
        "cards":           granted_cards,
        "currency_totals": currency_totals,
        "xp_before":       xp_within_level(xp_before),
        "xp_after":        xp_within_level(data["experience"]),
        "level_before":    level_before,
        "level_after":     level_after,
        "cells_before":    cells_before,
        "cells_after":     cells_after,
        "crowns_before":   crowns_before,
        "crowns_after":    crowns_after,
        "wins_today":      wins_today,
    }


class GwentHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Suppress noisy repeated token refresh requests
        if "/token" in self.path and "refresh_token" in self.path:
            return
        print(f"{self.command} {self.path} - {args[0]}")

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def collection_response(self, items):
        return {"items": items, "total_count": len(items), "limit": 100, "page_token": "", "next_page_token": ""}

    def get_user_products(self):
        return json.loads('{"items":[],"total_count":0,"limit":"9223372036854775807","page_token":1,"next_page_token":null}')

    def get_shop_products(self):
        return {
            "items": [
                {
                    "id": "49085943257433578",
                    "platform_specific_id": None,
                    "platform": "GOG",
                    "consumable": True,
                    "country": None,
                    "prices": [{"currency": "VIRTUAL", "price": 100, "display_price": "100",
                                "available_from": "2010-01-01T01:00:00+0000",
                                "available_to": "2077-01-01T01:00:00+0000", "type": "normal"}],
                    "image_url": "",
                    "translated_product_info": {"language": "en-US", "name": "Gwent Card Keg",
                                                "description": "A keg containing 5 cards.",
                                                "category_name": "Card Packs"},
                    "items": [{"item_id": 1, "count": 1, "type": "inventory_item"}],
                    "category": {"id": "CardPacks"}
                },
                {
                    "id": "317845682944953259",
                    "platform_specific_id": None,
                    "platform": "GOG",
                    "consumable": True,
                    "country": None,
                    "prices": [{"currency": "VIRTUAL", "price": 100, "display_price": "100",
                                "available_from": "2010-01-01T01:00:00+0000",
                                "available_to": "2077-01-01T01:00:00+0000", "type": "normal"}],
                    "image_url": "",
                    "translated_product_info": {"language": "en-US", "name": "500 Meteorite Powder",
                                                "description": "Transmute cards to premium.",
                                                "category_name": "Powder"},
                    "items": [{"item_id": 3, "count": 500, "type": "inventory_currency"}],
                    "category": {"id": "Gems"}
                }
            ],
            "total_count": 2,
            "limit": "9223372036854775807",
            "page_token": 1,
            "next_page_token": None
        }

    def handle_transaction(self, body, uid=None):
        try:
            total_price = body.get("total_price", 0)
            products = body.get("products", [])
            data = load_data(uid)

            # Deduct gold
            for c in data["currencies"]:
                if c["currency"]["id"] == 1:
                    c["amount"] = max(0, c["amount"] - total_price)
                    break

            # Track kegs in inventory
            inventory = data.setdefault("inventory", {})
            total_kegs = sum(p.get("count", 1) for p in products)
            inventory["1"] = inventory.get("1", 0) + total_kegs
            save_data(data, uid)

            transaction_id = int(time.time() * 1000)
            self.send_json({
                "transaction": {
                    "id": transaction_id,
                    "user_id": str(uid or "50069134988124048"),
                    "user_tracking_number": str(transaction_id),
                    "date_started": "2026-02-22T00:00:00+0000",
                    "date_modified": "2026-02-22T00:00:00+0000",
                    "state": "Finished",
                    "reason": None,
                    "total_price": total_price,
                    "currency": "VIRTUAL",
                    "country": None,
                    "products": [
                        {
                            "id": i + 1,
                            "platform_product": {
                                "product_id": p.get("product_id", 0),
                                "platform": "GOG",
                                "platform_specific_id": None,
                                "image_url": ""
                            },
                            "count": p.get("count", 1)
                        }
                        for i, p in enumerate(products)
                    ]
                },
                "platform_payload": {
                    "order_id": str(transaction_id),
                    "checkout_url": None,
                    "signed_offers": []
                }
            })
        except Exception as e:
            print(f"Transaction error: {e}")
            self.send_response(500)
            self.end_headers()

    # --- GET ---
    def do_GET(self):
        path = self.path.split("?")[0]
        uid = _extract_user_id(path)
        data = load_data(uid)

        # NOTE: More specific routes must come before less specific ones.
        # e.g. /cards/public before /cards, /public (host-aware) before generic /public

        # ---- GOG Auth API (auth.gog.com) ----
        # GET /token — returns fake OAuth token; extracts user_id from the
        # incoming refresh_token (format: "privateserver_{user_id}")
        host = self.headers.get("Host", "")
        if path == "/token" and "auth" in host:
            import secrets, base64 as _b64
            from urllib.parse import urlparse, parse_qs as _parse_qs
            qs = _parse_qs(urlparse(self.path).query)
            incoming_refresh = qs.get("refresh_token", [""])[0]
            # Extract user_id from "privateserver_{user_id}" token
            if incoming_refresh.startswith("privateserver_"):
                user_id = incoming_refresh[len("privateserver_"):]
            else:
                user_id = str(uid or "50069134988124048")
            # Build a fake JWT with user_id in the 'sub' claim —
            # the SDK parses the access_token as a JWT to extract user_id
            jwt_header = _b64.urlsafe_b64encode(json.dumps({"alg":"HS256","typ":"JWT"}).encode()).rstrip(b"=").decode()
            jwt_payload = _b64.urlsafe_b64encode(json.dumps({
                "sub": user_id,
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
                "jti": secrets.token_hex(16)
            }).encode()).rstrip(b"=").decode()
            jwt_sig = _b64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
            fake_access = f"{jwt_header}.{jwt_payload}.{jwt_sig}"
            # Echo back the same refresh token so the SDK keeps using it
            fake_session = str(abs(hash(time.time())) % (10**19))
            self.send_json({
                "access_token": fake_access,
                "refresh_token": incoming_refresh if incoming_refresh else f"privateserver_{user_id}",
                "token_type": "bearer",
                "expires_in": 3600,
                "session_id": fake_session,
                "user_id": user_id
            })
            return

        # ---- GOG Users API (users.gog.com) ----
        # GET /users?ids=<id1>,<id2>  — returns user profile info from users.json
        if path == "/users":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            requested_ids = []
            if "ids" in qs:
                requested_ids = [i.strip() for i in qs["ids"][0].split(",")]

            # Load all known users
            all_users = _load_users_json()
            users_by_id = {str(u["id"]): u for u in all_users}

            items = []
            for rid in requested_ids:
                user_cfg = users_by_id.get(str(rid))
                if user_cfg:
                    avatar_img = user_cfg.get("avatar_image_id", "")
                    # SDK requires all avatar fields to be present (even if empty strings)
                    dummy_img = avatar_img or "00000000000000000000000000000000000000000000000000000000000000000000"
                    base_url = f"https://images.gog.com/{dummy_img}"
                    avatar_block = {
                        "gog_image_id": dummy_img,
                        "small": f"{base_url}_avs.jpg",
                        "small_2x": f"{base_url}_avs2.jpg",
                        "medium": f"{base_url}_avm.jpg",
                        "medium_2x": f"{base_url}_avm2.jpg",
                        "large": f"{base_url}_avl.jpg",
                        "large_2x": f"{base_url}_avl2.jpg",
                        "sdk_img_32": f"{base_url}_sdk_img32.jpg",
                        "sdk_img_64": f"{base_url}_sdk_img64.jpg",
                        "sdk_img_184": f"{base_url}_sdk_img184.jpg",
                        "menu_small": f"{base_url}_menu_user_av_small.jpg",
                        "menu_small_2": f"{base_url}_menu_user_av_small2.jpg",
                        "menu_big": f"{base_url}_menu_user_av_big.jpg",
                        "menu_big_2": f"{base_url}_menu_user_av_big2.jpg"
                    }
                    items.append({
                        "id": str(rid),
                        "username": user_cfg.get("username", "Player"),
                        "created_date": user_cfg.get("created_date", "2017-01-01T00:00:00+00:00"),
                        "avatar": avatar_block,
                        "is_employee": False,
                        "tags": ["playgwent"]
                    })

            # The SDK's GetUsersInfoListener tries "items" first, then falls
            # back to parsing the whole response as a single user object.
            # Return the single-user format when only one ID was requested.
            if len(items) == 1:
                self.send_json(items[0])
            else:
                self.send_json({
                    "total_count": len(items),
                    "limit": 250,
                    "items": items
                })
            return

        # ---- Cards ----
        # /users/{id}/cards/public  — MUST be before /users/{id}/cards
        if re.match(r"^/users/\d+/cards/public$", path):
            load_card_pool()  # ensure _faction_map is populated

            faction_keys = ["Neutral", "Monster", "Nilfgaard", "NorthernKingdom", "Scoiatael", "Skellige"]
            any_counts         = {k: 0 for k in faction_keys}
            non_premium_counts = {k: 0 for k in faction_keys}
            premium_counts     = {k: 0 for k in faction_keys}

            # The game's max (875) represents a complete standard-only collection.
            # any/non_premium count standard copies; premium counts premium copies.
            # A full standard+premium collection should show 875/875 on each bar.
            for card in data["cards"]:
                full_id = card["card_definition"]["id"]
                template_id = full_id // 100
                is_premium = (full_id % 100) == 1
                faction_int = _faction_map.get(template_id)
                if faction_int is None:
                    continue
                faction_str = FACTION_INT_TO_GOG.get(faction_int)
                if faction_str is None:
                    continue
                if is_premium:
                    premium_counts[faction_str] += 1
                else:
                    any_counts[faction_str] += 1
                    non_premium_counts[faction_str] += 1
                    
            favourite_card_id = data.get("favourite_card_id", 11210101)
            favourite_faction_id = data.get("favourite_faction") or "Neutral"
            self.send_json({
                "collection": {
                    "any":         any_counts,
                    "non_premium": non_premium_counts,
                    "premium":     premium_counts
                },
                "favourite_card": {"card_definition": {"id": favourite_card_id}},
                "favourite_faction": favourite_faction_id
            })

        elif re.match(r"^/users/\d+/cards$", path):
            self.send_json(self.collection_response(data["cards"]))

        # ---- Decks ----
        elif re.match(r"^/users/\d+/decks$", path):
            self.send_json(self.collection_response(data["decks"]))

        # ---- Currencies ----
        elif re.match(r"^/users/\d+/currencies$", path):
            deltas = _get_user_deltas(uid)
            for c in data["currencies"]:
                cid = c["currency"]["id"]
                if cid in deltas:
                    c["amount"] = max(0, c["amount"] + deltas[cid])
            # Reset this user's deltas
            session_currency_deltas[str(uid) if uid else "default"] = {1: 0, 2: 0, 3: 0}
            save_data(data, uid)
            self.send_json(self.collection_response(data["currencies"]))

        # ---- Shop ----
        elif re.match(r"^/platforms/GOG/products$", path):
            self.send_json(self.get_shop_products())
            
        elif re.match(r"^/users/\d+/seasons$", path):
            self.send_json({
                "items": [{"id": 1, "name": "Season 1", "is_current": True,
                           "start_date": "2017-01-01T00:00:00",
                           "end_date": "2027-01-01T00:00:00"}],
                "total_count": 1
            })

        # ---- Public profile — host-aware, must distinguish profile vs rankings/inventory ----
        elif re.match(r"^/users/\d+/public$", path):
            host = self.headers.get("Host", "")
            print(f"[DEBUG /public] uid={uid} host={host}")
            if "profile" in host:
                maybe_reset_daily(data)
                self.send_json({
                    "id": str(uid or "50069134988124048"),
                    "progress_bar": {"level": data.get("level", 1), "crown_pieces": data.get("crown_pieces_today", 0)},
                    "accomplishments": data.get("accomplishments", []),
                    "stats": {
                        "wins": {
                            "Monster": 0, "Nilfgaard": 0, "NorthernKingdom": 0,
                            "Scoiatael": 0, "Skellige": 0
                        },
                        "ggs_sent_count": 0,
                        "ggs_received_count": 0
                    },
                    "public_profile_hidden": False,
                    "platform": "GOG"
                })
            elif "inventory" in host:
                equipped_vanity = data.get("equipped_vanity", [
                    {"id": 40008, "category": "Avatar"},
                    {"id": 39999, "category": "Border"},
                    {"id": 29999, "category": "Title"},])
                equipped = []
                for v in equipped_vanity:
                    equipped.append({
                        "id": v["id"],
                        "item_definition": {
                            "id": v["id"],
                            "item_template": {
                                "id": v["id"],
                                "name": "",
                                "category": v["category"],
                                "consumable": False
                            },
                            "parameters": {}
                        },
                        "state": "Equipped",
                        "user_id": str(uid or "50069134988124048")
                    })
                self.send_json({"equipped": equipped})
            else:
                # rankings + inventory domains
                self.send_json({
                    "current_season": {"rank_id": 1, "season_id": 1},
                    "best_season": {"rank_id": 5, "season_id": 1}
                })
                
        # Bare user lookup — seawolf-profile login check
        elif re.match(r"^/users/\d+$", path):
            maybe_reset_daily(data)
            now        = datetime.now(timezone.utc)
            end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
            expires_in = int((end_of_day - now).total_seconds())
            self.send_json({
                 "id": str(uid or "50069134988124048"),
                        # within-level XP, matching the semantics ProfileManager
                        # stores from the match-end profile_progress notification
                        "experience": xp_within_level(data.get("experience", 0)),
                        "progress_bar": {
                            "level":        data.get("level", 1),
                            "filled_cells": data.get("filled_cells", 0),
                            "wins_count":   data.get("wins_today", 0),
                            "date_created": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "date_expires": end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "expires_in":   expires_in,
                            "crown_pieces": data.get("crown_pieces_today", 0),
                        },
                        "accomplishments": data.get("accomplishments", []),
                        "stats": {
                            "wins": {
                                "Monster": 0, "Nilfgaard": 0, "NorthernKingdom": 0,
                                "Scoiatael": 0, "Skellige": 0
                            },
                            "ggs_sent_count": 0,
                            "ggs_received_count": 0
                        },
                        "equipped_items": [
                            {
                                "id": 40008,
                                "item_definition": {
                                    "id": 40008,
                                    "item_template": {
                                        "id": 40008,
                                        "name": "Default Avatar",
                                        "category": "Avatar",
                                        "consumable": False
                                    },
                                    "parameters": {}
                                },
                                "state": "Equipped",
                                "user_id": str(uid or "50069134988124048")
                            }
                        ],
                        "public_profile_hidden": False,
                        "platform": "GOG"
                    })

        # ---- Rankings seasons ----
        # The game requests /rankings/seasons (not /seasons)
        elif re.match(r"^/rankings/seasons$", path):
            self.send_json({
                "items": [{"id": 1, "name": "Season 1", "is_current": True,
                           "start_date": "2017-01-01T00:00:00",
                           "end_date": "2027-01-01T00:00:00"}],
                "total_count": 1
            })

        # ---- Legacy /seasons fallback ----
        elif re.match(r"^/seasons$", path):
            self.send_json({
                "items": [{"id": 1, "name": "Season 1", "is_current": True,
                           "start_date": "2017-01-01T00:00:00",
                           "end_date": "2027-01-01T00:00:00"}],
                "total_count": 1
            })

        # ---- Quests (gwent-quests.gog.com) ----
        # The game GETs https://gwent-quests.gog.com/quest_definitions and
        # /users/{id}/quests?state=active. This host was NOT redirected and had
        # no handler, so the request escaped to the REAL (dead) gwent-quests
        # server, timed out (Code:0 request_timeout, IsImportant=True), and the
        # game's ServiceEvents fired ServiceTimeout -> GlobalNetworkManager
        # "Enqueue connection lost" -> [SessionManager] Connection lost -> sign
        # out (this was the real "service interrupted" cause; see GwentClient.log).
        # Return EMPTY collections: satisfies the request with no quest-reward
        # logic to implement. (gwent-quests.gog.com is now in the redirect lists.)
        elif re.match(r"^/quest_definitions$", path):
            self.send_json({"items": [], "total_count": 0, "limit": 100,
                            "page_token": "", "next_page_token": None})

        elif re.match(r"^/users/\d+/quests$", path):
            self.send_json({"items": [], "total_count": 0, "limit": 100,
                            "page_token": "", "next_page_token": None})

        # ---- User products / payment methods ----
        elif re.match(r"^/users/\d+/products$", path):
            self.send_json(self.get_user_products())

        elif re.match(r"^/users/\d+/platforms/GOG/payment_methods$", path):
            self.send_json({"items": {}})

        # ---- User items (inventory / kegs) ----
        elif re.match(r"^/users/\d+/items$", path):
            inventory = data.get("inventory", {})
            items = []
            for item_id, count in inventory.items():
                for i in range(count):
                    items.append({
                        "id": int(item_id) * 1000 + i + 1,
                        "item_definition": {
                            "id": int(item_id),
                            "item_template": {
                                "id": int(item_id),
                                "name": "Card Keg",
                                "category": "CardPack",
                                "consumable": True
                            },
                            "parameters": {
                                "chooseable_count": 1,
                                "non_chooseable_count": 4,
                                "chooseable_restrictions": {"rarity": 2, "premium": False}
                            }
                        },
                        "state": "New",
                        "user_id": int(uid or "50069134988124048")
                    })
            equipped_vanity = data.get("equipped_vanity", [
                {"id": 40008, "category": "Avatar"},
                {"id": 39999, "category": "Border"},
                {"id": 29999, "category": "Title"},])
            equipped_ids = {v["id"] for v in equipped_vanity}
            for v in VANITY_ITEMS:
                items.append({
                    "id": v["id"],
                    "item_definition": {
                        "id": v["id"],
                        "item_template": {
                            "id": v["id"],
                            "name": "",
                            "category": v["category"],
                            "consumable": False
                        },
                        "parameters": {}
                    },
                    "state": "Equipped" if v["id"] in equipped_ids else "New",
                    "user_id": int(uid or "50069134988124048")
                })

            self.send_json(self.collection_response(items))

        # ---- Card pack contents ----
        elif re.match(r"^/user(s)?/\d+/card_packs/\d+$", path):
            pack_id = path.split("/")[-1]
            packs = data.get("card_packs", {})
            pack = packs.get(str(pack_id), None)
            if pack:
                self.send_json({
                    "id": int(pack_id),
                    "non_choosable_card_definitions": [{"id": c, "is_hidden": False} for c in pack["non_choosable"]],
                    "choosable_card_definitions": [{"id": c, "is_hidden": False} for c in pack["choosable"]],
                    "date_created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                })
            else:
                non_ch, ch = generate_keg_cards()
                self.send_json({
                    "id": int(pack_id),
                    "non_choosable_card_definitions": [{"id": c, "is_hidden": False} for c in non_ch],
                    "choosable_card_definitions": [{"id": c, "is_hidden": False} for c in ch],
                    "date_created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                })
                
        # ---- Reward definitions (fetched once on login) ----
        # Serve the full GOG-server dump verbatim. RewardDefinitionsHolder
        # initialises from this once at login and the challenge-selection menu
        # reads the per-accomplishment payouts from it.
        # Matchmaking — poll ticket
        elif re.match(r"^/tickets/\d+$", path):
            ticket_id = int(path.split("/")[-1])
            with _mm_lock:
                ticket = _mm_tickets.get(ticket_id)
                lobby = _mm_lobbies.get(ticket["lobby_id"]) if ticket and ticket["lobby_id"] else None
            if ticket is None:
                self.send_response(404)
                self.end_headers()
                return
            # Find this ticket's access_key from the lobby users list
            access_key = None
            if lobby:
                for u in lobby.get("users", []):
                    if str(u["id"]) == str(ticket["user_id"]):
                        access_key = u["access_key"]
                        break
            resp = {
                "id": ticket["id"],
                "game_version": ticket["game_version"],
                "type": ticket["type"],
                "platform": ticket["platform"],
                "phase": ticket["phase"],
                "lobby_id": ticket["lobby_id"],
                "endpoint": lobby["endpoint"] if lobby else None,
                "access_key": access_key,
            }
            self.send_json(resp)
            return

        # Matchmaking — get lobby
        elif re.match(r"^/lobbies/\d+$", path):
            lobby_id = int(path.split("/")[-1])
            with _mm_lock:
                lobby = _mm_lobbies.get(lobby_id)
            if lobby is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_json(lobby)
            return

        # Friend invite — get invitation status
        elif re.match(r"^/invitations/\d+$", path):
            inv_id = int(path.split("/")[-1])
            auth = self.headers.get("Authorization", "")
            caller_uid = _extract_uid_from_jwt(auth)
            with _mm_lock:
                inv = _mm_invitations.get(inv_id)
                if inv is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                # Return caller-specific access_key from the lobby
                access_key = None
                if inv["lobby_id"]:
                    lobby = _mm_lobbies.get(inv["lobby_id"])
                    if lobby:
                        for u in lobby.get("users", []):
                            if str(u["id"]) == str(caller_uid):
                                access_key = u["access_key"]
                                break
                resp = {
                    "id": inv["id"],
                    "game_version": inv["game_version"],
                    "user_id": inv["user_id"],
                    "invited_user_id": inv["invited_user_id"],
                    "lobby_id": inv["lobby_id"],
                    "access_key": access_key,
                    "confirmation_owner": inv["confirmation_owner"],
                    "confirmation_invited": inv["confirmation_invited"],
                    "declined_by": inv["declined_by"],
                    "endpoint": inv.get("endpoint"),
                    "date_created": inv.get("date_created"),
                }
            print(f"[FriendInvite] GET /invitations/{inv_id} caller={caller_uid} -> access_key={access_key}")
            self.send_json(resp)
            return


        # Friends list (Galaxy SDK)
        elif re.match(r"^/users/\d+/friends", path):
            owner_id = int(path.split("/")[2])
            friends_map = _friends.get(owner_id, {})
            items = []
            for fid, status in friends_map.items():
                if status != 2:  # Only return accepted friends
                    continue
                # Look up username from users table
                user_info = _db.get_user_by_id(fid) if USE_SQLITE else None
                username = user_info["username"] if user_info else ""
                items.append({"user": {"id": str(fid), "username": username}})
            self.send_json({"items": items, "total_count": len(items)})
            return

        # Recent opponents (games-log)
        elif re.match(r"^/users/\d+/opponents", path):
            caller_uid = int(path.split("/")[2])
            opps = _recent_opponents.get(caller_uid, [])
            limit = 5
            qs = self.path.split("?")[1] if "?" in self.path else ""
            for p in qs.split("&"):
                if p.startswith("limit="):
                    try: limit = int(p.split("=")[1])
                    except: pass
            # Fill in missing usernames from DB
            filled = []
            for o in opps[:limit]:
                if not o.get("username") and USE_SQLITE:
                    row = _db.get_user_by_id(o["user_id"])
                    if row:
                        o = dict(o, username=row["username"])
                filled.append(o)
            self.send_json({"items": filled})
            return

        elif re.match(r"^/rewards$", path):
            self.send_json(load_rewards_dump())

        # ---- Debug: grant all cards ----
        elif re.match(r"^/debug/grant_all_cards$", path):
            pool = load_card_pool()
            from collections import Counter
            existing_counts = Counter(c["card_definition"]["id"] for c in data["cards"])
            new_cards = []

            def add_card(template_id, rarity, premium, copies=1):
                full_id = template_id * 100 + (1 if premium else 0)
                have = existing_counts[full_id]
                for _ in range(max(0, copies - have)):
                    new_id = data["next_card_id"]
                    data["next_card_id"] += 1
                    new_cards.append({
                        "id": new_id,
                        "card_definition": {
                            "id": full_id,
                            "card_template_id": template_id,
                            "rarity": rarity,
                            "premium": premium,
                            "is_deleted": False
                        },
                        "state": "New"
                    })
                    existing_counts[full_id] += 1

            for rarity, template_ids in pool.items():
                is_bronze = rarity in (1, 2)  # Common/Rare = bronze; Epic/Legendary = silver/gold
                copies_standard = 3 if is_bronze else 1
                copies_premium = 3 if is_bronze else 1

                for template_id in template_ids:
                    add_card(template_id, rarity, premium=False, copies=copies_standard)
                    add_card(template_id, rarity, premium=True,  copies=copies_premium)

            data["cards"].extend(new_cards)
            save_data(data, uid)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Granted {len(new_cards)} cards. Collection now has {len(data['cards'])} total.".encode())

        # ---- Remote config for GOGGalaxySDK ----
        elif re.match(r"^/components/GOGGalaxySDK/clients/\d+$", path):
            self.send_json({
                "version": "1.113.0",
                "content": {
                    "broker": {"ssl": False, "host": SERVER_IP, "port": 8445},
                    "broker_enabled": True,
                    "remote_configuration_enabled": False,
                    "logging_appenders": [],
                    "user_overrides": []
                },
                "bases": []
            })

        # ---- Remote config for GOGGalaxyCommunicationService ----
        elif re.match(r"^/components/GOGGalaxyCommunicationService/clients/\d+$", path):
            self.send_json({
                "version": "1.0.0",
                "content": {
                    "broker": {"ssl": False, "host": SERVER_IP, "port": 8445},
                    "broker_enabled": True,
                    "remote_configuration_enabled": False,
                    "logging_appenders": [],
                    "user_overrides": []
                },
                "bases": []
            })

        # --- Stub endpoints for Galaxy SDK social features ---
        # These are called during login; returning empty lists prevents errors.
        # (removed dead /friends stub — handled earlier in do_GET)
        elif re.match(r"^/users/\d+/invites", path):
            owner_id = int(path.split("/")[2])
            friends_map = _friends.get(owner_id, {})
            items = []
            for fid, status in friends_map.items():
                if status == 3:  # pending_received
                    items.append({"user_id": str(fid), "date_created": "2026-01-01T00:00:00Z"})
            self.send_json({"items": items})
        elif re.match(r"^/users/\d+/opponents", path):
            caller_uid = int(path.split("/")[2])
            opps = _recent_opponents.get(caller_uid, [])
            limit = 5
            qs = self.path.split("?")[1] if "?" in self.path else ""
            for p in qs.split("&"):
                if p.startswith("limit="):
                    try: limit = int(p.split("=")[1])
                    except: pass
            filled = []
            for o in opps[:limit]:
                if not o.get("username") and USE_SQLITE:
                    row = _db.get_user_by_id(o["user_id"])
                    if row:
                        o = dict(o, username=row["username"])
                filled.append(o)
            self.send_json({"items": filled})
        elif re.match(r"^/users/\d+/clients/\d+/properties", path):
            self.send_json({})

        # Game invitations: pending poll (mod uses this instead of Galaxy SDK)
        elif re.match(r"^/users/\d+/game_invitations/pending$", path):
            target_id = int(path.split("/")[2])
            queue = _game_invitations_pending.get(target_id, [])
            if queue:
                inv = queue.pop(0)
                print(f"[GameInvite] Delivered pending invite to {target_id}: {inv}")
                self.send_json(inv)
            else:
                self.send_json({})

        # Chat rooms messages (Galaxy SDK)
        elif re.match(r"^/rooms/", path) or re.match(r"^/users/\d+/rooms", path):
            self.send_json({"messages": [], "items": []})

        # Presence: GET /statuses?user_id=XXXX (presence.gog.com)
        elif path == "/statuses":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            user_ids = qs.get("user_id", [])
            items = []
            _now = __import__("time").time()
            for uid_str in user_ids:
                uid_int = int(uid_str)
                online = (_now - _last_seen.get(uid_int, 0)) < PRESENCE_ONLINE_WINDOW
                status = "online" if online else "offline"
                stored = _presence.get(uid_int)
                if online and stored:
                    metadata = stored.get("data", {}).get("metadata", '{"status":"online"}')
                else:
                    metadata = '{"status":"%s"}' % status
                items.append({
                    "user_id": uid_str,
                    "client_id": "46899977096215655",
                    "platform": "gog",
                    "status": status,
                    "data": {"metadata": metadata},
                })
            print(f"[Presence] /statuses {user_ids} -> {[i['status'] for i in items]}")
            self.send_json({"items": items})

        else:
            print(f"[UNHANDLED GET] {self.path}")
            self.send_response(404)
            self.end_headers()

    # --- POST ---
    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        # ---- User registration (handled before uid/data parsing) ----
        if path == "/register":
            try:
                body = json.loads(raw_body) if raw_body else {}
                username = body.get("username", "").strip()
                if not username:
                    self.send_json({"error": "username is required"}, status=400)
                    return
                # Check for duplicate username
                if USE_SQLITE:
                    if _db.get_user_by_username(username):
                        self.send_json({"error": "username already taken"}, status=409)
                        return
                else:
                    users = _load_users_json()
                    if any(u.get("username", "").lower() == username.lower() for u in users):
                        self.send_json({"error": "username already taken"}, status=409)
                        return
                # Generate a unique user ID (GOG-style large int)
                new_id = int(time.time() * 1000000) + random.randint(0, 999999)
                new_user = {
                    "id": new_id,
                    "username": username,
                    "created_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "avatar_image_id": ""
                }
                if USE_SQLITE:
                    _db.save_user(new_user)
                else:
                    users.append(new_user)
                    lock = _get_user_lock("users_json")
                    with lock:
                        with open(USERS_FILE, "w") as f:
                            json.dump(users, f, indent=2)
                # Create their data file with defaults
                data = load_data(str(new_id))
                # Full collection option: grant all cards + max currencies
                full_collection = body.get("full_collection", False)
                if full_collection:
                    pool = load_card_pool()
                    from collections import Counter
                    existing_counts = Counter(c["card_definition"]["id"] for c in data["cards"])
                    new_cards = []
                    def _add_card(template_id, rarity, premium, copies=1):
                        full_id = template_id * 100 + (1 if premium else 0)
                        have = existing_counts[full_id]
                        for _ in range(max(0, copies - have)):
                            cid = data["next_card_id"]
                            data["next_card_id"] += 1
                            new_cards.append({
                                "id": cid,
                                "card_definition": {
                                    "id": full_id,
                                    "card_template_id": template_id,
                                    "rarity": rarity,
                                    "premium": premium,
                                    "is_deleted": False
                                },
                                "state": "New"
                            })
                            existing_counts[full_id] += 1
                    for rarity, template_ids in pool.items():
                        is_bronze = rarity in (1, 2)
                        copies = 3 if is_bronze else 1
                        for template_id in template_ids:
                            _add_card(template_id, rarity, premium=False, copies=copies)
                            _add_card(template_id, rarity, premium=True, copies=copies)
                    data["cards"].extend(new_cards)
                    # Max currencies
                    for cur in data.get("currencies", []):
                        cur["amount"] = 99999
                    # Seed starter decks (idempotent: only when the account has
                    # none yet). Full-collection accounts get the prebuilt decks
                    # so they can jump straight into a match.
                    seeded = 0
                    if not data.get("decks"):
                        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                        decks = []
                        for d in STARTER_DECKS:
                            nd = json.loads(json.dumps(d))  # deep copy
                            nd["user_id"] = new_id
                            nd["date_created"] = now
                            nd["date_modified"] = now
                            decks.append(nd)
                        data["decks"] = decks
                        seeded = len(decks)
                    save_data(data, str(new_id))
                    print(f"[REGISTER] Granted {len(new_cards)} cards + max currencies + {seeded} starter decks to {username!r}")
                print(f"[REGISTER] New user: id={new_id} username={username!r} full_collection={full_collection}")
                self.send_json({
                    "id": new_id,
                    "username": username,
                    "refresh_token": f"privateserver_{new_id}",
                    "message": "Registration successful. Use this info to configure your client."
                }, status=201)
            except Exception as e:
                print(f"[REGISTER] Error: {e}")
                import traceback; traceback.print_exc()
                self.send_json({"error": str(e)}, status=500)
            return

        # ---- Username change ----
        if path == "/change_username":
            try:
                body = json.loads(raw_body) if raw_body else {}
                user_id = body.get("user_id")
                new_username = body.get("new_username", "").strip()
                if not user_id or not new_username:
                    self.send_json({"error": "user_id and new_username are required"}, status=400)
                    return
                # Check for duplicate username
                if USE_SQLITE:
                    existing = _db.get_user_by_username(new_username)
                    if existing and existing["id"] != user_id:
                        self.send_json({"error": "Username already taken"}, status=409)
                        return
                    user = _db.get_user_by_id(str(user_id))
                    if not user:
                        self.send_json({"error": "User not found"}, status=404)
                        return
                    user["username"] = new_username
                    _db.save_user(user)
                else:
                    users = _load_users_json()
                    if any(u.get("username", "").lower() == new_username.lower()
                           and u.get("id") != user_id for u in users):
                        self.send_json({"error": "Username already taken"}, status=409)
                        return
                    found = False
                    for u in users:
                        if u.get("id") == user_id:
                            u["username"] = new_username
                            found = True
                            break
                    if not found:
                        self.send_json({"error": "User not found"}, status=404)
                        return
                    lock = _get_user_lock("users_json")
                    with lock:
                        with open(USERS_FILE, "w") as f:
                            json.dump(users, f, indent=2)
                print(f"[USERNAME] Changed user {user_id} username to {new_username!r}")
                self.send_json({"username": new_username}, status=200)
            except Exception as e:
                print(f"[USERNAME] Error: {e}")
                import traceback; traceback.print_exc()
                self.send_json({"error": str(e)}, status=500)
            return

        # ---- Login (verify username + user_id) ----
        if path == "/login":
            try:
                body = json.loads(raw_body) if raw_body else {}
                username = body.get("username", "").strip()
                # user_id acts as a low-security shared secret so opponents (who
                # can see your username in-match) can't hijack the account.
                claimed_id = body.get("id")
                if not username or claimed_id in (None, ""):
                    self.send_json({"error": "username and user ID are required"}, status=400)
                    return
                try:
                    claimed_id = int(claimed_id)
                except (TypeError, ValueError):
                    self.send_json({"error": "user ID must be a number"}, status=400)
                    return
                if USE_SQLITE:
                    user = _db.get_user_by_username(username)
                else:
                    users = _load_users_json()
                    user = next((u for u in users
                                 if u.get("username", "").lower() == username.lower()), None)
                # Same 404 for "no such user" and "id mismatch" so the endpoint
                # doesn't reveal whether a username exists.
                if not user or int(user["id"]) != claimed_id:
                    self.send_json({"error": "Username and user ID do not match"}, status=404)
                    return
                print(f"[LOGIN] user={user['username']!r} id={user['id']}")
                self.send_json({
                    "id": user["id"],
                    "username": user["username"],
                    "refresh_token": f"privateserver_{user['id']}",
                    "message": "Login successful."
                }, status=200)
            except Exception as e:
                print(f"[LOGIN] Error: {e}")
                import traceback; traceback.print_exc()
                self.send_json({"error": str(e)}, status=500)
            return

        uid = _extract_user_id(path)
        data = load_data(uid)
        body = json.loads(raw_body) if raw_body else {}

        # Heartbeat (login/logout ping from seawolf-profile)
        if re.match(r"^/users/\d+/heartbeat$", path):
            self.send_json({})

        # Presence / rich-presence status (presence.gog.com/users/{id}/status).
        # The Galaxy SDK POSTs {"data":{"metadata":"{\"status\":\"Menus\"}"}}
        # to report 'now playing'. On the REAL presence server this 401s
        # harmlessly on Windows, but when our launcher redirects presence to
        # us we MUST return 2xx: a 401 here makes the SDK invalidate its access
        # token and enter a refresh storm that ends in 'not logged on' and a
        # mid-session sign-out on the Deck. Return an empty 200 (no body needed).
        elif re.match(r"^/users/\d+/status$", path):
            uid_status = int(path.split("/")[2])
            _presence[uid_status] = body if body else {"data": {"metadata": '{"status":"online"}'}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        # Craft card
        elif re.match(r"^/users/\d+/cards/craft$", path):
            card_def_id = body.get("card_definition_id", 0)
            is_premium = bool(body.get("is_premium", False))
            if not is_premium:
                is_premium = str(card_def_id).endswith("2")
            rarity = body.get("rarity", 1)
            apply_currency_modifications(body.get("expected_currency_modification", []), uid)
            new_id = data["next_card_id"]
            data["next_card_id"] += 1
            new_card = {
                "id": new_id,
                "card_definition": {
                    "id": card_def_id, "card_template_id": card_def_id,
                    "rarity": rarity, "premium": is_premium, "is_deleted": False
                },
                "state": "New"
            }
            data["cards"].append(new_card)
            save_data(data, uid)
            self.send_json({"user_currency": {"id": 2, "name": "Scraps"}, "user_card": new_card})

        # Mill single card
        elif re.match(r"^/users/\d+/cards/\d+/mill$", path):
            card_id = int(path.split("/")[-2])
            milled_card = next((c for c in data["cards"] if c["id"] == card_id), None)
            is_premium = milled_card and milled_card["card_definition"].get("premium", False)
            rarity = milled_card["card_definition"].get("rarity", 1) if milled_card else 1
            scraps_profit = {1: 10, 2: 20, 4: 50, 8: 200}
            powder_profit = {1: 5, 2: 10, 4: 25, 8: 50}
            apply_currency_modifications([{"id": 2, "amount": scraps_profit.get(rarity, 10)}], uid)
            if is_premium:
                apply_currency_modifications([{"id": 3, "amount": powder_profit.get(rarity, 10)}], uid)
            data["cards"] = [c for c in data["cards"] if c["id"] != card_id]
            save_data(data, uid)
            self.send_json({"user_currency": {"currency": {"id": 2, "name": "Scraps"},
                                               "amount": get_currency(data, 2)["amount"], "expiration": []}})

        # Mill spares
        elif re.match(r"^/users/\d+/cards/mill$", path):
            ids_to_mill = [item.get("id") for item in body.get("items", [])]
            scraps_profit = {1: 10, 2: 20, 4: 50, 8: 200}
            powder_profit = {1: 10, 2: 20, 4: 50, 8: 200}
            for card in data["cards"]:
                if card["id"] in ids_to_mill:
                    rarity = card["card_definition"].get("rarity", 1)
                    is_premium = card["card_definition"].get("premium", False)
                    apply_currency_modifications([{"id": 2, "amount": scraps_profit.get(rarity, 10)}], uid)
                    if is_premium:
                        apply_currency_modifications([{"id": 3, "amount": powder_profit.get(rarity, 10)}], uid)
            data["cards"] = [c for c in data["cards"] if c["id"] not in ids_to_mill]
            save_data(data, uid)
            self.send_json({"user_currency": {"currency": {"id": 2, "name": "Scraps"},
                                               "amount": get_currency(data, 2)["amount"], "expiration": []}})

        # Upgrade card to premium
        elif re.match(r"^/users/\d+/cards/\d+/upgrade$", path):
            card_id = int(path.split("/")[-2])
            apply_currency_modifications(body.get("expected_currency_modification", []), uid)
            for card in data["cards"]:
                if card["id"] == card_id:
                    card["card_definition"]["premium"] = True
                    old_id = card["card_definition"]["id"]
                    base_id = (old_id // 100) * 100
                    card["card_definition"]["id"] = base_id + 1
                    save_data(data, uid)
                    self.send_json(card)
                    return
            self.send_response(404)
            self.end_headers()

        # Save deck
        elif re.match(r"^/users/\d+/decks$", path):
            deck = body
            deck["id"] = len(data["decks"]) + 1
            deck["date_created"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            deck["date_modified"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            if deck.get("is_current"):
                for other in data["decks"]:
                    if other.get("is_current"):
                        other["is_current"] = False
            data["decks"].append(deck)
            save_data(data, uid)
            self.send_json(deck)

        # Purchase transaction
        elif re.match(r"^/users/\d+/transactions$", path):
            self.handle_transaction(body, uid)

        # Deliver purchased items
        elif re.match(r"^/users/\d+/transactions/\d+/delivery$", path):
            self.send_json({})

        # Consume item (open keg)
        elif re.match(r"^/users/\d+/items/\d+/consumption$", path):
            pack_id = int(time.time() * 1000)
            non_choosable, choosable = generate_keg_cards()
            if "card_packs" not in data:
                data["card_packs"] = {}
            data["card_packs"][str(pack_id)] = {
                "non_choosable": non_choosable,
                "choosable": choosable
            }
            inventory = data.get("inventory", {})
            if "1" in inventory:
                inventory["1"] = max(0, inventory["1"] - 1)
                if inventory["1"] == 0:
                    del inventory["1"]
            save_data(data, uid)
            self.send_json({"user_card_pack_id": pack_id})
        
        #Save accomplishments/quest rewards and progression
        elif re.match(r"^/users/\d+/accomplishments$", path):
            acc_type   = body.get("type", "")
            rounds_won = int(body.get("rounds_won", body.get("wins_count", 1)) or 1)
            print(f"[ACCOMPLISHMENT] type={acc_type!r} body={body}")
            granted = {"currencies": [], "items": [], "cards": [], "currency_totals": {}}
            if acc_type:
                accomplishments = data.setdefault("accomplishments", [])
                # Only grant the reward the FIRST time the player completes the
                # accomplishment — repeat completions don't pay out again.
                if not any(a.get("type") == acc_type for a in accomplishments):
                    new_acc = {
                        "id": str(int(time.time() * 1000)),
                        "type": acc_type
                    }
                    accomplishments.append(new_acc)
                    granted = grant_reward(data, acc_type, rounds_won=rounds_won)
                    save_data(data, uid)
                else:
                    new_acc = next(a for a in data["accomplishments"] if a.get("type") == acc_type)
                    # On repeat: still award XP + crowns and fire notifications so the
                    # outcome screen completes.
                    maybe_reset_daily(data)
                    xp_before = data.get("experience", 0)
                    data["experience"] = xp_before + compute_match_xp(True, rounds_won)
                    level_before, cells_before = compute_level_and_cells(xp_before)
                    level_after,  cells_after  = compute_level_and_cells(data["experience"])
                    data["level"]        = level_after
                    data["filled_cells"] = cells_after
                    crowns_before = data.get("crown_pieces_today", 0)
                    crowns_to_add = min(rounds_won, get_crown_cap(crowns_before) - crowns_before)
                    if crowns_to_add > 0:
                        data["crown_pieces_today"] = crowns_before + crowns_to_add
                    data["wins_today"] = data.get("wins_today", 0) + 1
                    save_data(data, uid)
                    granted = {
                        "currencies":    [],
                        "items":         [],
                        "cards":         [],
                        "currency_totals": {str(c["currency"]["id"]): c["amount"]
                                            for c in data.get("currencies", [])},
                        "xp_before":     xp_within_level(xp_before),
                        "xp_after":      xp_within_level(data["experience"]),
                        "level_before":  level_before,
                        "level_after":   level_after,
                        "cells_before":  cells_before,
                        "cells_after":   cells_after,
                        "crowns_before": crowns_before,
                        "crowns_after":  data["crown_pieces_today"],
                        "wins_today":    data["wins_today"],
                    }
                # Trigger broker to push reward notifications
                try:
                    import socket as _socket
                    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                    s.connect(("127.0.0.1", 8446))
                    trigger = json.dumps({
                        "acc_type":        acc_type,
                        "target_user_id":  str(uid) if uid else None,
                        "currencies":      granted["currencies"],
                        "items":           granted["items"],
                        "cards":           granted["cards"],
                        "currency_totals": granted["currency_totals"],
                        "xp_before":       granted.get("xp_before", 0),
                        "xp_after":        granted.get("xp_after", 0),
                        "level_before":    granted.get("level_before", 1),
                        "level_after":     granted.get("level_after", 1),
                        "cells_before":    granted.get("cells_before", 0),
                        "cells_after":     granted.get("cells_after", 0),
                        "crowns_before":   granted.get("crowns_before", 0),
                        "crowns_after":    granted.get("crowns_after", 0),
                        "wins_today":      granted.get("wins_today", 1),
                    })
                    header = "POST / HTTP/1.1\r\nContent-Length: " + str(len(trigger)) + "\r\n\r\n"
                    s.sendall(header.encode() + trigger.encode())
                    s.recv(64)
                    s.close()
                except Exception as e:
                    print(f"[BROKER TRIGGER] Failed: {e}")
            self.send_json(new_acc if acc_type else {})

        #Save favourite faction
        elif re.match(r"^/users/\d+/factions/favourite$", path):
            faction_fav = body.get("faction")
            if faction_fav:
                data["favourite_faction"] = faction_fav
                save_data(data, uid)
            self.send_json({})
        
        #Save favourite card        
        elif re.match(r"^/users/\d+/cards/favourite$", path):
            card_def_id = body.get("card_definition_id")
            if card_def_id:
                data["favourite_card_id"] = card_def_id
                save_data(data, uid)
            self.send_json({})  

        # Choose card from card pack
        elif re.match(r"^/users/\d+/card_packs/\d+/choice$", path):
            pack_id = path.split("/")[-2]
            chosen_index = body.get("chosen_card_index", 0)
            packs = data.get("card_packs", {})
            pack = packs.get(str(pack_id), {})
            non_choosable = pack.get("non_choosable", [])
            choosable = pack.get("choosable", [])

            all_full_ids = list(non_choosable)
            if choosable and 0 <= chosen_index < len(choosable):
                all_full_ids.append(choosable[chosen_index])

            new_cards = []
            pool = load_card_pool()
            for full_id in all_full_ids:
                template_id = full_id // 100
                is_premium = (full_id % 100) == 1
                rarity = 1
                for r in [8, 4, 2, 1]:
                    if template_id in pool.get(r, []):
                        rarity = r
                        break
                new_id = data["next_card_id"]
                data["next_card_id"] += 1
                new_card = {
                    "id": new_id,
                    "card_definition": {
                        "id": full_id, "card_template_id": template_id,
                        "rarity": rarity, "premium": is_premium, "is_deleted": False
                    },
                    "state": "New"
                }
                data["cards"].append(new_card)
                new_cards.append(new_card)

            if str(pack_id) in packs:
                del packs[str(pack_id)]
            save_data(data, uid)
            self.send_json(self.collection_response(new_cards))

        # Matchmaking — create ticket
        elif re.match(r"^/tickets$", path):
            global _mm_next_id
            # Extract the caller's user ID from the auth header if present,
            # fallback to a counter-based ID
            auth = self.headers.get("Authorization", "")
            with _mm_lock:
                ticket_id = _mm_next_id
                _mm_next_id += 1
                ticket = {
                    "id": ticket_id,
                    "game_version": body.get("game_version", ""),
                    "type": body.get("type", "quick"),
                    "platform": body.get("platform", "GOG"),
                    "phase": 0,
                    "lobby_id": None,
                    "user_id": ticket_id,  # unique per-ticket user identifier
                }
                _mm_tickets[ticket_id] = ticket

                # Try to pair with an existing waiting ticket
                waiting = [t for t in _mm_tickets.values()
                           if t["id"] != ticket_id and t["phase"] == 0]
                if waiting:
                    other = waiting[0]
                    lobby_id = _mm_next_id
                    _mm_next_id += 1

                    # First ticket = HOST (listens); second ticket = CLIENT (connects)
                    host_key = "host-key-{}".format(other["id"])
                    client_key = "client-key-{}".format(ticket_id)

                    lobby = {
                        "id": lobby_id,
                        "endpoint": "ws://{}:{}".format(MM_HOST_IP, MM_GAME_PORT),
                        "state": "assigned",
                        "game_version": ticket["game_version"],
                        "users": [
                            {"id": str(other["user_id"]), "access_key": host_key,
                             "ip": MM_HOST_IP, "platform": "GOG"},
                            {"id": str(ticket["user_id"]), "access_key": client_key,
                             "ip": MM_HOST_IP, "platform": "GOG"},
                        ]
                    }
                    _mm_lobbies[lobby_id] = lobby

                    other["phase"] = 1
                    other["lobby_id"] = lobby_id
                    ticket["phase"] = 1
                    ticket["lobby_id"] = lobby_id

            resp = {
                "id": ticket["id"],
                "game_version": ticket["game_version"],
                "type": ticket["type"],
                "platform": ticket["platform"],
                "phase": ticket["phase"],
                "lobby_id": ticket["lobby_id"],
            }
            self.send_json(resp, status=201)

        # Friend invite — create invitation
        elif re.match(r"^/invitations$", path):
            auth = self.headers.get("Authorization", "")
            caller_uid = _extract_uid_from_jwt(auth)
            with _mm_lock:
                inv_id = _mm_next_id
                _mm_next_id += 1
                inv = {
                    "id": inv_id,
                    "game_version": body.get("game_version", ""),
                    "user_id": caller_uid,
                    "invited_user_id": body.get("invited_user_id"),
                    "lobby_id": None,
                    "access_key": None,
                    "confirmation_owner": False,
                    "confirmation_invited": False,
                    "declined_by": [],
                    "endpoint": None,
                    "date_created": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                }
                _mm_invitations[inv_id] = inv
            resp = dict(inv)
            print(f"[FriendInvite] Created invitation {inv_id} by user {caller_uid}")
            self.send_json(resp, status=201)

        # Friend invite — accept invitation
        elif re.match(r"^/invitations/\d+/acceptance$", path):
            inv_id = int(path.split("/")[-2])
            auth = self.headers.get("Authorization", "")
            caller_uid = _extract_uid_from_jwt(auth)
            with _mm_lock:
                inv = _mm_invitations.get(inv_id)
                if inv is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                inv["invited_user_id"] = caller_uid
            print(f"[FriendInvite] Invitation {inv_id} accepted by user {caller_uid}")
            self.send_json(inv)

        # Friend invite — confirm invitation (both sides call this)
        elif re.match(r"^/invitations/\d+/confirmation$", path):
            inv_id = int(path.split("/")[-2])
            auth = self.headers.get("Authorization", "")
            caller_uid = _extract_uid_from_jwt(auth)
            with _mm_lock:
                inv = _mm_invitations.get(inv_id)
                if inv is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if caller_uid == inv["user_id"]:
                    inv["confirmation_owner"] = True
                else:
                    inv["confirmation_invited"] = True
                # Once both confirmed, create lobby
                if inv["confirmation_owner"] and inv["confirmation_invited"] and inv["lobby_id"] is None:
                    lobby_id = _mm_next_id
                    _mm_next_id += 1
                    host_key = "host-key-{}".format(inv["user_id"])
                    client_key = "client-key-{}".format(inv["invited_user_id"])
                    lobby = {
                        "id": lobby_id,
                        "endpoint": "ws://{}:{}".format(MM_HOST_IP, MM_GAME_PORT),
                        "state": "assigned",
                        "game_version": inv["game_version"],
                        "users": [
                            {"id": str(inv["user_id"]), "access_key": host_key, "ip": MM_HOST_IP, "platform": "GOG"},
                            {"id": str(inv["invited_user_id"]), "access_key": client_key, "ip": MM_HOST_IP, "platform": "GOG"},
                        ]
                    }
                    _mm_lobbies[lobby_id] = lobby
                    inv["lobby_id"] = lobby_id
                    inv["endpoint"] = lobby["endpoint"]
                    inv["access_key"] = host_key  # owner gets host key; invited gets client key on GET
                    print(f"[FriendInvite] Both confirmed inv {inv_id}, lobby {lobby_id} created")
            self.send_response(200)
            self.end_headers()

        # Friend invite — decline invitation
        elif re.match(r"^/invitations/\d+/declination$", path):
            inv_id = int(path.split("/")[-2])
            auth = self.headers.get("Authorization", "")
            caller_uid = _extract_uid_from_jwt(auth)
            with _mm_lock:
                inv = _mm_invitations.get(inv_id)
                if inv is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if inv["declined_by"] is None:
                    inv["declined_by"] = []
                inv["declined_by"].append(caller_uid)
            print(f"[FriendInvite] Invitation {inv_id} declined by user {caller_uid}")
            self.send_response(200)
            self.end_headers()

        elif re.match(r"^/games/\d+/gg$", path):
            # POST /games/{game_id}/gg
            # Body: {"username": "<sender>"} (GwentWebServiceClient.DTO.GoodGame)
            # Grant 5 gold to the local (opponent) account and push a good_game
            # broker notification so the receiving client shows the slide toast.
            m_gg  = re.match(r"^/games/(\d+)/gg$", path)
            gg_game_id   = int(m_gg.group(1)) if m_gg else 0
            length       = int(self.headers.get("Content-Length", 0))
            gg_body      = body
            sender_name  = gg_body.get("username", "Opponent")
            # +5 gold for the opponent — in multi-user, we don't know which
            # user is the recipient from this path alone; skip gold grant for now.
            # TODO: pass recipient user_id in the GG body from the game client
            data = load_data(uid)
            for c in data.setdefault("currencies", []):
                if c["currency"]["id"] == 1:
                    c["amount"] = c.get("amount", 0) + 5
                    break
            save_data(data, uid)
            print(f"[GG] game_id={gg_game_id} sender={sender_name!r} +5 gold granted")

            # Push good_game notification to broker.
            # MatchRewardManager.ValidateNotification: no context.tag, but
            # notification.Type == good_game -> routes to GoodGame RewardSource.
            # The notification payload must have sender.id and sender.username so
            # GoodGameSender deserialises correctly.
            gg_key = (gg_game_id, sender_name)
            if gg_key not in _gg_sent_games:
                _gg_sent_games.add(gg_key)
                try:
                    import socket as _socket
                    # Look up sender's user_id to exclude them from receiving their own GG
                    sender_uid = None
                    for u in _load_users_json():
                        if u.get("username") == sender_name:
                            sender_uid = str(u["id"])
                            break
                    notif = json.dumps({
                        "type":    "good_game",
                        "context": {"game_id": gg_game_id},
                        "payload": {
                            "sender": {
                                "id":       sender_name,
                                "username": sender_name,
                            }
                        },
                        "exclude_user_id": sender_uid,
                    })
                    hdr = "POST / HTTP/1.1\r\nContent-Length: " + str(len(notif)) + "\r\n\r\n"
                    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                    s.settimeout(2)
                    s.connect(("127.0.0.1", 8446))
                    s.sendall(hdr.encode() + notif.encode())
                    s.recv(64)
                    s.close()
                except Exception as e:
                    print(f"[GG] Broker push failed: {e}")
            else:
                print(f"[GG] game_id={gg_game_id} sender={sender_name!r} already sent, skipping")

            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # Game invitations (mod bypasses Galaxy SDK; sender_id in body)
        elif re.match(r"^/users/\d+/game_invitations$", path):
            target_id = int(path.split("/")[2])
            # Mod sends sender_id in body; fall back to JWT sub if absent
            sender_id = int(body.get("sender_id", 0)) if body.get("sender_id") else 0
            if not sender_id:
                auth = self.headers.get("Authorization", "")
                import base64 as _b64, json as _json
                try:
                    payload_b64 = auth.split(".")[1]
                    payload_b64 += "=" * (-len(payload_b64) % 4)
                    sender_id = int(_json.loads(_b64.b64decode(payload_b64))["sub"])
                except Exception:
                    sender_id = 0
            sender_info = _db.get_user_by_id(sender_id) if USE_SQLITE and sender_id else None
            sender_name = sender_info["username"] if sender_info else ""
            connection_string = str(body.get("connection_string", body.get("connectionString", "")))
            _game_invitations_pending.setdefault(target_id, []).append({
                "inv_id": connection_string,
                "sender_id": str(sender_id),
                "sender_name": sender_name,
            })
            print(f"[GameInvite] POST /users/{target_id}/game_invitations from {sender_id} ({sender_name}) conn={connection_string}")
            self.send_json({"id": str(target_id) + "_" + str(sender_id), "status": "sent"})

        # Chat rooms (Galaxy SDK - chat.gog.com)
        elif re.match(r"^/users/\d+/rooms", path):
            owner_id = int(path.split("/")[2])
            participants = body.get("participants", [])
            all_ids = sorted([str(owner_id)] + [str(p) for p in participants])
            # Room ID must be a numeric ulong for OnChatRoomWithUserRetrieveSuccess.
            # XOR of sorted participant IDs is deterministic, symmetric, and fits in 56 bits.
            numeric_ids = sorted([owner_id] + [int(p) for p in participants])
            room_id = numeric_ids[0] ^ numeric_ids[1] if len(numeric_ids) >= 2 else numeric_ids[0]
            print(f"[Chat] POST /users/{owner_id}/rooms participants={participants} -> room_id={room_id}")
            participant_objs = []
            for uid in ([owner_id] + [int(p) for p in participants]):
                user_info = _db.get_user_by_id(uid) if USE_SQLITE else None
                uname = user_info["username"] if user_info else ""
                participant_objs.append({"user": {"id": str(uid), "username": uname}})
            self.send_json({
                "id": room_id,
                "name": "",
                "participants": participant_objs,
                "messages": [],
                "total_count": 0,
            })

        elif re.match(r"^/users/\d+/friends/\d+", path):
            # SDK sends POST to accept/respond to friend invitations
            parts = path.split("/")
            owner_id = int(parts[2])
            friend_id = int(parts[4])
            body = json.loads(raw_body) if raw_body else {}
            req_status = body.get("status", 3)
            print(f"[Friends] POST /users/{owner_id}/friends/{friend_id} status={req_status}")
            if req_status == 3:
                # Accept: mark both sides as friends (status=2)
                _friends.setdefault(owner_id, {})[friend_id] = 2
                _friends.setdefault(friend_id, {})[owner_id] = 2
                _db.accept_friend(owner_id, friend_id)
                _bump_friends_rev(owner_id, friend_id)
                print(f"[Friends] Accepted: {owner_id} <-> {friend_id} are now friends")
            else:
                _friends.setdefault(owner_id, {})[friend_id] = req_status
                if USE_SQLITE:
                    _db.set_friend(owner_id, friend_id, req_status)
                _bump_friends_rev(owner_id, friend_id)
            self.send_json({"status": "ok"})

        else:
            print(f"[UNHANDLED POST] {self.path}")
            self.send_response(404)
            self.end_headers()

    # --- PUT ---
    def do_PUT(self):
        path = self.path.split("?")[0]
        uid = _extract_user_id(path)
        data = load_data(uid)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if re.match(r"^/users/\d+/decks/\d+$", path):
            deck_id = int(path.split("/")[-1])
            for i, d in enumerate(data["decks"]):
                if d["id"] == deck_id:
                    if "faction" in body:
                        body["id"] = deck_id
                        body["date_modified"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                        data["decks"][i] = body
                        # is_current must be exclusive: selecting a deck clears the
                        # flag on all others, or /internal/user_battle_deck always
                        # returns the first starter deck for everyone.
                        if body.get("is_current"):
                            for j, other in enumerate(data["decks"]):
                                if j != i and other.get("is_current"):
                                    other["is_current"] = False
                        save_data(data, uid)
                        self.send_json(body)
                        return
                    else:
                        self.send_json({})
                        return
            self.send_response(404)
            self.end_headers()
            
        elif re.match(r"^/users/\d+/cards$", path):
            updates = {item["user_card_id"]: item["state"] for item in body.get("items", [])}
            for card in data["cards"]:
                if card["id"] in updates:
                    card["state"] = updates[card["id"]]
            save_data(data, uid)
            self.send_json({})   
            
        elif re.match(r"^/users/\d+/items/\d+$", path):
            item_id = int(path.split("/")[-1])
            new_state = body.get("state", "")
            
            if new_state == "Equipped":
                # Find which category this item belongs to
                category = next((v["category"] for v in VANITY_ITEMS if v["id"] == item_id), None)
                if category:
                    # Load current equipped_vanity, defaulting to the three defaults
                    equipped_vanity = data.get("equipped_vanity", [
                        {"id": 40008, "category": "Avatar"},
                        {"id": 39999, "category": "Border"},
                        {"id": 29999, "category": "Title"},
                    ])
                    # Replace the entry for this category
                    equipped_vanity = [v for v in equipped_vanity if v["category"] != category]
                    equipped_vanity.append({"id": item_id, "category": category})
                    data["equipped_vanity"] = equipped_vanity
                    save_data(data, uid)

            # Return the item with updated state
            self.send_json({
                "id": item_id,
                "item_definition": {
                    "id": item_id,
                    "item_template": {
                        "id": item_id,
                        "name": "",
                        "category": category if new_state == "Equipped" else "",
                        "consumable": False
                    },
                    "parameters": {}
                },
                "state": new_state,
                "user_id": str(uid or "50069134988124048")
            })
        elif re.match(r"^/transactions$", path):
            params = self.path.split("?")[1] if "?" in self.path else ""
            if "transaction_id=" in params:
                self.send_json({})
            else:
                self.send_json({"items": [], "total_count": 0})
        elif re.match(r"^/users/\d+/friends/\d+$", path):
            parts = path.split("/")
            owner_id = int(parts[2])
            friend_id = int(parts[4])
            status = body.get("status", 1)
            # Record the friend request/acceptance
            existing = _friends.get(owner_id, {}).get(friend_id)
            if status == 1 and _friends.get(friend_id, {}).get(owner_id) == 2:
                # The other side already considers us a friend (asymmetric row
                # from older data) — complete the friendship instead of leaving
                # this side stuck pending forever.
                _friends.setdefault(owner_id, {})[friend_id] = 2
                _db.accept_friend(owner_id, friend_id)
                _bump_friends_rev(owner_id, friend_id)
                print(f"[Friends] Healed asymmetric pair on invite: {owner_id} <-> {friend_id} now friends")
                self.send_json({"user_id": owner_id, "user_id_2": friend_id, "status": 2})
                return
            if status == 1 and existing == 3:
                # Don't overwrite pending_received with sent — they already invited us
                print(f"[Friends] Keeping pending_received for {owner_id}<-{friend_id} (they already invited us)")
            else:
                _friends.setdefault(owner_id, {})[friend_id] = status
                _db.set_friend(owner_id, friend_id, status)
            # If sending a request (status=1), mark as pending on the other side
            if status == 1:
                other_existing = _friends.get(friend_id, {}).get(owner_id)
                if other_existing not in (2, 3):  # Don't overwrite accepted or pending_received
                    _friends.setdefault(friend_id, {})[owner_id] = 3
                    _db.set_friend(friend_id, owner_id, 3)
            # If accepting (status=2), mark both as friends
            elif status == 2:
                _friends.setdefault(friend_id, {})[owner_id] = 2
                _db.accept_friend(owner_id, friend_id)
            _bump_friends_rev(owner_id, friend_id)
            print(f"[Friends] PUT /users/{owner_id}/friends/{friend_id} status={status}")
            # Push broker notification to recipient
            if status == 1:
                # Friend request sent — notify recipient via friends topic
                sender_info = _db.get_user_by_id(owner_id) if USE_SQLITE else None
                sender_name = sender_info["username"] if sender_info else ""
                try:
                    import socket as _socket
                    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                    s.connect(("127.0.0.1", 8446))
                    trigger = json.dumps({
                        "type": "friend_invite",
                        "target_user_id": str(friend_id),
                        "notification": {
                            "type": "friend_invitation_received",
                            "sender_id": str(owner_id),
                            "sender_username": sender_name,
                        }
                    })
                    header = "POST / HTTP/1.1\r\nContent-Length: " + str(len(trigger)) + "\r\n\r\n"
                    s.sendall(header.encode() + trigger.encode())
                    s.recv(128)
                    s.close()
                    print(f"[Friends] Triggered broker notification for friend invite to {friend_id}")
                except Exception as e:
                    print(f"[Friends] Failed to trigger broker: {e}")
            self.send_json({"user_id": owner_id, "user_id_2": friend_id, "status": status})
        else:
            print(f"[UNHANDLED PUT] {self.path}")
            self.send_response(404)
            self.end_headers()
    # --- DELETE ---
    def do_DELETE(self):
        path = self.path.split("?")[0]
        uid = _extract_user_id(path)
        data = load_data(uid)
        if re.match(r"^/tickets/\d+$", path):
            ticket_id = int(path.split("/")[-1])
            with _mm_lock:
                _mm_tickets.pop(ticket_id, None)
            self.send_response(200)
            self.end_headers()
        elif re.match(r"^/invitations/\d+$", path):
            inv_id = int(path.split("/")[-1])
            with _mm_lock:
                _mm_invitations.pop(inv_id, None)
            self.send_response(200)
            self.end_headers()
        elif re.match(r"^/users/\d+/decks/\d+$", path):
            deck_id = int(path.split("/")[-1])
            data["decks"] = [d for d in data["decks"] if d["id"] != deck_id]
            save_data(data, uid)
            self.send_response(200)
            self.end_headers()
        elif re.match(r"^/users/\d+/friends/\d+$", path):
            parts = path.split("/")
            owner_id = int(parts[2])
            friend_id = int(parts[4])
            _friends.get(owner_id, {}).pop(friend_id, None)
            _friends.get(friend_id, {}).pop(owner_id, None)
            _bump_friends_rev(owner_id, friend_id)
            _db.delete_friend(owner_id, friend_id)
            print(f"[Friends] DELETE /users/{owner_id}/friends/{friend_id}")
            self.send_response(200)
            self.end_headers()
        else:
            print(f"[UNHANDLED DELETE] {self.path}")
            self.send_response(404)
            self.end_headers()


# ── Internal game-finish handler (plain HTTP on port 8444, called by relay.py) ──
class InternalHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress access log

    # ── Faction normalization for BattleDeck JSON ──
    FACTION_ALIASES = {
        "northernkingdom": "NorthernRealms", "northernkingdoms": "NorthernRealms",
        "northern_kingdom": "NorthernRealms", "northern_kingdoms": "NorthernRealms",
        "northernrealm": "NorthernRealms", "northernrealms": "NorthernRealms",
        "neutral": "Neutral", "monsters": "Monsters", "monster": "Monsters",
        "nilfgaard": "Nilfgaard", "scoiatael": "Scoiatael", "skellige": "Skellige",
    }

    def do_GET(self):
        m = re.match(r"^/internal/user_battle_deck/(\d+)$", self.path)
        if m:
            uid = m.group(1)
            try:
                data = load_data(uid)
                card_map = {c["id"]: c["card_definition"] for c in data.get("cards", []) if c.get("card_definition")}
                decks = data.get("decks", [])
                if not decks:
                    self._send_json(404, {"error": "no decks"})
                    return
                # Prefer the most recently modified is_current deck — legacy data
                # has is_current=True on all five starter decks, so first-match
                # would always return deck 1 ("Mulligan Elves") for everyone.
                cur = [d for d in decks if d.get("is_current")]
                if cur:
                    current_deck = max(cur, key=lambda d: d.get("date_modified") or d.get("date_created") or "")
                else:
                    current_deck = decks[0]
                user_cards = current_deck.get("user_cards", [])
                leader = None
                cards = []
                for i, uc in enumerate(user_cards):
                    cd = card_map.get(uc["id"])
                    if not cd:
                        continue
                    entry = {"TemplateId": cd["card_template_id"], "Premium": bool(cd.get("premium", False))}
                    if i == 0:
                        leader = entry
                    else:
                        cards.append(entry)
                if leader is None:
                    self._send_json(404, {"error": "no leader"})
                    return
                raw_faction = current_deck.get("faction", "Neutral")
                key = raw_faction.replace(" ", "").replace("-", "").lower()
                faction = self.FACTION_ALIASES.get(key, raw_faction)
                battle_deck = {"FactionId": faction, "Leader": leader, "Cards": cards, "Name": current_deck.get("name", "Deck")}
                self._send_json(200, battle_deck)
            except Exception as e:
                print(f"[INTERNAL] user_battle_deck error: {e}")
                self._send_json(500, {"error": str(e)})
        elif re.match(r"^/internal/friends_rev/\d+$", self.path):
            uid = int(self.path.split("/")[-1])
            self._send_json(200, {"rev": str(_friends_rev.get(uid, 0))})
        elif re.match(r"^/internal/game_invitations/pending/\d+$", self.path):
            target_id = int(self.path.split("/")[-1])
            queue = _game_invitations_pending.get(target_id, [])
            # Piggyback the friends revision so the mod's poll loop needs only
            # this one request per cycle (values are strings for the mod's
            # Dictionary<string,string> deserializer).
            fmap = _friends.get(target_id, {})
            rev = {
                "friends_rev": str(_friends_rev.get(target_id, 0)),
                # Authoritative state for the mod's contact-list sync (comma-joined
                # so the payload stays a flat Dictionary<string,string> for the mod):
                # friends_list = accepted friends; pending_list = incoming requests.
                "friends_list": ",".join(str(fid) for fid, st in fmap.items() if st == 2),
                "pending_list": ",".join(str(fid) for fid, st in fmap.items() if st == 3),
            }
            _last_seen[target_id] = __import__("time").time()
            if queue:
                inv = dict(queue.pop(0))
                inv.update(rev)
                print(f"[GameInvite] Delivered pending invite to {target_id}: {inv}")
                self._send_json(200, inv)
            else:
                self._send_json(200, rev)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, code, obj):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if re.match(r"^/internal/game_invitations/\d+$", self.path):
            target_id = int(self.path.split("/")[-1])
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            sender_id = int(body.get("sender_id", 0) or 0)
            sender_info = _db.get_user_by_id(sender_id) if USE_SQLITE and sender_id else None
            sender_name = sender_info["username"] if sender_info else str(sender_id)
            connection_string = str(body.get("connection_string", ""))
            _game_invitations_pending.setdefault(target_id, []).append({
                "inv_id": connection_string,
                "sender_id": str(sender_id),
                "sender_name": sender_name,
            })
            print(f"[GameInvite] Internal POST /internal/game_invitations/{target_id} from {sender_id} ({sender_name}) conn={connection_string}")
            self._send_json(200, {"status": "sent"})
        elif self.path == "/internal/game_finish":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length)) if length else {}
            # rounds_won: how many rounds THIS player won (0, 1, or 2).
            # won: whether this player won the match. Default derives from
            # rounds_won (>=2 means match win) for backward compatibility with
            # callers that only send rounds_won.
            rounds_won = int(body.get("rounds_won", 1) or 0)
            won        = bool(body.get("won", rounds_won >= 2))
            game_id    = int(body.get("game_id", 0) or 0)
            uid        = body.get("user_id")  # Optional; relay can pass this
            data = load_data(uid)
            maybe_reset_daily(data)

            # Award XP: base (win/loss) + per-round performance bonus.
            xp_before = data.get("experience", 0)
            data["experience"] = xp_before + compute_match_xp(won, rounds_won)
            level_before, cells_before = compute_level_and_cells(xp_before)
            level_after,  cells_after  = compute_level_and_cells(data["experience"])
            data["level"]        = level_after
            data["filled_cells"] = cells_after

            # Award crown pieces = rounds actually won this match (0/1/2),
            # capped at the daily cap. A loser who won one round still gets 1.
            crowns_before = data.get("crown_pieces_today", 0)
            crowns_to_add = min(rounds_won, get_crown_cap(crowns_before) - crowns_before)
            if crowns_to_add > 0:
                data["crown_pieces_today"] = crowns_before + crowns_to_add
            # "win of the day" only counts actual match wins.
            if won:
                data["wins_today"] = data.get("wins_today", 0) + 1
            crowns_after = data["crown_pieces_today"]
            wins_today   = data["wins_today"]

            # Award level-up rewards for every level crossed this match.
            # grant_level_up_reward() mutates `data` in place for currencies/items/cards.
            lu_currencies = []
            lu_items      = []
            lu_cards      = []
            if level_after > level_before:
                for lvl in range(level_before + 1, level_after + 1):
                    lu = grant_level_up_reward(data, lvl)
                    lu_currencies.extend(lu.get("currencies", []))
                    lu_items.extend(lu.get("items", []))
                    lu_cards.extend(lu.get("cards", []))
                if lu_currencies or lu_items or lu_cards:
                    print(f"[GAME FINISH] level-up rewards levels "
                          f"{level_before+1}-{level_after}: "
                          f"currencies={lu_currencies} items={lu_items} cards={lu_cards}")

            save_data(data, uid)

            # Record opponent pairing for recent-opponents list
            if game_id and uid:
                _game_finish_pairs.setdefault(game_id, [])
                _uname = data.get("username", "")
                if not _uname and USE_SQLITE:
                    _urow = _db.get_user_by_id(int(uid))
                    if _urow:
                        _uname = _urow["username"]
                entry = {"user_id": int(uid), "won": won, "username": _uname}
                _game_finish_pairs[game_id].append(entry)
                # When both players have reported, record each as the other's opponent
                pair = _game_finish_pairs[game_id]
                if len(pair) >= 2:
                    for i, me in enumerate(pair):
                        for j, them in enumerate(pair):
                            if i == j:
                                continue
                            opp_entry = {
                                "user_id": them["user_id"],
                                "username": them["username"],
                                "platform": "GOG",
                                "player_result": "win" if me["won"] else "lose",
                            }
                            opps = _recent_opponents.setdefault(me["user_id"], [])
                            # Prepend (most recent first), dedupe, cap at 20
                            opps = [o for o in opps if o["user_id"] != them["user_id"]]
                            opps.insert(0, opp_entry)
                            _recent_opponents[me["user_id"]] = opps[:20]
                    del _game_finish_pairs[game_id]

            # Send WITHIN-LEVEL XP (NOT lifetime totals). The client tracks XP
            # within the current level (0..band width) and resets on level-up.
            # experience_change.from = within-level XP of the from-level,
            # experience_change.to   = within-level XP of the to-level. The
            # client's ExperienceToDistribute() then equals exactly the XP gained
            # (verified for no-level-up AND level-up). Sending lifetime totals here
            # made the level-up branch ((next-from)+to) overshoot by ~a full level,
            # which is why the player gained far too much XP per match.
            xp_wl_before = xp_within_level(xp_before)
            xp_wl_after  = xp_within_level(data["experience"])

            print(f"[GAME FINISH] xp {xp_before}->{data['experience']} "
                  f"(within-level {xp_wl_before}->{xp_wl_after}) "
                  f"level {level_before}->{level_after} "
                  f"crowns {crowns_before}->{crowns_after} "
                  f"wins_today={wins_today}")

            # Trigger broker
            try:
                import socket as _socket
                s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                s.connect(("127.0.0.1", 8446))
                trigger = json.dumps({
                    "game_id":         game_id,
                    "target_user_id":  str(uid) if uid else None,
                    "currencies":      lu_currencies,
                    "items":           lu_items,
                    "cards":           lu_cards,
                    "currency_totals": {str(c["currency"]["id"]): c["amount"]
                                        for c in data.get("currencies", [])},
                    "xp_before":       xp_wl_before,
                    "xp_after":        xp_wl_after,
                    "level_before":    level_before,
                    "level_after":     level_after,
                    "cells_before":    cells_before,
                    "cells_after":     cells_after,
                    "crowns_before":   crowns_before,
                    "crowns_after":    crowns_after,
                    "wins_today":      wins_today,
                })
                hdr = "POST / HTTP/1.1\r\nContent-Length: " + str(len(trigger)) + "\r\n\r\n"
                s.sendall(hdr.encode() + trigger.encode())
                s.recv(64)
                s.close()
            except Exception as e:
                print(f"[GAME FINISH] Broker trigger failed: {e}")

            body_resp = b"OK"
            self.send_response(200)
            self.send_header("Content-Length", len(body_resp))
            self.end_headers()
            self.wfile.write(body_resp)
        else:
            self.send_response(404)
            self.end_headers()


import threading as _threading

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

server = ThreadedHTTPServer(("0.0.0.0", 8443), GwentHandler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
_cert_dir = os.environ.get(
    "GWENT_CERT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "Nginx", "conf"),
)
context.load_cert_chain(
    os.path.join(_cert_dir, "fake.crt"),
    os.path.join(_cert_dir, "fake.key")
)
server.socket = context.wrap_socket(server.socket, server_hostname="seawolf-deck.gog.com")

class _ThreadedInternalServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

internal_server = _ThreadedInternalServer(("127.0.0.1", 8444), InternalHandler)
_threading.Thread(target=internal_server.serve_forever, daemon=True).start()
print("[SERVER] Internal game-finish listener on 127.0.0.1:8444")
internal_server2 = _ThreadedInternalServer(("0.0.0.0", 8447), InternalHandler)
_threading.Thread(target=internal_server2.serve_forever, daemon=True).start()
print("[SERVER] Internal game-invite listener on 127.0.0.1:8447")

print("Gwent server running on port 8443")
server.serve_forever()
