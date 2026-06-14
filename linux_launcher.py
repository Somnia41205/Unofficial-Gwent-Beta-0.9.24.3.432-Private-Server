#!/usr/bin/env python3
"""
Gwent Beta Private Server -- Linux / Steam Deck Launcher

A Tk-based launcher that mirrors the Windows GwentBetaLauncher.exe flow but
runs the Windows Gwent client under Wine on Linux (Steam Deck in
particular).

Architecture:
  * commservice on 127.0.0.1:9977         (daemon thread, in-process)
  * dns_proxy   on 127.0.0.1:53           (daemon thread, in-process)
  * https_proxy on 127.0.0.1:443          (daemon thread, in-process)
  * broker_proxy on 127.0.0.1:8445        (TCP passthrough -> remote :8445)
  * relay_proxy  on 127.0.0.1:7777        (TCP passthrough -> remote :7777)
  * fake.crt installed inside the WINEPREFIX only (less intrusive than the
    host CA store, and survives SteamOS atomic updates)
  * Mod DLL is bundled and copied to <gwent>/Mods/ on every launch
    (matches Windows launcher behaviour)

Root is required to bind ports 53 and 443. On Steam Deck the launcher
re-executes itself with `pkexec` when not root.
"""

import atexit
import ctypes  # noqa: F401  (kept for symmetry with launcher.py; not used here)
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import urllib.request
import urllib.error
import zipfile

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "1.0.0-linux"


def _resolve_server_host():
    """Server host/IP, resolved at runtime so it is never hardcoded in source.

    Resolution order (first hit wins):
      1. GWENT_SERVER_HOST environment variable.
      2. A bundled `server_host.txt` (added at build time from your private
         server.txt; see build_launcher_linux.sh).
      3. `127.0.0.1` (localhost fallback for local testing).
    """
    env = os.environ.get("GWENT_SERVER_HOST", "").strip()
    if env:
        return env
    bundle = getattr(sys, "_MEIPASS",
                     os.path.dirname(os.path.abspath(sys.argv[0])))
    for cand in (os.path.join(bundle, "server_host.txt"),
                 os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                              "server_host.txt")):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                host = f.read().strip().splitlines()[0].strip()
            if host and not host.startswith("#"):
                return host
        except Exception:
            pass
    return "127.0.0.1"


SERVER_IP = _resolve_server_host()
SERVER_URL = f"https://{SERVER_IP}"


def _resolve_to_ip(host):
    """Resolve `host` to a literal IP string ONCE, at launch, while normal DNS
    is still in effect (before resolv.conf is repointed at 127.0.0.1).

    The local proxies forward to the server by whatever value is in `server_ip`
    (the bundled DuckDNS default, the user's own hostname, or a raw IP). Once the
    launcher repoints resolv.conf/hosts at our DNS proxy -- which only
    special-cases *.gog.com and forwards everything else upstream -- a proxy that
    still holds a *hostname* must resolve it through our own redirect on every
    forward. A single hiccup there returns `[Errno 11001] getaddrinfo failed` ->
    502 and collapses the users.gog.com burst. Resolving to an IP here removes
    that self-inflicted dependency; https_proxy.py still sends the original Host
    header so nginx routing is unaffected.

    Handles all input shapes: a literal IP returns unchanged (no lookup); a
    hostname is resolved once; localhost/empty return unchanged. On ANY failure
    it returns `host` unchanged, so behaviour is no worse than before.
    """
    import ipaddress
    h = (host or "").strip()
    if not h:
        return host
    try:
        ipaddress.ip_address(h)   # already a literal IP -> no DNS needed
        return h
    except ValueError:
        pass
    try:
        return socket.gethostbyname(h)
    except Exception:
        return host   # fall back to the hostname; no regression vs. prior behaviour

MELONLOADER_URL = (
    "https://github.com/LavaGang/MelonLoader/releases/"
    "download/v0.5.7/MelonLoader.x64.zip"
)
# Placeholder -> bundled DLL is the only delivery path (matches Windows launcher).
GITHUB_MOD_RELEASE_URL = ""
GITHUB_MOD_DLL_NAME = "GwentBetaRestorationMod.dll"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.argv[0])))
EXE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))


def clean_subprocess_env(base=None):
    """Return an environment safe for spawning external programs (flatpak,
    wine, system tools) from inside the PyInstaller-frozen launcher.

    PyInstaller injects its own bundled libssl/libcrypto into LD_LIBRARY_PATH
    so the frozen exe can run. But that path leaks into every child process,
    so when we exec `flatpak` (and its libcurl) the child loads PyInstaller's
    OLDER OpenSSL and dies with:
        libssl.so.3: version `OPENSSL_3.x.0' not found (required by libcurl)
    which made every `sc create`/`sc start` fail (rc=1) and the dummy
    GalaxyCommunication service never registered.

    PyInstaller stashes the user's original value in *_ORIG; restore it (or
    drop the var entirely) and strip any remaining _MEI bundle dir."""
    env = dict(base if base is not None else os.environ)
    for var in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        orig = env.get(var + "_ORIG")
        if orig is not None:
            if orig:
                env[var] = orig
            else:
                env.pop(var, None)
        else:
            # No _ORIG saved: strip the PyInstaller bundle dir from the value.
            mei = getattr(sys, "_MEIPASS", None)
            if mei and env.get(var):
                parts = [x for x in env[var].split(os.pathsep)
                         if x and mei not in x and "/_MEI" not in x]
                if parts:
                    env[var] = os.pathsep.join(parts)
                else:
                    env.pop(var, None)
    return env


# Persist data under XDG_DATA_HOME so it survives reboot on Deck.
XDG_DATA = os.environ.get(
    "XDG_DATA_HOME",
    os.path.join(os.path.expanduser("~"), ".local", "share"),
)
DATA_DIR = os.path.join(XDG_DATA, "gwent-beta-launcher")

CONFIG_FILE = os.path.join(DATA_DIR, "gwent_launcher.json")
USERS_JSON = os.path.join(DATA_DIR, "users.json")
LOG_FILE = os.path.join(DATA_DIR, "launcher_debug.log")

# Default WINEPREFIX -- isolated so we don't pollute the user's other prefixes.
DEFAULT_PREFIX = os.path.join(DATA_DIR, "wineprefix")

# Bundled assets
CERT_FILE = os.path.join(BUNDLE_DIR, "fake.crt")
KEY_FILE = os.path.join(BUNDLE_DIR, "fake.key")
MOD_DLL = os.path.join(BUNDLE_DIR, "GwentBetaRestorationMod.dll")
GALAXY_COMM_EXE = os.path.join(BUNDLE_DIR, "GalaxyCommunication.exe")

# Where the dummy service lives inside the WINEPREFIX (matches GOG layout)
SERVICE_REL_DIR = os.path.join("drive_c", "ProgramData", "GOG.com",
                               "Galaxy", "redists")
SERVICE_NAME = "GalaxyCommunication"
COMMSERVICE_PY = os.path.join(BUNDLE_DIR, "commservice.py")
DNS_PROXY_PY = os.path.join(BUNDLE_DIR, "dns_proxy.py")
HTTPS_PROXY_PY = os.path.join(BUNDLE_DIR, "https_proxy.py")
SETTINGS_CONFIG = os.path.join(BUNDLE_DIR, "settings", "config.json")
SETTINGS_LAUNCH = os.path.join(BUNDLE_DIR, "settings", "Launch.cfg")

# Where the launcher will install Gwent if the user picks "auto-download".
DEFAULT_GWENT_DIR = os.path.join(
    os.path.expanduser("~"),
    "Games", "Gwent The Witcher Card Game",
)

# Candidate paths to scan for an existing Windows Gwent install under Wine.
GWENT_SEARCH_PATHS = [
    DEFAULT_GWENT_DIR,
    os.path.join(os.path.expanduser("~"), "Gwent The Witcher Card Game"),
    os.path.join(os.path.expanduser("~"), "GOG Games", "Gwent The Witcher Card Game"),
    # Common Lutris / bottles locations
    os.path.join(os.path.expanduser("~"), ".local", "share", "lutris",
                 "runners", "gwent"),
]

# Resolv.conf / systemd-resolved handling
RESOLV_CONF = "/etc/resolv.conf"
RESOLV_BACKUP = os.path.join(DATA_DIR, "resolv.conf.gwent_backup")

# /etc/hosts fallback (needed on Steam Deck where NetworkManager/ConnMan keep
# rewriting resolv.conf or where systemd-resolved intercepts 127.0.0.53).
HOSTS_PATH = "/etc/hosts"
HOSTS_BACKUP = os.path.join(DATA_DIR, "hosts.gwent_backup")
HOSTS_MARKER_BEGIN = "# >>> gwent-beta-launcher BEGIN <<<"
HOSTS_MARKER_END   = "# >>> gwent-beta-launcher END <<<"
GOG_HOSTS = [
    "gwent-quests.gog.com",
    "presence.gog.com",
    "seawolf-config.gog.com",
    "seawolf-deck.gog.com",
    "seawolf-inventory.gog.com",
    "seawolf-shop.gog.com",
    "seawolf-rankings.gog.com",
    "seawolf-profile.gog.com",
    "seawolf-rewards.gog.com",
    "seawolf-matchmaking.gog.com",
    "seawolf-games-log.gog.com",
    "remote-config.gog.com",
    "notifications-pusher.gog.com",
    "users.gog.com",
    "auth.gog.com",
]

# CIDR ranges GOG's services live in. DNAT'ing the WHOLE range to 127.0.0.1
# is the IP-layer equivalent of the Windows launcher's blanket *.gog.com
# redirect: it catches EVERY gog host (incl. ones we never enumerated, e.g.
# gwent-quests) and every rotated Fastly anycast IP, regardless of how the
# game's wine/Mono resolver picks an IP. Per-host /etc/hosts + dns_proxy only
# help clients that honor them; the game under wine sometimes resolves to a
# real gog IP directly (curl/getent honor hosts and hit 127.0.0.1, but the
# game timed out -> it bypassed hosts). The range DNAT is the only layer that
# can't be bypassed. 151.101.0.0/16 = Fastly (auth/seawolf-*/users/remote-
# config/gwent-quests/...); 91.222.185.0/24 = GOG (notifications-pusher).
# Installed only during a session and removed on cleanup.
GOG_IP_RANGES = [
    "151.101.0.0/16",
    "91.222.185.0/24",
]

# Public DNS to use when resolving the REAL gog.com IPs (so we know which
# IPs to DNAT-redirect to localhost). Must NOT be 127.0.0.* so we bypass
# our own redirects.
PUBLIC_DNS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]

# Module-level mirror of the IPs/CIDRs we DNAT this session. The GUI tracks
# them on self._iptables_ips, but the atexit/signal emergency handler is
# module-level and cannot see the instance, so install_iptables_dnat also
# records here and _emergency_cleanup flushes from here.
_install_session_ips = set()

# ---------------------------------------------------------------------------
# Theme (same palette as the Windows launcher)
# ---------------------------------------------------------------------------
BG_DARK = "#1a1a2e"
BG_CARD = "#16213e"
BG_INPUT = "#0f3460"
FG_TEXT = "#e0e0e0"
FG_DIM = "#8888aa"
FG_TITLE = "#e8d5a3"
ACCENT = "#c9a84c"
ACCENT_HOVER = "#dfc06a"
BTN_BG = "#c9a84c"
BTN_FG = "#1a1a2e"
ERROR_FG = "#ff6b6b"
SUCCESS_FG = "#51cf66"
PROGRESS_BG = "#0f3460"
PROGRESS_FG = "#c9a84c"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def log(msg):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def is_root():
    return os.geteuid() == 0


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def ssl_context():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def load_config():
    ensure_data_dir()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg):
    ensure_data_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def validate_gwent_install(path):
    if not path:
        return False
    return (
        os.path.isfile(os.path.join(path, "Gwent.exe"))
        and os.path.isfile(os.path.join(path, "Gwent_Data", "Managed",
                                        "Assembly-CSharp.dll"))
    )


def find_gwent():
    for p in GWENT_SEARCH_PATHS:
        if validate_gwent_install(p):
            return p
    # Also scan Steam library compatdata dirs for an "installed Non-Steam" copy.
    for steam_dir in steam_library_paths():
        common = os.path.join(steam_dir, "steamapps", "common")
        if os.path.isdir(common):
            for name in os.listdir(common):
                if "gwent" in name.lower():
                    cand = os.path.join(common, name)
                    if validate_gwent_install(cand):
                        return cand
    return None


def is_melonloader_installed(gwent_path):
    return (
        os.path.isdir(os.path.join(gwent_path, "MelonLoader"))
        and os.path.isfile(os.path.join(gwent_path, "version.dll"))
    )


# ---------------------------------------------------------------------------
# Steam / Wine detection
# ---------------------------------------------------------------------------
STEAM_ROOT_CANDIDATES = [
    os.path.expanduser("~/.steam/steam"),
    os.path.expanduser("~/.steam/root"),
    os.path.expanduser("~/.local/share/Steam"),
    "/home/deck/.steam/steam",
    "/home/deck/.local/share/Steam",
]


def steam_root():
    for p in STEAM_ROOT_CANDIDATES:
        if os.path.isdir(p):
            return p
    return None


def steam_library_paths():
    """Return all Steam library folders (main + extra drives)."""
    libs = []
    root = steam_root()
    if not root:
        return libs
    libs.append(root)
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            with open(vdf) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('"path"'):
                        # crude vdf parse: "path"   "/run/media/sdcard/SteamLibrary"
                        parts = line.split('"')
                        if len(parts) >= 4:
                            p = parts[3]
                            if os.path.isdir(p) and p not in libs:
                                libs.append(p)
        except Exception:
            pass
    return libs


FLATPAK_WINE_ID = "org.winehq.Wine"


def find_flatpak_wine():
    """Return the flatpak app id if org.winehq.Wine is installed via Flatpak,
    else None. Flatpak wine is NOT on PATH (it is invoked as
    `flatpak run org.winehq.Wine`), so shutil.which("wine") never finds it.
    This is the common case on a Steam Deck where pacman is locked down but
    `flatpak install flathub org.winehq.Wine` works.

    On the Deck the launcher may run in a stripped Steam/Gamescope session
    whose env points `flatpak` at the wrong installation, so a plain
    `flatpak info` can report "not installed" even when a system install
    exists. We therefore probe info, both installation scopes explicitly,
    AND the on-disk export directories, and log raw output so failures are
    diagnosable from launcher_debug.log alone."""
    if not shutil.which("flatpak"):
        return None

    def _probe(args):
        try:
            r = subprocess.run(["flatpak"] + args, capture_output=True,
                               text=True, timeout=8,
                               env=clean_subprocess_env())
            return r.returncode, (r.stdout or ""), (r.stderr or "")
        except Exception as e:
            return -1, "", str(e)

    # NOTE: do NOT use `flatpak info <id>` to detect installation. Once we have
    # run `remote-ls flathub` (which the auto-installer does), the local remote
    # summary is populated and `flatpak info org.winehq.Wine` can return rc=0
    # for an app that is merely KNOWN to the remote but NOT installed -- a
    # false positive that made the launcher skip install and download the game.
    # `flatpak list` only ever shows actually-installed apps, so it is the
    # authoritative check.

    # 1./2. List across scopes; only installed apps appear here.
    for args in (["list", "--app", "--columns=application"],
                 ["list", "--system", "--app", "--columns=application"],
                 ["list", "--user", "--app", "--columns=application"]):
        rc, out, err = _probe(args)
        if FLATPAK_WINE_ID in out:
            log(f"find_flatpak_wine: found via `flatpak {' '.join(args)}`")
            return FLATPAK_WINE_ID

    # 3. On-disk fallback -- only for envs where `flatpak list/info` can lie
    # (stripped Steam/Gamescope session). IMPORTANT: a bare app dir is NOT
    # proof of a working install -- a FAILED/aborted install leaves an empty
    # or partial `app/org.winehq.Wine/` directory behind, which previously
    # caused a FALSE POSITIVE (launcher thought wine was installed when
    # `flatpak list` showed nothing). So we require an actually-deployed
    # build: app/<id>/<arch>/<branch>/active/files/bin/wine must exist.
    base_dirs = [
        "/var/lib/flatpak",
        os.path.expanduser("~/.local/share/flatpak"),
        "/home/deck/.local/share/flatpak",
    ]
    # Include the desktop user's home when we are root (HOME is root's).
    try:
        if is_root():
            import pwd
            base_dirs.append(os.path.join(
                pwd.getpwnam(target_user()).pw_dir, ".local", "share",
                "flatpak"))
    except Exception:
        pass
    import glob as _glob
    for base in base_dirs:
        app_root = os.path.join(base, "app", FLATPAK_WINE_ID)
        if not os.path.isdir(app_root):
            continue
        # Look for a real deployed wine binary under any arch/branch/active.
        hits = _glob.glob(os.path.join(
            app_root, "*", "*", "active", "files", "bin", "wine"))
        hits += _glob.glob(os.path.join(
            app_root, "*", "*", "active", "files", "bin", "wine64"))
        if hits:
            log(f"find_flatpak_wine: found deployed binary at {hits[0]}")
            return FLATPAK_WINE_ID
        log(f"find_flatpak_wine: {app_root} exists but no deployed wine "
            f"binary (stale/partial install) -- ignoring")

    log("find_flatpak_wine: org.winehq.Wine NOT detected by any method")
    return None


def target_user():
    """The non-root desktop user we should run Flatpak as.

    The launcher runs as root (for iptables/hosts), but Flatpak wine is
    installed in the desktop user's per-user Flatpak install
    (~/.local/share/flatpak), and bwrap cannot bind-mount that user's runtime
    extensions while running as root -- hence:
        bwrap: Can't find source path .../flatpak/runtime/...: Permission denied
    So we must drop back to the original user for `flatpak run`. pkexec/sudo
    preserved USER/HOME of the invoking user, so prefer those; fall back to
    SUDO_USER, then the owner of HOME, then 'deck'."""
    for var in ("SUDO_USER", "PKEXEC_UID"):
        v = os.environ.get(var)
        if var == "PKEXEC_UID" and v:
            try:
                import pwd
                return pwd.getpwuid(int(v)).pw_name
            except Exception:
                pass
        elif v and v != "root":
            return v
    u = os.environ.get("USER")
    if u and u != "root":
        return u
    home = os.environ.get("HOME", "")
    if home.startswith("/home/"):
        return home.split("/")[2]
    return "deck"


def user_runtime_env(user):
    """XDG_RUNTIME_DIR / DBUS / display vars for the target desktop user, so
    `flatpak run` (executed via sudo -u) finds the session bus and Wayland
    socket instead of root's (nonexistent) ones."""
    try:
        import pwd
        uid = pwd.getpwnam(user).pw_uid
    except Exception:
        uid = 1000
    xrd = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    out = {
        "XDG_RUNTIME_DIR": xrd,
        "DBUS_SESSION_BUS_ADDRESS": os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path={xrd}/bus"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", ""),
        "XAUTHORITY": os.environ.get("XAUTHORITY", ""),
    }
    return {k: v for k, v in out.items() if v}


_FLATPAK_PERMS_FIXED = False


def fix_flatpak_runtime_perms():
    """Earlier (buggy) builds ran `flatpak run` as ROOT, which created
    root-owned lock/instance files under the desktop user's
    /run/user/<uid>/.flatpak and ~/.local/share/flatpak/.changed etc. Now that
    we correctly run flatpak as the desktop user, that user can no longer lock
    those root-owned files:
        error: Unable to lock /run/user/<uid>/.flatpak/<app>/.ref: Permission denied
    Since we are root here, hand ownership of the runtime flatpak dirs back to
    the desktop user (and clear stale instance locks). Idempotent; root only."""
    global _FLATPAK_PERMS_FIXED
    if _FLATPAK_PERMS_FIXED or not is_root():
        return
    _FLATPAK_PERMS_FIXED = True
    user = target_user()
    try:
        import pwd
        pw = pwd.getpwnam(user)
        uid, gid = pw.pw_uid, pw.pw_gid
        home = pw.pw_dir
    except Exception as e:
        log(f"fix_flatpak_runtime_perms: cannot resolve user {user}: {e}")
        return
    targets = [
        f"/run/user/{uid}/.flatpak",
        os.path.join(home, ".local", "share", "flatpak"),
        os.path.join(home, ".cache", "flatpak"),
        os.path.join(home, ".var", "app", FLATPAK_WINE_ID),
        os.path.join(home, ".var", "app"),
    ]
    for base in targets:
        if not os.path.exists(base):
            continue
        fixed = 0
        for root, dirs, files in os.walk(base):
            for name in dirs + files:
                fp = os.path.join(root, name)
                try:
                    st = os.lstat(fp)
                    if st.st_uid == 0:            # only touch root-owned leftovers
                        os.lchown(fp, uid, gid)
                        fixed += 1
                except Exception:
                    pass
        try:
            if os.lstat(base).st_uid == 0:
                os.lchown(base, uid, gid)
                fixed += 1
        except Exception:
            pass
        if fixed:
            log(f"fix_flatpak_runtime_perms: reassigned {fixed} root-owned "
                f"entries under {base} to {user}")
    # Clear stale per-app instance locks under the runtime dir.
    inst = f"/run/user/{uid}/.flatpak"
    if os.path.isdir(inst):
        for sub in os.listdir(inst):
            ref = os.path.join(inst, sub, ".ref")
            if os.path.exists(ref):
                try:
                    st = os.lstat(ref)
                    if st.st_uid != uid:
                        os.remove(ref)
                        log(f"fix_flatpak_runtime_perms: removed stale lock {ref}")
                except Exception:
                    pass


def fix_prefix_owner(prefix_dir):
    """When the launcher runs as root but wine runs as the desktop user
    (flatpak-wine), the WINEPREFIX must be owned by that user, or wine aborts:
        wine: '<prefix>' is not owned by you
    Earlier root-context builds created the prefix root-owned. Chown the whole
    prefix tree back to the target user. Root only; idempotent (skips if
    already correctly owned)."""
    if not is_root() or not prefix_dir or not os.path.isdir(prefix_dir):
        return
    try:
        import pwd
        pw = pwd.getpwnam(target_user())
        uid, gid = pw.pw_uid, pw.pw_gid
    except Exception as e:
        log(f"fix_prefix_owner: cannot resolve user: {e}")
        return
    try:
        if os.stat(prefix_dir).st_uid == uid:
            return  # already owned by the desktop user
    except Exception:
        pass
    fixed = 0
    for root, dirs, files in os.walk(prefix_dir):
        for n in dirs + files:
            try:
                os.lchown(os.path.join(root, n), uid, gid)
                fixed += 1
            except Exception:
                pass
    try:
        os.lchown(prefix_dir, uid, gid)
        fixed += 1
    except Exception:
        pass
    log(f"fix_prefix_owner: chowned {fixed} entries in {prefix_dir} "
        f"to {target_user()}")


def fix_game_file_owner(gwent_path):
    """The launcher runs as ROOT and downloads/extracts the game and writes
    Launch.cfg / config.json / the mod DLL, so the ENTIRE game tree ends up
    root-owned. wine runs as the desktop user (sudo -u) and then cannot:
      - read root-owned config (UnauthorizedAccessException on Launch.cfg ->
        infinite splash), or
      - CREATE new files/dirs in root-owned folders. In particular MelonLoader
        tries to make `<game>/MelonLoader/Logs` (and `<game>/Logs`) at startup
        and crashes with "couldn't create logs folder" when the game dir is
        not user-writable.
    So chown the WHOLE game directory to the desktop user and make dirs
    user-writable. Root only; idempotent (skips files already owned)."""
    if not is_root() or not gwent_path or not os.path.isdir(gwent_path):
        return
    try:
        import pwd
        pw = pwd.getpwnam(target_user())
        uid, gid = pw.pw_uid, pw.pw_gid
    except Exception as e:
        log(f"fix_game_file_owner: cannot resolve user: {e}")
        return
    fixed = 0
    # Chown the game root itself first, then everything under it.
    for base in (gwent_path,):
        try:
            if os.lstat(base).st_uid != uid:
                os.lchown(base, uid, gid)
                fixed += 1
            # Ensure the root dir is user-writable so MelonLoader can create
            # its Logs/ subfolders.
            os.chmod(base, 0o755)
        except Exception:
            pass
        for root, dirs, files in os.walk(base):
            for n in dirs:
                dp = os.path.join(root, n)
                try:
                    if os.lstat(dp).st_uid != uid:
                        os.lchown(dp, uid, gid)
                        fixed += 1
                    if not os.path.islink(dp):
                        # rwx for owner so the user can create files inside.
                        os.chmod(dp, 0o755)
                except Exception:
                    pass
            for n in files:
                fp = os.path.join(root, n)
                try:
                    if os.lstat(fp).st_uid != uid:
                        os.lchown(fp, uid, gid)
                        fixed += 1
                except Exception:
                    pass
    if fixed:
        log(f"fix_game_file_owner: chowned {fixed} game paths to "
            f"{target_user()}")


def flatpak_wine_cmd(env):
    """Build the `flatpak run ...` prefix that invokes wine inside the Flatpak
    sandbox while (a) giving it access to the host filesystem -- so WINEPREFIX
    and the game files outside the sandbox resolve -- and (b) forwarding the
    wine-related env vars, since flatpak does NOT inherit the parent
    environment. CRITICAL: --filesystem=host plus sharing /etc keeps our
    /etc/hosts and /etc/resolv.conf redirects visible to wine; without this
    the flatpak sandbox would hide them exactly like a Proton-style sandbox
    would, and gog.com lookups would escape to the real internet."""
    fp = ["flatpak", "run",
          "--filesystem=host",
          "--share=network",
          "--device=all",
          "--socket=x11", "--socket=wayland", "--socket=pulseaudio"]
    # Forward the env vars wine needs across the sandbox boundary.
    for key in ("WINEPREFIX", "WINEDEBUG", "WINEDLLOVERRIDES", "DISPLAY",
                "WAYLAND_DISPLAY", "XAUTHORITY", "PULSE_SERVER"):
        if env.get(key):
            fp.append("--env=%s=%s" % (key, env[key]))
    fp.append(FLATPAK_WINE_ID)

    # Flatpak must run as the desktop user (not root). When we are root, wrap
    # the whole flatpak invocation in `sudo -u <user>` with the user's session
    # env, otherwise bwrap fails to bind the user's per-user runtime.
    if is_root():
        user = target_user()
        ue = user_runtime_env(user)
        sudo = ["sudo", "-u", user]
        for k, v in ue.items():
            sudo.append("%s=%s" % (k, v))
        # `sudo VAR=val cmd` sets those in the child env (env_reset default).
        # Reorder: sudo -u user env VAR=val ... flatpak ...
        sudo = ["sudo", "-u", user, "env"] + ["%s=%s" % (k, v)
                                              for k, v in ue.items()]
        log(f"flatpak_wine_cmd: dropping to user '{user}' for flatpak run")
        return sudo + fp
    return fp


def ensure_flatpak_installed(status_cb=None):
    """If `flatpak` is not on PATH, try to install it via the system package
    manager. We already run as root, so this is feasible. Best-effort and
    per-distro; returns True if flatpak is available afterwards.

    Distros vary: Fedora/Mint/Pop ship flatpak; stock Ubuntu/Debian/Arch do
    not. We detect the package manager and run the matching install. Each
    branch is logged so a failure on a bare machine is diagnosable from
    launcher_debug.log alone. This is a no-op when flatpak already exists
    (the common case, e.g. Linux Mint / SteamOS), so it cannot regress the
    already-working path."""
    if shutil.which("flatpak"):
        return True
    if not is_root():
        log("ensure_flatpak_installed: flatpak missing and not root -- "
            "cannot auto-install")
        return False
    if status_cb:
        status_cb("Installing Flatpak (one-time)...")

    # (binary-to-detect, command-to-run). Ordered; first matching pm wins.
    managers = [
        ("apt-get", ["apt-get", "install", "-y", "flatpak"]),
        ("dnf",     ["dnf", "install", "-y", "flatpak"]),
        ("yum",     ["yum", "install", "-y", "flatpak"]),
        ("pacman",  ["pacman", "-S", "--noconfirm", "flatpak"]),
        ("zypper",  ["zypper", "--non-interactive", "install", "flatpak"]),
    ]
    for binname, cmd in managers:
        if not shutil.which(binname):
            continue
        # apt needs an update first to populate the package lists.
        if binname == "apt-get":
            try:
                u = subprocess.run(["apt-get", "update"], capture_output=True,
                                   text=True, timeout=180,
                                   env=clean_subprocess_env())
                log(f"ensure_flatpak_installed: apt-get update rc={u.returncode}")
            except Exception as e:
                log(f"ensure_flatpak_installed: apt-get update error: {e}")
        try:
            log("ensure_flatpak_installed: " + " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=600, env=clean_subprocess_env())
            log(f"ensure_flatpak_installed: {binname} rc={r.returncode} "
                f"{(r.stderr or '')[-200:].strip()}")
            if shutil.which("flatpak"):
                return True
        except Exception as e:
            log(f"ensure_flatpak_installed: {binname} error: {e}")

    log("ensure_flatpak_installed: could not install flatpak via any known "
        "package manager")
    return shutil.which("flatpak") is not None


def install_flatpak_wine(status_cb=None):
    """Attempt to install org.winehq.Wine from Flathub via Flatpak.
    Returns (ok, message).

    Bugs this version fixes (each cost a debugging round on Linux Mint):
      1. The launcher re-executes itself as ROOT (for iptables/hosts). Running
         `flatpak ... --user` as root writes root-owned files into the desktop
         user's ~/.local/share/flatpak, CORRUPTING that repo so every later
         --user call dies with "Permission denied" on repo/tmp/cache. We must
         drop to the desktop user for ALL --user operations (same trick as
         flatpak_wine_cmd). --system operations correctly stay as root.
      2. Mint adds the flathub remote with a `verified_floss` subset filter;
         Wine isn't in it, so install says "Nothing matches ... in remote
         flathub". We clear the subset (--subset=) before installing.
      3. A bare ref can't be auto-resolved non-interactively, so we try a
         fully-qualified `org.winehq.Wine/<arch>/stable` ref first.
    """
    if not shutil.which("flatpak"):
        # Try to install flatpak itself via the system package manager.
        if not ensure_flatpak_installed(status_cb=status_cb):
            return (False,
                    "flatpak is not installed and could not be installed "
                    "automatically -- install flatpak, then re-run")
    if status_cb:
        status_cb("Installing Wine via Flatpak (one-time, ~30-60s)...")

    import platform
    mach = platform.machine()
    arch = {"x86_64": "x86_64", "amd64": "x86_64",
            "aarch64": "aarch64", "arm64": "aarch64",
            "i686": "i386", "i386": "i386"}.get(mach, mach)
    flathub_url = "https://flathub.org/repo/flathub.flatpakrepo"

    # When we are root, --user flatpak ops MUST run as the desktop user so the
    # per-user repo is created/owned by them (not root). --system stays root.
    def run_flatpak(scope, args, timeout):
        base = ["flatpak"] + args  # args already include the scope flag
        if is_root() and scope == "--user":
            user = target_user()
            ue = user_runtime_env(user)
            cmd = (["sudo", "-u", user, "env"]
                   + ["%s=%s" % (k, v) for k, v in ue.items()]
                   + base)
        else:
            cmd = base
        log("install_flatpak_wine: " + " ".join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=clean_subprocess_env())

    for scope in ("--user", "--system"):
        try:
            rr = run_flatpak(scope, ["remote-add", "--if-not-exists", scope,
                                     "flathub", flathub_url], 60)
            log(f"  remote-add {scope}: rc={rr.returncode} "
                f"{(rr.stderr or '')[-160:].strip()}")
        except Exception as e:
            log(f"  remote-add {scope} error: {e}")

        # Clear any verified_floss subset filter (Mint default).
        try:
            mr = run_flatpak(scope, ["remote-modify", scope, "--subset=",
                                     "flathub"], 60)
            log(f"  remote-modify(subset=) {scope}: rc={mr.returncode} "
                f"{(mr.stderr or '')[-160:].strip()}")
        except Exception as e:
            log(f"  remote-modify {scope} error: {e}")

        # The drop-to-user fix above means the per-user repo is owned by the
        # desktop user, so the repo is NOT corrupt and we do NOT need to wipe
        # repo/tmp/cache (an earlier version did `rm -rf` here, which actually
        # broke things by deleting the summary right before remote-ls read it).
        # Just refresh appstream so a freshly added remote has its index.
        try:
            ar = run_flatpak(scope, ["update", scope, "--appstream", "-y",
                                     "flathub"], 180)
            log(f"  appstream {scope}: rc={ar.returncode} "
                f"{(ar.stderr or '')[-160:].strip()}")
        except Exception as e:
            log(f"  appstream {scope} error: {e}")

        # org.winehq.Wine has MANY branches on flathub (stable-21.08 ...
        # stable-25.08, plus wow64-*). flatpak refuses to auto-pick among them
        # ("Multiple branches available ... you must specify one of"), which is
        # the "No ref chosen" error. So we query the remote and select the
        # HIGHEST `stable-NN.NN` branch for our arch -- future-proof as new
        # runtime versions land, and avoids the wow64 variants.
        try:
            ls = run_flatpak(scope, ["remote-ls", scope, "flathub",
                                     "--app", "--columns=ref"], 180)
            import re as _re
            best = None  # (version_tuple, ref_string)
            for line in (ls.stdout or "").splitlines():
                line = line.strip()
                # Refs may appear as "org.winehq.Wine/<arch>/<branch>" or
                # "app/org.winehq.Wine/<arch>/<branch>".
                norm = line[4:] if line.startswith("app/") else line
                if not norm.startswith(FLATPAK_WINE_ID + "/"):
                    continue
                parts = norm.split("/")
                if len(parts) != 3:
                    continue
                _, ref_arch, branch = parts
                if ref_arch != arch:
                    continue
                m = _re.match(r"stable-(\d+)\.(\d+)$", branch)
                if not m:
                    continue  # skip wow64-* and any non-stable branch
                ver = (int(m.group(1)), int(m.group(2)))
                if best is None or ver > best[0]:
                    best = (ver, norm)
            exact_ref = best[1] if best else None
            log(f"  remote-ls {scope}: rc={ls.returncode} "
                f"exact_ref={exact_ref}")
        except Exception as e:
            exact_ref = None
            log(f"  remote-ls {scope} error: {e}")

        # Install the exact ref first (most reliable), then fall back to the
        # looser forms in case remote-ls failed.
        ref_variants = []
        if exact_ref:
            ref_variants.append(["flathub", exact_ref])
        ref_variants += [
            ["flathub", f"{FLATPAK_WINE_ID}/{arch}/stable"],
            ["flathub", f"{FLATPAK_WINE_ID}/{arch}"],
            ["flathub", FLATPAK_WINE_ID],
            [FLATPAK_WINE_ID],
        ]
        for ref in ref_variants:
            try:
                r = run_flatpak(
                    scope,
                    ["install", scope, "-y", "--noninteractive"] + ref, 600)
                log(f"  install {scope} {ref}: rc={r.returncode} "
                    f"{(r.stderr or '')[-200:].strip()}")
                if r.returncode == 0 or find_flatpak_wine():
                    return True, "wine installed via Flatpak"
            except Exception as e:
                log(f"  install {scope} {ref} error: {e}")

    if find_flatpak_wine():
        return True, "wine installed via Flatpak"
    return False, "Flatpak wine install failed -- see launcher_debug.log"


def find_wine():
    """Return absolute path to a `wine` binary, or None."""
    return shutil.which("wine") or shutil.which("wine64")


def detect_runner(status_cb=None):
    """
    Returns dict describing how to invoke Gwent.exe:
        {"kind": "wine",        "cmd": ["/usr/bin/wine"], "label": "wine 9.x"}
        {"kind": "flatpak-wine","cmd": None,              "label": "flatpak wine"}
        None only if no wine is present and auto-install failed.

    Priority: system wine -> flatpak org.winehq.Wine -> auto-install flatpak
    wine from Flathub, then re-detect.

    Proton is deliberately NOT a runner. Proton-Experimental ships a heavily
    modified Wine fork whose `services.exe` does not reliably transition the
    32-bit Heroic GalaxyCommunication dummy to SERVICE_RUNNING, so the Galaxy
    SDK never connects to 127.0.0.1:9977 and login fails. Plain Wine (system
    or flatpak) handles the dummy correctly, so we use only wine.
    """
    log(f"detect_runner: "
        f"flatpak={'yes' if shutil.which('flatpak') else 'no'} "
        f"flatpak_wine={'yes' if find_flatpak_wine() else 'no'} "
        f"system_wine={find_wine() or 'no'}")

    def _wine_result():
        wine = find_wine()
        if wine:
            try:
                ver = subprocess.run([wine, "--version"], capture_output=True,
                                     text=True, timeout=5,
                                     env=clean_subprocess_env()).stdout.strip()
            except Exception:
                ver = "wine"
            log(f"detect_runner: using wine ({ver})")
            return {"kind": "wine", "cmd": [wine], "label": ver}
        fp = find_flatpak_wine()
        if fp:
            log("detect_runner: using flatpak wine (org.winehq.Wine)")
            return {"kind": "flatpak-wine", "cmd": None,
                    "label": "flatpak wine"}
        return None

    runner = _wine_result()
    if runner:
        return runner

    # No wine found anywhere -- auto-install flatpak wine from Flathub.
    log("detect_runner: no wine found -- attempting auto-install via Flatpak")
    if status_cb:
        status_cb("Installing Wine (one-time, ~30-60s)...")
    ok, msg = install_flatpak_wine(status_cb=status_cb)
    log(f"detect_runner: install_flatpak_wine -> ok={ok} msg={msg}")
    runner = _wine_result()
    if runner:
        return runner

    log("detect_runner: wine still not available after auto-install")
    return None


# ---------------------------------------------------------------------------
# Wine prefix preparation
# ---------------------------------------------------------------------------
def prefix_env(runner, prefix_dir):
    """Return a clean env + the env vars needed for the chosen runner."""
    env = clean_subprocess_env()
    env["WINEPREFIX"] = prefix_dir
    env["WINEDEBUG"] = env.get("WINEDEBUG", "-all")
    env["WINEDLLOVERRIDES"] = env.get(
        "WINEDLLOVERRIDES", "winhttp=n,b;version=n,b"
    )
    return env


def run_in_prefix(runner, prefix_dir, args, capture=False, timeout=None):
    """Run a Windows exe inside the prefix using the chosen runner."""
    env = prefix_env(runner, prefix_dir)
    if runner["kind"] == "flatpak-wine":
        fix_flatpak_runtime_perms()
        fix_prefix_owner(prefix_dir)
        cmd = flatpak_wine_cmd(env) + list(args)
    else:
        cmd = list(runner["cmd"]) + list(args)
    log(f"run_in_prefix: {' '.join(cmd)}")
    if capture:
        return subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=timeout)
    return subprocess.Popen(cmd, env=env)


def ensure_wine_prefix(runner, prefix_dir, status_cb=None):
    """Make sure the prefix exists and has the basics. Idempotent."""
    os.makedirs(prefix_dir, exist_ok=True)
    sys_reg = os.path.join(prefix_dir, "system.reg")
    if os.path.isfile(sys_reg):
        return  # already initialised
    if status_cb:
        status_cb("Initialising Wine prefix (first run can take ~30s)...")
    # `wineboot -i` will create the prefix.
    if runner["kind"] == "flatpak-wine":
        # Just running any small exe triggers prefix creation.
        try:
            run_in_prefix(runner, prefix_dir, ["wineboot", "-i"],
                          capture=True, timeout=120)
        except Exception as e:
            log(f"{runner['kind']} wineboot failed: {e}")
    else:
        try:
            subprocess.run(
                [runner["cmd"][0], "wineboot", "-i"],
                env=prefix_env(runner, prefix_dir),
                capture_output=True, timeout=120,
            )
        except Exception as e:
            log(f"wine wineboot failed: {e}")


def install_cert_into_prefix(runner, prefix_dir, status_cb=None):
    """
    Install fake.crt into the Wine prefix's Windows Root CA store.

    Uses `certutil -addstore Root` inside the prefix. This is identical
    to what the Windows launcher does -- but scoped to the prefix only.
    """
    if not os.path.isfile(CERT_FILE):
        log("install_cert_into_prefix: CERT_FILE missing")
        return False
    if status_cb:
        status_cb("Installing TLS cert into Wine prefix...")
    # Copy the cert into the prefix's C: drive so certutil can reach it.
    drive_c = os.path.join(prefix_dir, "drive_c")
    os.makedirs(drive_c, exist_ok=True)
    inside_cert = os.path.join(drive_c, "fake.crt")
    shutil.copy2(CERT_FILE, inside_cert)
    try:
        res = run_in_prefix(
            runner, prefix_dir,
            ["certutil", "-addstore", "Root", "C:\\fake.crt"],
            capture=True, timeout=60,
        )
        log(f"certutil rc={res.returncode}: {res.stdout[-200:] if res.stdout else ''} | "
            f"{res.stderr[-200:] if res.stderr else ''}")
        return res.returncode == 0
    except Exception as e:
        log(f"certutil failed: {e}")
        return False


def install_dummy_service_in_prefix(runner, prefix_dir, status_cb=None):
    """
    Install the dummy GalaxyCommunication service inside the Wine prefix.

    The Galaxy SDK calls StartService("GalaxyCommunication") before it tries
    to connect to 127.0.0.1:9977. Under Wine this goes through services.exe,
    so the service must actually exist in the prefix. We do exactly what
    Heroic's install-dummy-service.bat does:

      1. Copy GalaxyCommunication.exe into C:\ProgramData\GOG.com\Galaxy\
         redists\ inside the prefix.
      2. wine sc create GalaxyCommunication binpath=<that path>
      3. wine sc start GalaxyCommunication

    Idempotent: re-creating an existing service is a no-op error we ignore.
    """
    if not os.path.isfile(GALAXY_COMM_EXE):
        log("install_dummy_service_in_prefix: GALAXY_COMM_EXE missing from bundle")
        return False
    if status_cb:
        status_cb("Installing GalaxyCommunication service in prefix...")

    target_dir = os.path.join(prefix_dir, SERVICE_REL_DIR)
    os.makedirs(target_dir, exist_ok=True)
    target_exe = os.path.join(target_dir, "GalaxyCommunication.exe")
    try:
        shutil.copy2(GALAXY_COMM_EXE, target_exe)
    except Exception as e:
        log(f"copy dummy service failed: {e}")
        return False
    # The copy runs as root, leaving the exe root-owned in the desktop user's
    # prefix. wine's services.exe runs as that user and then CANNOT spawn a
    # root-owned binary -> the service never reaches RUNNING -> `sc start`
    # times out (rc=2) -> the SDK thinks Galaxy isn't running. Chown it (and
    # its dir) back to the desktop user, same as the prefix/game-file fixes.
    if is_root():
        try:
            import pwd
            pw = pwd.getpwnam(target_user())
            for pth in (target_dir, target_exe):
                try:
                    os.lchown(pth, pw.pw_uid, pw.pw_gid)
                except Exception:
                    pass
            os.chmod(target_exe, 0o755)
            log(f"install_dummy_service_in_prefix: chowned dummy exe to "
                f"{target_user()}")
        except Exception as e:
            log(f"dummy exe chown failed: {e}")

    # Windows-side path the SDK / sc see inside the prefix
    win_binpath = "C:\\ProgramData\\GOG.com\\Galaxy\\redists\\GalaxyCommunication.exe"

    # sc create with start=auto so wine's own services.exe launches the dummy
    # automatically whenever a wineserver boots (incl. Gwent's). This is what
    # makes the GalaxyCommunication service RUNNING inside Gwent's process
    # WITHOUT a second `flatpak run` (which would SIGKILL the game). ok if it
    # already exists (rc=1073 ERROR_SERVICE_EXISTS).
    try:
        res = run_in_prefix(
            runner, prefix_dir,
            ["sc", "create", SERVICE_NAME, f"binpath={win_binpath}",
             "start=", "auto"],
            capture=True, timeout=30,
        )
        log(f"sc create rc={res.returncode}: {(res.stdout or '')[-200:]} | "
            f"{(res.stderr or '')[-200:]}")
    except Exception as e:
        log(f"sc create failed: {e}")

    # Ensure start=auto even if the service already existed.
    try:
        run_in_prefix(
            runner, prefix_dir,
            ["sc", "config", SERVICE_NAME, "start=", "auto"],
            capture=True, timeout=30,
        )
    except Exception as e:
        log(f"sc config failed: {e}")

    # CRITICAL (per project_summary.md): set a permissive DACL so a non-admin
    # user can start the service. Without this, the service is created with a
    # default security descriptor that the desktop user can't start, and
    # `sc start` times out (rc=2) -> SDK thinks Galaxy isn't running. This is
    # the step the working Windows/Mint path runs that was missing on Linux.
    # SDDL grants: SYSTEM + Builtin Admins full, Interactive/Service users
    # read+start, and Everyone (S-1-1-0) start/stop rights.
    try:
        sddl = ("D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)"
                "(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)"
                "(A;;CCLCSWLOCRRC;;;IU)"
                "(A;;CCLCSWLOCRRC;;;SU)"
                "(A;;RPWPDTLO;;;S-1-1-0)")
        res = run_in_prefix(
            runner, prefix_dir,
            ["sc", "sdset", SERVICE_NAME, sddl],
            capture=True, timeout=30,
        )
        log(f"sc sdset rc={res.returncode}: {(res.stdout or '')[-200:]} | "
            f"{(res.stderr or '')[-200:]}")
    except Exception as e:
        log(f"sc sdset failed: {e}")

    # sc start -- ok if already running.
    try:
        res = run_in_prefix(
            runner, prefix_dir,
            ["sc", "start", SERVICE_NAME],
            capture=True, timeout=30,
        )
        log(f"sc start rc={res.returncode}: {(res.stdout or '')[-200:]} | "
            f"{(res.stderr or '')[-200:]}")
    except Exception as e:
        log(f"sc start failed: {e}")

    return True


def install_mod_dll(gwent_path):
    if not os.path.isfile(MOD_DLL):
        log("install_mod_dll: MOD_DLL bundle missing")
        return False
    mods = os.path.join(gwent_path, "Mods")
    os.makedirs(mods, exist_ok=True)
    shutil.copy2(MOD_DLL, os.path.join(mods, "GwentBetaRestorationMod.dll"))
    return True


def install_settings(gwent_path):
    settings_dir = os.path.join(gwent_path, "Gwent_Data", "StreamingAssets",
                                "Settings")
    os.makedirs(settings_dir, exist_ok=True)
    if os.path.isfile(SETTINGS_CONFIG):
        shutil.copy2(SETTINGS_CONFIG, os.path.join(settings_dir, "config.json"))
    if os.path.isfile(SETTINGS_LAUNCH):
        shutil.copy2(SETTINGS_LAUNCH, os.path.join(settings_dir, "Launch.cfg"))


def install_melonloader(gwent_path, status_cb=None, progress_cb=None):
    if is_melonloader_installed(gwent_path):
        return True
    if status_cb:
        status_cb("Downloading MelonLoader...")
    zip_path = os.path.join(gwent_path, "ml_download.zip")
    download_file(MELONLOADER_URL, zip_path, progress_callback=progress_cb)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(gwent_path)
    os.remove(zip_path)
    return True


# ---------------------------------------------------------------------------
# Server registration (same wire format as the Windows launcher)
# ---------------------------------------------------------------------------
def register_user(username, full_collection=False, server_url=None):
    base = server_url or SERVER_URL
    payload = json.dumps({
        "username": username,
        "full_collection": full_collection,
    }).encode()
    req = urllib.request.Request(
        f"{base}/register", data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, context=ssl_context(), timeout=10)
        data = json.loads(resp.read())
        return data["id"], data.get("username", username)
    except Exception as e:
        return None, str(e)


def login_user(username, user_id, server_url=None):
    """Sign in to an existing account (username + user_id must match).

    Returns (user_id, username) or (None, error).
    """
    base = server_url or SERVER_URL
    payload = json.dumps({"username": username, "id": user_id}).encode()
    req = urllib.request.Request(
        f"{base}/login", data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, context=ssl_context(), timeout=10)
        data = json.loads(resp.read())
        return data["id"], data.get("username", username)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
            return None, err.get("error", body)
        except Exception:
            return None, f"Server error {e.code}: {body}"
    except Exception as e:
        return None, f"Connection failed: {e}"


def change_username(user_id, new_username):
    payload = json.dumps({
        "user_id": user_id,
        "new_username": new_username,
    }).encode()
    req = urllib.request.Request(
        f"{SERVER_URL}/change_username", data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, context=ssl_context(), timeout=10)
        data = json.loads(resp.read())
        return True, data.get("username", new_username)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
            return False, err.get("error", body)
        except Exception:
            return False, f"Server error {e.code}: {body}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def create_users_json(user_id, username):
    ensure_data_dir()
    with open(USERS_JSON, "w") as f:
        json.dump([{"id": user_id, "username": username}], f, indent=2)


# ---------------------------------------------------------------------------
# DNS plumbing -- systemd-resolved aware
# ---------------------------------------------------------------------------
def detect_upstream_dns():
    """
    Pick a real upstream DNS server. We try resolv.conf first; if it points at
    127.0.0.53 (systemd-resolved), we read the actual upstream from `resolvectl`.
    Falls back to 1.1.1.1.
    """
    # Try resolvectl first.
    try:
        r = subprocess.run(
            ["resolvectl", "dns"], capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                # "Link 2 (wlan0): 192.168.1.1"
                parts = line.strip().split(":")
                if len(parts) >= 2:
                    ips = parts[-1].strip().split()
                    for ip in ips:
                        if ip and ip[0].isdigit() and "." in ip \
                                and not ip.startswith("127."):
                            return ip
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    # Fall back to parsing /etc/resolv.conf.
    try:
        with open(RESOLV_CONF) as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver"):
                    ip = line.split()[1]
                    if not ip.startswith("127."):
                        return ip
    except Exception:
        pass

    return "1.1.1.1"


def is_resolv_symlink():
    try:
        return os.path.islink(RESOLV_CONF)
    except Exception:
        return False


def write_resolv_localhost():
    """
    Point /etc/resolv.conf at 127.0.0.1. Backs up the original (or saves the
    symlink target) so we can restore it on exit.

    Returns True on success.
    """
    try:
        if not os.path.exists(RESOLV_BACKUP):
            if is_resolv_symlink():
                # Save the symlink target as a marker.
                target = os.readlink(RESOLV_CONF)
                with open(RESOLV_BACKUP, "w") as f:
                    f.write(f"__symlink__:{target}\n")
            else:
                shutil.copy2(RESOLV_CONF, RESOLV_BACKUP)
        # Replace.
        try:
            os.remove(RESOLV_CONF)
        except FileNotFoundError:
            pass
        with open(RESOLV_CONF, "w") as f:
            f.write(
                "# Managed by Gwent Beta Linux Launcher\n"
                "nameserver 127.0.0.1\n"
                "options edns0 trust-ad\n"
            )
        # Verify the write actually landed. On SteamOS the file is owned
        # by NetworkManager and atomic-replaces our content immediately; on
        # other distros the same can happen via dhcpcd / systemd-resolved.
        try:
            with open(RESOLV_CONF) as vf:
                verify = vf.read()
            if "127.0.0.1" not in verify:
                log(f"write_resolv_localhost: post-write verify FAILED. "
                    f"File still says: {verify!r}")
                # Don't return True -- caller will fall back to /etc/hosts.
                return False
        except Exception as e:
            log(f"write_resolv_localhost: verify read failed: {e}")
            return False
        # Make it immutable so NetworkManager / dhcpcd / systemd-resolved
        # can't silently rewrite it mid-session. This is the #1 cause of
        # "kicked to login screen" on Linux Mint / Ubuntu derivatives --
        # NetworkManager owns /etc/resolv.conf and refreshes it on every
        # DHCP renewal / wifi event, blowing away our 127.0.0.1 entry.
        # chattr +i is reverted in restore_resolv().
        try:
            subprocess.run(["chattr", "+i", RESOLV_CONF],
                           capture_output=True, timeout=5)
        except Exception as e:
            log(f"chattr +i resolv.conf failed (non-fatal): {e}")
        return True
    except PermissionError:
        log("write_resolv_localhost: permission denied (not root?)")
        return False
    except Exception as e:
        log(f"write_resolv_localhost failed: {e}")
        return False


def restore_resolv():
    """Restore /etc/resolv.conf from backup. Best-effort."""
    # Always try to drop the immutable bit first, even if we're not sure
    # we set it -- a leftover +i from a previous crashed session would
    # otherwise block all writes here.
    try:
        subprocess.run(["chattr", "-i", RESOLV_CONF],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    if not os.path.exists(RESOLV_BACKUP):
        return
    try:
        with open(RESOLV_BACKUP) as f:
            head = f.read(200)
        if head.startswith("__symlink__:"):
            target = head.split(":", 1)[1].strip()
            try:
                os.remove(RESOLV_CONF)
            except FileNotFoundError:
                pass
            os.symlink(target, RESOLV_CONF)
        else:
            shutil.copy2(RESOLV_BACKUP, RESOLV_CONF)
        os.remove(RESOLV_BACKUP)
    except Exception as e:
        log(f"restore_resolv failed: {e}")


_STEAMOS_CACHE = None


def is_steamos():
    """True if running on SteamOS / Steam Deck.

    Robust against the several ways a Deck identifies itself: os-release may
    carry ID=steamos, ID=holo, ID_LIKE=arch with VARIANT_ID=steamdeck, and the
    value may be quoted. We also treat the presence of the steamos-readonly
    tool or the canonical /home/deck user as corroborating signals, since the
    bare ID= check has missed real Decks in the field."""
    global _STEAMOS_CACHE
    if _STEAMOS_CACHE is not None:
        return _STEAMOS_CACHE
    result = False
    detail = ""
    try:
        with open("/etc/os-release") as f:
            data = f.read().lower()
        detail = " ".join(data.split())[:200]
        if ("steamos" in data or "steamdeck" in data
                or "id=holo" in data or 'id="holo"' in data):
            result = True
    except Exception as e:
        detail = f"os-release read failed: {e}"
    if not result:
        # Corroborating signals.
        if shutil.which("steamos-readonly"):
            result = True
            detail += " | steamos-readonly present"
        elif os.path.isdir("/home/deck"):
            result = True
            detail += " | /home/deck present"
    try:
        log(f"is_steamos: {result} ({detail})")
    except Exception:
        pass
    _STEAMOS_CACHE = result
    return result


def disable_steamos_readonly():
    """SteamOS protects / as read-only. Drop the protection so we can edit
    /etc/resolv.conf and /etc/hosts. No-op on non-SteamOS systems."""
    if not is_steamos():
        return
    try:
        r = subprocess.run(["steamos-readonly", "status"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and "disabled" in (r.stdout + r.stderr).lower():
            return  # already disabled
    except FileNotFoundError:
        return
    except Exception as e:
        log(f"steamos-readonly status check failed: {e}")
    try:
        subprocess.run(["steamos-readonly", "disable"],
                       capture_output=True, timeout=10)
        log("steamos-readonly disabled (SteamOS detected)")
    except Exception as e:
        log(f"steamos-readonly disable failed: {e}")


def write_hosts_entries():
    """
    Append seawolf-*.gog.com -> 127.0.0.1 entries to /etc/hosts inside our
    own marker block. This is the most robust path on Steam Deck because
    NetworkManager/ConnMan don't touch /etc/hosts and libc consults hosts
    before DNS. Idempotent: re-running just rewrites our block.
    """
    try:
        with open(HOSTS_PATH) as f:
            content = f.read()
    except Exception as e:
        log(f"read /etc/hosts failed: {e}")
        return False
    # Backup once, on first run.
    try:
        if not os.path.exists(HOSTS_BACKUP):
            with open(HOSTS_BACKUP, "w") as f:
                f.write(content)
    except Exception as e:
        log(f"backup /etc/hosts failed: {e}")
    # Strip any previous block.
    if HOSTS_MARKER_BEGIN in content and HOSTS_MARKER_END in content:
        before = content.split(HOSTS_MARKER_BEGIN)[0]
        after = content.split(HOSTS_MARKER_END)[1]
        content = before.rstrip() + "\n" + after.lstrip()
    # Append our block.
    block = [HOSTS_MARKER_BEGIN]
    for h in GOG_HOSTS:
        block.append(f"127.0.0.1\t{h}")
    block.append(HOSTS_MARKER_END)
    new_content = content.rstrip() + "\n" + "\n".join(block) + "\n"
    try:
        # chattr -i in case we set it last session
        subprocess.run(["chattr", "-i", HOSTS_PATH],
                       capture_output=True, timeout=5)
        with open(HOSTS_PATH, "w") as f:
            f.write(new_content)
        log(f"/etc/hosts: added {len(GOG_HOSTS)} entries")
        return True
    except PermissionError:
        log("write_hosts_entries: permission denied (not root?)")
        return False
    except Exception as e:
        log(f"write_hosts_entries failed: {e}")
        return False


def restore_hosts():
    """Remove our marker block from /etc/hosts."""
    try:
        subprocess.run(["chattr", "-i", HOSTS_PATH],
                       capture_output=True, timeout=5)
        with open(HOSTS_PATH) as f:
            content = f.read()
        if HOSTS_MARKER_BEGIN not in content:
            return
        before = content.split(HOSTS_MARKER_BEGIN)[0]
        try:
            after = content.split(HOSTS_MARKER_END)[1]
        except IndexError:
            after = ""
        cleaned = before.rstrip() + "\n" + after.lstrip()
        with open(HOSTS_PATH, "w") as f:
            f.write(cleaned)
    except Exception as e:
        log(f"restore_hosts failed: {e}")


def _resolve_via_public_dns(hostname):
    """Resolve hostname via a public DNS server (1.1.1.1 / 8.8.8.8) using a
    raw socket query so we bypass /etc/hosts and /etc/resolv.conf. Returns
    a set of IPv4 strings. Used to find the real gog.com IPs so we can
    DNAT-redirect them to localhost."""
    ips = set()
    for upstream in PUBLIC_DNS:
        try:
            import struct, random
            tid = random.randint(0, 65535)
            # DNS query: header + question (qname, A, IN)
            header = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
            q = b""
            for label in hostname.split("."):
                q += bytes([len(label)]) + label.encode("ascii")
            q += b"\x00" + struct.pack("!HH", 1, 1)  # QTYPE=A, QCLASS=IN
            packet = header + q
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.0)
            sock.sendto(packet, (upstream, 53))
            data, _ = sock.recvfrom(4096)
            sock.close()
            # Skip past header (12 bytes) and question section
            pos = 12
            while data[pos] != 0:
                pos += data[pos] + 1
            pos += 5  # null + QTYPE + QCLASS
            # Parse answers
            ancount = struct.unpack("!H", data[6:8])[0]
            for _ in range(ancount):
                # name (pointer or label)
                if data[pos] & 0xC0:
                    pos += 2
                else:
                    while data[pos] != 0:
                        pos += data[pos] + 1
                    pos += 1
                rtype = struct.unpack("!H", data[pos:pos+2])[0]
                rdlen = struct.unpack("!H", data[pos+8:pos+10])[0]
                pos += 10
                if rtype == 1 and rdlen == 4:  # A record
                    ip = ".".join(str(b) for b in data[pos:pos+4])
                    ips.add(ip)
                pos += rdlen
            if ips:
                break  # got answers, stop trying upstreams
        except Exception as e:
            log(f"_resolve_via_public_dns({hostname}) via {upstream}: {e}")
            continue
    return ips


def _resolve_gog_ips(passes=4):
    """Resolve every gog host several times and union the answers. Fastly
    (auth/remote-config/seawolf-*/users) is anycast and returns only a few
    of its many .241 IPs per query, rotating which ones -- so a single pass
    catches only a fraction of the IPs the SDK might actually connect to.
    Multiple passes gather a much larger slice. Only ever returns IPs that a
    GOG hostname actually resolves to, so we never touch unrelated sites."""
    ips = set()
    for _ in range(max(1, passes)):
        for host in GOG_HOSTS:
            try:
                ips |= _resolve_via_public_dns(host)
            except Exception:
                pass
    return ips


def _add_dnat_for_ips(ips, already):
    """Add an OUTPUT DNAT (:443 -> 127.0.0.1:443) for each IP not already
    redirected. Mutates and returns `already` (the tracked set used for
    cleanup). Narrow by construction: only the passed gog-derived IPs."""
    for ip in ips:
        if ip in already:
            continue
        try:
            r = subprocess.run(
                ["iptables", "-t", "nat", "-A", "OUTPUT",
                 "-p", "tcp", "-d", ip, "--dport", "443",
                 "-j", "DNAT", "--to-destination", "127.0.0.1:443"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                already.add(ip)
                log(f"iptables DNAT: ({ip}):443 -> 127.0.0.1:443")
            else:
                log(f"iptables DNAT failed for {ip}: "
                    f"{(r.stderr or r.stdout).strip()}")
        except FileNotFoundError:
            log("iptables not found -- cannot install DNAT fallback")
            break
        except Exception as e:
            log(f"iptables DNAT exception for {ip}: {e}")
    return already


def install_iptables_dnat():
    """
    Kernel-level redirect of all TCP-443 traffic destined for the real
    gog.com IPs back to 127.0.0.1:443. This is the only redirect that
    NetworkManager / systemd-resolved / Wine's resolver can't bypass, and
    it's the workaround for Steam Deck where /etc/hosts gets ignored or
    overwritten despite our best efforts.

    Resolves each host over MULTIPLE passes so we capture more of Fastly's
    rotating anycast IPs up front (root cause of the 'auth call escaped to
    real gog -> invalid_grant -> SessionManager Connection lost' bug: a
    single-pass DNAT caught only ~4 of auth.gog.com's Fastly IPs, and the
    SDK later used one we'd missed). `refresh_iptables_dnat()` tops this up
    periodically during the session for IPs that rotate in later.

    Returns the set of IPs we redirected so we can flush exactly those
    rules on cleanup.
    """
    redirected = set()
    _add_dnat_for_ips(_resolve_gog_ips(passes=4), redirected)
    # Range DNAT (the real catch-all -- see GOG_IP_RANGES). DNAT every :443 to
    # a gog CIDR -> 127.0.0.1 so no gog host can escape regardless of which IP
    # the game resolves. Tracked in `redirected` so cleanup removes them too.
    for _cidr in GOG_IP_RANGES:
        try:
            r = subprocess.run(
                ["iptables", "-t", "nat", "-A", "OUTPUT",
                 "-p", "tcp", "-d", _cidr, "--dport", "443",
                 "-j", "DNAT", "--to-destination", "127.0.0.1:443"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                redirected.add(_cidr)
                log(f"iptables DNAT: range {_cidr}:443 -> 127.0.0.1:443")
            else:
                log(f"iptables DNAT range failed for {_cidr}: "
                    f"{(r.stderr or r.stdout).strip()}")
        except Exception as e:
            log(f"iptables DNAT range exception for {_cidr}: {e}")
    # Block outgoing IPv6 :443 entirely so the SDK can't escape over v6
    # when our v4 redirects miss something. Some seawolf-* names have
    # AAAA records (Fastly) that we won't catch otherwise.
    try:
        subprocess.run(
            ["ip6tables", "-A", "OUTPUT", "-p", "tcp", "--dport", "443",
             "-j", "REJECT"],
            capture_output=True, timeout=5,
        )
        log("ip6tables: blocked IPv6 :443 outgoing")
    except Exception:
        pass
    log(f"iptables DNAT: installed {len(redirected)} rule(s) for gog IPs")
    _install_session_ips.update(redirected)
    return redirected


def refresh_iptables_dnat(redirected):
    """Re-resolve the gog hosts and add DNAT for any newly-seen Fastly IPs.
    Mutates `redirected` in place (the same set passed to cleanup) so rules
    added here are also removed on session end. Cheap and narrow -- only
    gog-domain IPs, never a blanket range, so it can't capture other sites."""
    try:
        new_ips = _resolve_gog_ips(passes=2) - redirected
        if new_ips:
            _add_dnat_for_ips(new_ips, redirected)
            _install_session_ips.update(new_ips)
            log(f"iptables DNAT refresh: +{len(new_ips)} rotated gog IP(s)")
    except Exception as e:
        log(f"refresh_iptables_dnat failed: {e}")
    return redirected


def uninstall_iptables_dnat(redirected_ips):
    """Flush the DNAT rules we added. Uses -D (delete-by-match) for each
    rule so we don't blow away rules other apps may have added."""
    for ip in redirected_ips:
        try:
            subprocess.run(
                ["iptables", "-t", "nat", "-D", "OUTPUT",
                 "-p", "tcp", "-d", ip, "--dport", "443",
                 "-j", "DNAT", "--to-destination", "127.0.0.1:443"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
    try:
        subprocess.run(
            ["ip6tables", "-D", "OUTPUT", "-p", "tcp", "--dport", "443",
             "-j", "REJECT"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass
    log(f"iptables DNAT: removed {len(redirected_ips)} rules")


def stop_systemd_resolved():
    """Stop systemd-resolved so we can bind 127.0.0.1:53 ourselves."""
    try:
        subprocess.run(["systemctl", "stop", "systemd-resolved"],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def start_systemd_resolved():
    try:
        subprocess.run(["systemctl", "start", "systemd-resolved"],
                       capture_output=True, timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# DNS safety net
# ---------------------------------------------------------------------------
_dns_modified = False


def reconcile_stale_redirects():
    """Undo any redirect state stranded by a previous HARD kill (SIGKILL /
    power-loss), where neither the per-match finally nor the signal handler
    ran. Safe to call unconditionally at startup: every step is a no-op when
    nothing was stranded. Mirrors the Windows launcher's startup self-heal."""
    # 1. resolv.conf: it may be chattr +i and pointing at 127.0.0.1. Only act
    #    if OUR backup exists (proof we were the ones who changed it).
    try:
        if os.path.exists(RESOLV_BACKUP):
            log("reconcile: stale resolv.conf backup found -- restoring")
            restore_resolv()  # does chattr -i then restores from backup
    except Exception as e:
        log(f"reconcile resolv failed: {e}")
    # 2. /etc/hosts: strip our marker block if it survived.
    try:
        with open(HOSTS_PATH) as _f:
            if HOSTS_MARKER_BEGIN in _f.read():
                log("reconcile: stale /etc/hosts block found -- removing")
                restore_hosts()
    except Exception as e:
        log(f"reconcile hosts failed: {e}")
    # 3. iptables/ip6tables: flush any leftover gog DNAT + the IPv6 REJECT.
    #    We don't know the exact per-IP rules from a dead session, but the
    #    CIDR range rules and the ip6tables REJECT are fixed and the most
    #    harmful leftovers, so remove those by their known signature.
    try:
        uninstall_iptables_dnat(set(GOG_IP_RANGES))
    except Exception as e:
        log(f"reconcile iptables failed: {e}")


def _emergency_cleanup():
    global _dns_modified
    if _dns_modified:
        restore_resolv()
        restore_hosts()
        # Flush iptables/ip6tables redirects too -- the per-match finally
        # blocks do this, but on a signal-triggered exit we must also clear
        # them or a machine-wide ip6tables REJECT on :443 / stale gog DNAT
        # is left behind, breaking HTTPS until manually flushed.
        try:
            uninstall_iptables_dnat(_install_session_ips | set(GOG_IP_RANGES))
            _install_session_ips.clear()
        except Exception:
            pass
        start_systemd_resolved()
        _dns_modified = False


atexit.register(_emergency_cleanup)
for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    try:
        signal.signal(_sig, lambda s, f: (_emergency_cleanup(), sys.exit(1)))
    except (OSError, ValueError):
        pass


def mark_dns_modified():
    global _dns_modified
    _dns_modified = True


def mark_dns_restored():
    global _dns_modified
    _dns_modified = False


# ---------------------------------------------------------------------------
# In-process service runners (mirror launcher.py)
# ---------------------------------------------------------------------------
def _import_bundled(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def start_commservice_thread():
    sys.path.insert(0, BUNDLE_DIR)
    comm = _import_bundled("commservice", COMMSERVICE_PY)
    comm.USERS_FILE = USERS_JSON
    # Route commservice's [COMM] print() output into launcher_debug.log so we
    # can see the per-connection handshake (AUTH_INFO_REQUEST etc.) after the
    # session -- otherwise its stdout is lost and we only see the bare socket
    # state. Inject a module-level print that forwards to log().
    def _comm_print(*args, **kwargs):
        try:
            log("COMMSVC " + " ".join(str(a) for a in args))
        except Exception:
            pass
    comm.print = _comm_print
    comm.load_users()
    srv = comm.ThreadedTCPServer(("127.0.0.1", 9977), comm.CommServiceHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log("commservice started on 127.0.0.1:9977")
    return srv


def start_dns_proxy_thread(server_ip, upstream):
    dns_mod = _import_bundled("dns_proxy", DNS_PROXY_PY)

    def _run():
        try:
            dns_mod.run_dns_proxy(server_ip, upstream, 53, "127.0.0.1")
        except Exception as e:
            log(f"dns_proxy crashed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    log(f"dns_proxy started -> {server_ip} (upstream={upstream})")
    return t


def start_https_proxy_thread(cert, key, remote):
    proxy_mod = _import_bundled("https_proxy", HTTPS_PROXY_PY)

    def _run():
        try:
            proxy_mod.run_https_proxy(cert, key, remote, 443)
        except Exception as e:
            log(f"https_proxy crashed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    log(f"https_proxy started on 127.0.0.1:443 -> {remote}")
    return t


def start_broker_proxy_thread(cert, key, remote):
    proxy_mod = _import_bundled("https_proxy", HTTPS_PROXY_PY)

    def _run():
        try:
            proxy_mod.run_broker_proxy(cert, key, remote, 8445, 8445)
        except Exception as e:
            log(f"broker_proxy crashed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    log(f"broker_proxy started on 127.0.0.1:8445 -> {remote}:8445")
    return t


def start_relay_proxy_thread(remote):
    proxy_mod = _import_bundled("https_proxy", HTTPS_PROXY_PY)

    def _run():
        try:
            proxy_mod.run_relay_proxy(remote, 7777, 7777)
        except Exception as e:
            log(f"relay_proxy crashed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    log(f"relay_proxy started on 127.0.0.1:7777 -> {remote}:7777")
    return t


def wait_port_open(host, port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            time.sleep(0.1)
        finally:
            try:
                s.close()
            except Exception:
                pass
    return False


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------
def download_file(url, dest_path, progress_callback=None, ctx=None):
    if ctx is None:
        ctx = ssl_context()
    req = urllib.request.Request(url)
    resp = urllib.request.urlopen(req, context=ctx, timeout=60)
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    block = 256 * 1024
    with open(dest_path, "wb") as f:
        while True:
            chunk = resp.read(block)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress_callback:
                progress_callback(downloaded, total)
    return dest_path


# ---------------------------------------------------------------------------
# Root re-exec via pkexec
# ---------------------------------------------------------------------------
def ensure_root():
    """
    If not root, re-execute under pkexec/sudo. Returns only if already root.

    On the Steam Deck the user has a sudo password (the one they set in
    Desktop Mode); pkexec will pop a graphical prompt for it.
    """
    if is_root():
        return
    cmd = None
    if shutil.which("pkexec"):
        # Preserve DISPLAY/WAYLAND env so the Tk GUI can reopen.
        env_kv = [
            f"DISPLAY={os.environ.get('DISPLAY', '')}",
            f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '')}",
            f"XAUTHORITY={os.environ.get('XAUTHORITY', '')}",
            f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '')}",
            f"HOME={os.environ.get('HOME', '')}",
            f"USER={os.environ.get('USER', '')}",
            f"DBUS_SESSION_BUS_ADDRESS={os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')}",
        ]
        cmd = ["pkexec", "env"] + env_kv + [sys.executable] + sys.argv
    elif shutil.which("sudo"):
        cmd = ["sudo", "-E", sys.executable] + sys.argv
    else:
        print("ERROR: root is required (need pkexec or sudo).", file=sys.stderr)
        sys.exit(1)
    os.execvp(cmd[0], cmd)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def _snapshot_log():
    """Dump key DNS/network state to launcher_debug.log. Called periodically
    during run_game() so we can see what the system actually looked like
    while Gwent was running -- otherwise the post-session restore wipes the
    evidence."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"\n===== SNAPSHOT {time.strftime('%H:%M:%S')} =====\n")
            # Compact: hosts redirect present?  resolv nameserver?  9977 state?
            try:
                with open(HOSTS_PATH) as hf:
                    hc = hf.read()
                f.write("[hosts] gwent block %s\n"
                        % ("PRESENT" if HOSTS_MARKER_BEGIN in hc else "ABSENT"))
            except Exception as e:
                f.write(f"[hosts] read failed: {e}\n")
            try:
                with open(RESOLV_CONF) as rf:
                    ns = [l.strip() for l in rf if l.startswith("nameserver")]
                f.write("[resolv] %s\n" % (", ".join(ns) or "(none)"))
            except Exception:
                pass
            try:
                r = subprocess.run(["getent", "hosts", "auth.gog.com"],
                                   capture_output=True, text=True, timeout=3)
                f.write(f"[getent auth.gog.com] {r.stdout.strip() or '(empty)'}\n")
            except Exception:
                pass
            # Redirect-layer diagnostics. ROOT CAUSE under investigation: on
            # failing sessions the SDK's https auth.gog.com/token escaped to the
            # REAL gog servers (GalaxyPeer log: Via: varnish, 400 invalid_grant).
            # These lines show, per snapshot, whether each gog host still
            # resolves to 127.0.0.1 and whether the iptables DNAT rules that
            # catch real-gog IPs are still present (Fastly rotates IPs, so a
            # launch-time DNAT snapshot can miss an IP the SDK uses later).
            try:
                for _h in ("auth.gog.com", "remote-config.gog.com",
                           "users.gog.com", "notifications-pusher.gog.com"):
                    rr = subprocess.run(["getent", "hosts", _h],
                                        capture_output=True, text=True, timeout=3)
                    _out = rr.stdout.strip().split("\n")[0] if rr.stdout.strip() else "(empty)"
                    _ok = _out.startswith("127.0.0.1")
                    f.write(f"[redir {_h}] {_out} {'OK' if _ok else 'ESCAPE!'}\n")
            except Exception as e:
                f.write(f"[redir] getent loop failed: {e}\n")
            try:
                rr = subprocess.run(["iptables", "-t", "nat", "-S", "OUTPUT"],
                                    capture_output=True, text=True, timeout=3)
                _dnat = [l.strip() for l in rr.stdout.splitlines()
                         if "DNAT" in l and "127.0.0.1:443" in l]
                f.write(f"[dnat] {len(_dnat)} rule(s) redirecting :443 -> 127.0.0.1\n")
            except Exception as e:
                f.write(f"[dnat] iptables -S failed: {e}\n")
            # Per-port socket states. 9977 = commservice (SDK identity);
            # 8445 = broker notification WebSocket; 443 = HTTPS/token refresh.
            # ESTAB = SDK connected (healthy); only LISTEN / absent at the kick
            # moment for 8445 or 443 = the leg whose collapse signs us out.
            try:
                r = subprocess.run(["ss", "-tnp", "state", "all"],
                                   capture_output=True, text=True, timeout=3)
                ss_lines = r.stdout.splitlines()
                for port in ("9977", "8445", "443"):
                    needle = f":{port}"
                    hits = [l.strip() for l in ss_lines if needle in l]
                    if hits:
                        for l in hits:
                            f.write(f"[{port}] {l}\n")
                    else:
                        f.write(f"[{port}] no sockets on {port}\n")
            except Exception as e:
                f.write(f"[ports] check failed: {e}\n")
            # SDK session/auth tail: surface the most recent SessionManager /
            # auth / token / connection-lost lines from Gwent's output_log so
            # the kick REASON is timestamped alongside the socket states.
            try:
                prefix = os.environ.get("WINEPREFIX") or ""
                if not prefix:
                    cfg = load_config()
                    prefix = cfg.get("prefix") or DEFAULT_PREFIX
                _olog = None
                _users_root = os.path.join(prefix, "drive_c", "users")
                for _root, _dirs, _files in os.walk(_users_root):
                    for _fn in _files:
                        if _fn.startswith("output_log") or _fn == "Player.log":
                            _olog = os.path.join(_root, _fn)
                            break
                    if _olog:
                        break
                if _olog:
                    with open(_olog, "r", errors="replace") as of:
                        _lines = of.readlines()
                    _key = ("SessionManager", "Connection lost", "Sign Out",
                            "Sign out", "Login screen", "invalid_grant",
                            "Unauthorized", "401", "auth", "Auth",
                            "Compromised", "token", "Token", "Service interrupted")
                    _hits = [ln.rstrip() for ln in _lines[-400:]
                             if any(k in ln for k in _key)]
                    if _hits:
                        f.write("[sdk] " + "\n[sdk] ".join(_hits[-8:]) + "\n")
            except Exception as e:
                f.write(f"[sdk] tail failed: {e}\n")
            f.write("===== END SNAPSHOT =====\n\n")
    except Exception as e:
        log(f"snapshot failed: {e}")


def _snapshot_game_view(game_pid):
    """
    Once Gwent.exe is running, capture /proc/<pid>/root/etc/hosts + resolv.conf
    so we can see whether Proton's pressure-vessel sandbox is hiding our
    redirects from the game. Logged to launcher_debug.log under a clearly
    labelled section.
    """
    # Wait up to 60s for Gwent.exe (a child of the proton/wine launcher) to
    # appear. The pid we got is the proton wrapper, not the actual game --
    # the game launches as a descendant.
    deadline = time.time() + 60.0
    gwent_pid = None
    while time.time() < deadline:
        try:
            r = subprocess.run(["pgrep", "-f", "Gwent.exe"],
                               capture_output=True, text=True, timeout=3)
            pids = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
            if pids:
                gwent_pid = pids[0]
                break
        except Exception:
            pass
        time.sleep(1.0)
    if not gwent_pid:
        log("game_view: never saw Gwent.exe in pgrep")
        return
    # Give the game a couple of seconds to fully start its libc resolver.
    time.sleep(3.0)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"\n========== GAME-VIEW SNAPSHOT pid={gwent_pid} "
                    f"at {time.strftime('%H:%M:%S')} ==========\n")
            # /proc/<pid>/root shows the filesystem AS THAT PROCESS SEES IT.
            # If Proton pressure-vessel sandboxes the game, this will be
            # different from the host /etc/hosts.
            for path in ("/etc/hosts", "/etc/resolv.conf",
                         "/etc/nsswitch.conf"):
                target = f"/proc/{gwent_pid}/root{path}"
                f.write(f"\n[game view {path}]\n")
                try:
                    with open(target) as gf:
                        content = gf.read()
                    # Tail-ish: last 40 lines is plenty
                    lines = content.splitlines()
                    if len(lines) > 40:
                        f.write("... (truncated, showing last 40 lines)\n")
                        lines = lines[-40:]
                    f.write("\n".join(lines) + "\n")
                except Exception as e:
                    f.write(f"  read failed: {e}\n")
            # Compare host vs game by hashing the hosts file -- quickest way
            # to know if they differ at all.
            try:
                import hashlib
                with open("/etc/hosts", "rb") as hf:
                    host_h = hashlib.sha256(hf.read()).hexdigest()[:16]
                with open(f"/proc/{gwent_pid}/root/etc/hosts", "rb") as gf:
                    game_h = hashlib.sha256(gf.read()).hexdigest()[:16]
                f.write(f"\n[hosts hash] host={host_h} game={game_h} "
                        f"{'IDENTICAL' if host_h == game_h else 'DIFFER!'}\n")
            except Exception as e:
                f.write(f"[hosts hash] failed: {e}\n")
            # Mount info -- shows whether /etc is bind-mounted from somewhere
            try:
                with open(f"/proc/{gwent_pid}/mounts") as mf:
                    mounts = mf.read()
                f.write("\n[game mounts /etc lines]\n")
                for line in mounts.splitlines():
                    if "/etc" in line or "/run/host" in line:
                        f.write(f"  {line}\n")
            except Exception as e:
                f.write(f"[game mounts] read failed: {e}\n")
            # Also surface Gwent's own logs (Unity Player.log + MelonLoader).
            # These are the most diagnostic of all: they tell us which HTTP
            # call to which gog.com endpoint actually failed and how.
            f.write("\n[Gwent/Unity logs]\n")
            try:
                # Wine maps the Windows %APPDATA% to drive_c/users/<user>/AppData
                prefix = os.environ.get("WINEPREFIX") or ""
                if not prefix:
                    # Fallback: read from the cfg
                    cfg = load_config()
                    prefix = cfg.get("prefix") or DEFAULT_PREFIX
                candidates = []
                # Unity Player/output log (holds the real runtime errors).
                # We intentionally do NOT echo the MelonLoader logs -- they are
                # large and static (just confirm the mod loaded), and bloat the
                # debug log enormously.
                appdata_lo = os.path.join(prefix, "drive_c", "users")
                for root, dirs, files in os.walk(appdata_lo):
                    for fn in files:
                        if (fn in ("Player.log", "Player-prev.log")
                                or fn.startswith("output_log")):
                            candidates.append(os.path.join(root, fn))
                if not candidates:
                    f.write("  no Player/output log found\n")
                for c in candidates:
                    f.write(f"\n--- {os.path.basename(c)} (last 60 lines) ---\n")
                    try:
                        with open(c, "r", errors="replace") as cf:
                            lines = cf.readlines()
                        f.write("".join(lines[-60:]))
                    except Exception as e:
                        f.write(f"  read failed: {e}\n")
            except Exception as e:
                f.write(f"  unity log capture failed: {e}\n")
            f.write("========== END GAME-VIEW SNAPSHOT ==========\n\n")
    except Exception as e:
        log(f"_snapshot_game_view failed: {e}")


def _start_service_in_game_session(runner, prefix_dir, initial_delay=0.0):
    """
    Wait for Gwent.exe to appear, then run `wine sc start GalaxyCommunication`
    in the same wineprefix. This ensures the dummy service is running INSIDE
    the wineserver that Gwent is using.

    On Steam Deck with Proton, the launcher's earlier `sc create/start` calls
    run in their own (now-exited) wineservers; the service registration
    persists in the prefix but the running instance dies with the wineserver.
    When Gwent.exe starts (in a NEW wineserver), the SDK calls
    StartService("GalaxyCommunication") -- on some Wine builds this fails
    because the service binary's last exit code stuck it in a STOPPED state
    that wine-services.exe won't bring back automatically. Running sc start
    explicitly during Gwent's session works around that.

    Idempotent: sc start on an already-running service returns rc=1056 and
    we ignore it. Re-runs every 15s for the first 90s of the game to catch
    any later StartService attempts by the SDK.
    """
    def _run():
        if initial_delay > 0:
            # Flatpak: let Gwent's own wineserver fully come up first, so our
            # later `sc start` (a new flatpak instance sharing this WINEPREFIX)
            # attaches to it instead of racing startup and SIGKILLing the game.
            log(f"service-in-session: waiting {initial_delay:.0f}s before "
                f"starting service (flatpak wineserver settle)")
            time.sleep(initial_delay)
        # Wait for Gwent.exe to appear
        deadline = time.time() + 60.0
        while time.time() < deadline:
            try:
                r = subprocess.run(["pgrep", "-f", "Gwent.exe"],
                                   capture_output=True, text=True, timeout=3)
                if r.stdout.strip():
                    break
            except Exception:
                pass
            time.sleep(1.0)
        else:
            log("service-in-session: Gwent.exe never appeared")
            return

        # Fire `sc start` repeatedly for the first 90s of the session.
        log("service-in-session: Gwent.exe up, starting service from inside")
        for attempt in range(6):
            try:
                res = run_in_prefix(
                    runner, prefix_dir,
                    ["sc", "start", SERVICE_NAME],
                    capture=True, timeout=15,
                )
                out = ((res.stdout or "") + (res.stderr or ""))[-200:]
                log(f"service-in-session sc start attempt {attempt+1}: "
                    f"rc={res.returncode} {out.strip()}")
                # rc=0 = started, rc=1056 = already running, both OK
                if res.returncode == 0 or "1056" in out:
                    break
            except Exception as e:
                log(f"service-in-session attempt {attempt+1}: {e}")
            time.sleep(15.0)

        # Diagnostic: what does sc query say? `proton run` swallows stdout,
        # so we redirect to a file inside the prefix and read it back.
        try:
            # Use cmd.exe redirection so the file lands in the prefix's drive_c
            out_path_win = "C:\\sc_query_out.txt"
            out_path_unix = os.path.join(prefix_dir, "drive_c",
                                         "sc_query_out.txt")
            try:
                os.remove(out_path_unix)
            except FileNotFoundError:
                pass
            run_in_prefix(
                runner, prefix_dir,
                ["cmd", "/c", f"sc query {SERVICE_NAME} > {out_path_win}"],
                capture=True, timeout=10,
            )
            time.sleep(0.5)
            if os.path.isfile(out_path_unix):
                with open(out_path_unix, errors="replace") as qf:
                    content = qf.read()
                log("service-in-session sc query output:")
                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        log(f"  | {line}")
            else:
                log("service-in-session sc query: output file not created")
        except Exception as e:
            log(f"service-in-session sc query failed: {e}")

        # Diagnostic: native Linux probe of 127.0.0.1:9977. Wine doesn't have
        # its own network namespace -- it uses the host's loopback. So a
        # native socket.connect() from Python tests the same path the SDK
        # would take through Wine's winsock.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect(("127.0.0.1", 9977))
            s.close()
            log("service-in-session 9977 probe: CONNECTED (Linux side)")
        except Exception as e:
            log(f"service-in-session 9977 probe: FAILED -- {e}")

        # Also dump /proc/net/tcp filter to see established connections to
        # 9977 from the Gwent process tree. If the SDK ever connects, we'll
        # see it; if not, the SDK is never attempting to connect.
        try:
            r = subprocess.run(
                ["ss", "-tn", "state", "all", "sport", "=", ":9977"],
                capture_output=True, text=True, timeout=5,
            )
            log("service-in-session ss :9977 connections:")
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line:
                    log(f"  | {line}")
        except Exception as e:
            log(f"service-in-session ss check failed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _start_game_view_thread():
    """Spawn game-view snapshots: once shortly after Gwent starts, once
    30s later (after the user has tried to log in and seen the error).
    The second snapshot is the most diagnostic -- it includes Unity logs
    written during the failed auth."""
    def _multi():
        _snapshot_game_view(None)
        # Wait long enough for the user to see the failed login screen.
        time.sleep(30.0)
        _snapshot_game_view(None)
    t = threading.Thread(target=_multi, daemon=True)
    t.start()
    return t


def _start_snapshot_thread(stop_event):
    """Background thread: take a snapshot every 5s until stop_event is set."""
    def _run():
        # Take one immediately so the user can see the state right after
        # the launcher finishes its DNS setup.
        _snapshot_log()
        while not stop_event.wait(5.0):
            _snapshot_log()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _start_dnat_refresh_thread(stop_event, get_ip_set):
    """Background thread: every 10s, re-resolve the gog hosts and DNAT any
    newly-rotated Fastly IPs. Closes the root-cause gap where the SDK's
    auth.gog.com/token connected to a Fastly anycast IP that the launch-time
    DNAT snapshot had missed, escaping to real gog (-> invalid_grant ->
    SessionManager Connection lost). Narrow: only ever adds gog-derived IPs,
    so it cannot redirect unrelated websites. Shares the launcher's tracked
    IP set so cleanup removes every rule it adds."""
    def _run():
        while not stop_event.wait(10.0):
            ip_set = get_ip_set()
            if ip_set is None:
                continue
            try:
                refresh_iptables_dnat(ip_set)
            except Exception as e:
                log(f"dnat refresh thread: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


class GwentLinuxLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gwent Beta -- Private Server (Linux)")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)

        # Steam Deck native res is 1280x800; keep the window small.
        w, h = 540, 640
        sx = (self.winfo_screenwidth() - w) // 2
        sy = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{sx}+{sy}")

        self.cfg = load_config()
        self.comm_server = None
        self.runner = None
        self.game_proc = None
        # Proxies bind to ports 443/8445/7777/53 and must only start once per
        # launcher session. On subsequent Play clicks the threads from the
        # first match are still running -- starting new ones would crash with
        # Errno 98 (Address already in use) and the SDK would lose the broker
        # connection, falling back to the login screen.
        self._proxies_started = False
        self._snapshot_stop = None
        self._iptables_ips = set()  # IPs we DNAT'd, used for cleanup


        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.style.configure(
            "Gold.TButton",
            background=BTN_BG, foreground=BTN_FG,
            font=("Sans", 13, "bold"),
            padding=(40, 12),
        )
        self.style.map(
            "Gold.TButton",
            background=[("active", ACCENT_HOVER), ("disabled", "#555555")],
            foreground=[("disabled", "#888888")],
        )
        self.style.configure(
            "Gold.Horizontal.TProgressbar",
            troughcolor=PROGRESS_BG, background=PROGRESS_FG, thickness=18,
        )

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if "user_id" in self.cfg and self.cfg.get("gwent_path"):
            self.show_main_screen()
        else:
            self.show_setup_screen()

    # ── Setup screen ──────────────────────────────────────────────────────
    def show_setup_screen(self):
        self._clear()
        frame = tk.Frame(self, bg=BG_DARK)
        frame.pack(fill="both", expand=True, padx=30, pady=20)

        tk.Label(frame, text="GWENT", font=("Sans", 28, "bold"),
                 fg=FG_TITLE, bg=BG_DARK).pack(pady=(5, 0))
        tk.Label(frame, text="Private Server Setup (Linux)",
                 font=("Sans", 12), fg=FG_DIM, bg=BG_DARK).pack(pady=(0, 16))

        # Runner detection
        self.runner = detect_runner()
        runner_label = (
            f"Runner: {self.runner['label']} ({self.runner['kind']})"
            if self.runner else "Runner: NOT FOUND -- install wine"
        )
        runner_color = SUCCESS_FG if self.runner else ERROR_FG
        tk.Label(frame, text=runner_label, font=("Sans", 9),
                 fg=runner_color, bg=BG_DARK).pack(anchor="w")

        # Gwent path
        tk.Label(frame, text="Gwent Installation", font=("Sans", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w", pady=(10, 2))
        path_row = tk.Frame(frame, bg=BG_DARK)
        path_row.pack(fill="x")
        self.path_var = tk.StringVar(value=find_gwent() or "")
        tk.Entry(path_row, textvariable=self.path_var, font=("Sans", 10),
                 bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                 relief="flat", bd=0).pack(side="left", fill="x",
                                           expand=True, ipady=6, padx=(0, 6))
        tk.Button(path_row, text="Browse", font=("Sans", 9),
                  bg=BG_CARD, fg=FG_TEXT, relief="flat",
                  command=self.browse_gwent).pack(side="right", ipady=3, ipadx=8)
        self.path_status = tk.Label(frame, text="", font=("Sans", 8),
                                    fg=FG_DIM, bg=BG_DARK)
        self.path_status.pack(anchor="w", pady=(2, 0))
        if self.path_var.get():
            self.path_status.config(text="Found existing installation",
                                    fg=SUCCESS_FG)
        else:
            self.path_status.config(
                text="Not found -- click Browse and select your Gwent folder",
                fg=FG_DIM)

        # --- Server address: which server to connect to (user-supplied) ---
        tk.Label(frame, text="Server Address", font=("Sans", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w", pady=(10, 2))
        self.server_var = tk.StringVar(
            value=self.cfg.get("server_ip", "") or (SERVER_IP if SERVER_IP != "127.0.0.1" else ""))
        tk.Entry(frame, textvariable=self.server_var, font=("Sans", 10),
                 bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                 relief="flat", bd=0).pack(fill="x", ipady=6)
        tk.Label(frame,
                 text="Host or IP of the server you're joining (ask whoever runs it).",
                 font=("Sans", 8), fg=FG_DIM, bg=BG_DARK).pack(anchor="w", pady=(2, 0))

        # Wine prefix is always the bundled wine in DEFAULT_PREFIX now, so the
        # path field was removed (it only added clutter). Keep the var for
        # run_install, which still reads self.prefix_var.
        self.prefix_var = tk.StringVar(value=DEFAULT_PREFIX)

        # Auth mode: create a new account vs sign in to an existing one.
        self.auth_mode_var = tk.StringVar(value="create")
        mode_row = tk.Frame(frame, bg=BG_DARK)
        mode_row.pack(fill="x", pady=(10, 2))
        tk.Radiobutton(
            mode_row, text="Create account", variable=self.auth_mode_var,
            value="create", command=self._update_auth_mode,
            font=("Sans", 10), fg=FG_TEXT, bg=BG_DARK,
            selectcolor=BG_INPUT, activebackground=BG_DARK,
            activeforeground=FG_TEXT, highlightthickness=0,
        ).pack(side="left", padx=(0, 16))
        tk.Radiobutton(
            mode_row, text="Sign in", variable=self.auth_mode_var,
            value="signin", command=self._update_auth_mode,
            font=("Sans", 10), fg=FG_TEXT, bg=BG_DARK,
            selectcolor=BG_INPUT, activebackground=BG_DARK,
            activeforeground=FG_TEXT, highlightthickness=0,
        ).pack(side="left")

        # Username
        tk.Label(frame, text="Username", font=("Sans", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w", pady=(10, 2))
        # (User ID field is created after the username entry below.)
        self.username_var = tk.StringVar()
        name = tk.Entry(frame, textvariable=self.username_var,
                        font=("Sans", 12), bg=BG_INPUT, fg=FG_TEXT,
                        insertbackground=FG_TEXT, relief="flat", bd=0)
        name.pack(fill="x", ipady=8, pady=(0, 8))
        name.focus_set()

        # User ID -- required for Sign in only (hidden in Create mode). Acts as
        # a shared secret so opponents who see your username can't sign in.
        self.userid_var = tk.StringVar()
        self.userid_frame = tk.Frame(frame, bg=BG_DARK)
        tk.Label(self.userid_frame, text="User ID", font=("Sans", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w", pady=(0, 2))
        tk.Entry(self.userid_frame, textvariable=self.userid_var,
                 font=("Sans", 12), bg=BG_INPUT, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", bd=0).pack(
            fill="x", ipady=8)
        tk.Label(self.userid_frame,
                 text="The numeric ID shown on your account screen.",
                 font=("Sans", 8), fg=FG_DIM, bg=BG_DARK).pack(
            anchor="w", pady=(2, 8))
        # Hidden by default (Create mode); _update_auth_mode shows it for Sign in.

        # Full collection toggle
        self.full_collection_var = tk.BooleanVar(value=True)
        check_frame = tk.Frame(frame, bg=BG_DARK)
        check_frame.pack(fill="x", pady=(0, 6))
        self.collection_cb = tk.Checkbutton(
            check_frame,
            text="Start with full collection, max currencies & starter decks",
            variable=self.full_collection_var,
            font=("Sans", 10), fg=FG_TEXT, bg=BG_DARK,
            selectcolor=BG_INPUT, activebackground=BG_DARK,
            activeforeground=FG_TEXT, highlightthickness=0,
        )
        self.collection_cb.pack(anchor="w")
        tk.Label(check_frame,
                 text="Uncheck to start from scratch with no cards or decks",
                 font=("Sans", 8), fg=FG_DIM, bg=BG_DARK).pack(
            anchor="w", padx=(24, 0))
        # New-account-only options live in this frame; hidden in sign-in mode.
        self.new_account_frame = check_frame

        # Progress
        self.progress_frame = tk.Frame(frame, bg=BG_DARK)
        self.progress_frame.pack(fill="x", pady=(10, 0))
        self.status_label = tk.Label(self.progress_frame, text="",
                                     font=("Sans", 9), fg=FG_DIM, bg=BG_DARK)
        self.status_label.pack(anchor="w")
        self.progress_bar = ttk.Progressbar(
            self.progress_frame,
            style="Gold.Horizontal.TProgressbar",
            mode="determinate", length=460,
        )
        self.progress_bar.pack(fill="x", pady=(4, 0))
        self.progress_bar.pack_forget()

        self.install_btn = ttk.Button(
            frame, text="Install & Play",
            style="Gold.TButton",
            command=self.start_install,
        )
        self.install_btn.pack(pady=(14, 0), ipadx=20, ipady=4)
        # Gate the button on a non-empty username (NOT on runner detection --
        # if no wine is found, start_install() auto-installs it). Disabled
        # until the user types a username.
        self.username_var.trace_add("write", self._update_install_btn_state)
        self.userid_var.trace_add("write", self._update_install_btn_state)
        try:
            self.server_var.trace_add("write", self._update_install_btn_state)
        except Exception:
            pass
        self._update_install_btn_state()

    def _update_install_btn_state(self, *args):
        has_name = bool(self.username_var.get().strip())
        has_server = True
        try:
            has_server = bool(self.server_var.get().strip())
        except Exception:
            pass
        ok = has_name and has_server
        try:
            if self.auth_mode_var.get() == "signin":
                ok = has_name and bool(self.userid_var.get().strip())
        except Exception:
            pass
        try:
            self.install_btn.state(["!disabled"] if ok else ["disabled"])
        except Exception:
            pass

    def _update_auth_mode(self, *args):
        """Toggle new-account-only options and button label by auth mode."""
        signin = self.auth_mode_var.get() == "signin"
        try:
            if signin:
                self.new_account_frame.pack_forget()
                # Show the User ID field just before the progress UI.
                self.userid_frame.pack(fill="x", before=self.progress_frame)
                self.install_btn.config(text="Sign In & Play")
            else:
                self.userid_frame.pack_forget()
                # Re-pack the new-account options just before the progress UI.
                self.new_account_frame.pack(fill="x", pady=(0, 6),
                                            before=self.progress_frame)
                self.install_btn.config(text="Install & Play")
        except Exception:
            pass
        self._update_install_btn_state()

    def browse_gwent(self):
        p = filedialog.askdirectory(title="Select Gwent install folder")
        if p:
            if validate_gwent_install(p):
                self.path_var.set(p)
                self.path_status.config(text="Valid Gwent installation",
                                        fg=SUCCESS_FG)
            else:
                self.path_status.config(
                    text="No Gwent.exe found in that folder", fg=ERROR_FG)

    def set_status(self, text, color=FG_DIM):
        self.status_label.config(text=text, fg=color)
        self.update_idletasks()

    def show_progress(self):
        self.progress_bar.pack(fill="x", pady=(4, 0))
        self.progress_bar["value"] = 0
        self.update_idletasks()

    def update_progress(self, downloaded, total):
        if total > 0:
            pct = downloaded * 100 / total
            mb_done = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self.progress_bar["value"] = pct
            self.status_label.config(
                text=f"Downloading... {mb_done:.0f} / {mb_total:.0f} MB")
        else:
            mb_done = downloaded / (1024 * 1024)
            self.status_label.config(text=f"Downloading... {mb_done:.0f} MB")
        self.update_idletasks()

    def start_install(self):
        username = self.username_var.get().strip()
        if not username:
            self.set_status("Please enter a username.", ERROR_FG)
            return
        # Disable controls up front; the worker thread handles wine
        # auto-install (which can take 30-60s) so the UI stays responsive.
        self.install_btn.state(["disabled"])
        self.collection_cb.config(state="disabled")
        threading.Thread(target=self.run_install, args=(username,),
                         daemon=True).start()

    def _set_status_threadsafe(self, msg, color=ERROR_FG):
        self.after(0, lambda: self.set_status(msg, color))

    def run_install(self, username):
        try:
            # Account register/login must target the ACTUAL server the user
            # entered, NOT the baked-in SERVER_URL (which is 127.0.0.1 in a
            # neutral build). The DNS/proxy redirect isn't up yet during install,
            # so we hit the server's real IP directly. (Cross-machine clients
            # otherwise get errno 111 connecting to localhost.)
            _entered = (self.server_var.get().strip() if hasattr(self, "server_var") else "")
            reg_url = ("https://" + _entered) if _entered else SERVER_URL
            # Sign-in fast path: if signing in to an existing account and this
            # machine already has a completed install (valid game + an existing
            # prefix), skip all the one-time setup (wine install, 2.3 GB
            # download, prefix/cert/service, MelonLoader, mod) -- just
            # authenticate and go. This avoids re-running setup after a
            # sign-out/sign-in on an already-configured device.
            if self.auth_mode_var.get() == "signin":
                cfg_path = self.cfg.get("gwent_path") or self.path_var.get().strip()
                cfg_prefix = self.cfg.get("prefix") or DEFAULT_PREFIX
                if (cfg_path and validate_gwent_install(cfg_path)
                        and os.path.isdir(cfg_prefix)):
                    self.after(0, self.set_status, "Signing in...")
                    raw_id = self.userid_var.get().strip()
                    try:
                        claimed_id = int(raw_id)
                    except ValueError:
                        self.after(0, self.install_error,
                                   "User ID must be a number.")
                        return
                    user_id, result = login_user(username, claimed_id, server_url=reg_url)
                    if user_id is None:
                        self.after(0, self.install_error,
                                   f"Sign-in failed: {result}")
                        return
                    create_users_json(user_id, result)
                    self.cfg["user_id"] = user_id
                    self.cfg["username"] = result
                    self.cfg["server_ip"] = self.server_var.get().strip() or SERVER_IP
                    save_config(self.cfg)
                    self.after(0, self.show_main_screen)
                    return

            # We ALWAYS need flatpak wine. run_install only runs during
            # first-time setup (the configured "Play" path skips it), so just
            # install wine unconditionally here -- don't trust runner detection
            # to decide. `flatpak install` is idempotent: a no-op if wine is
            # already present, a real install if not. This avoids the whole
            # class of detection false-positives (e.g. `flatpak info` reporting
            # an app that's merely known to a remote but not installed).
            self._set_status_threadsafe("Installing Wine (one-time)...")
            ok, msg = install_flatpak_wine(
                status_cb=lambda m: self._set_status_threadsafe(m))
            log(f"run_install: install_flatpak_wine -> ok={ok} msg={msg}")
            # Re-detect now that wine should be present.
            self.runner = detect_runner(
                status_cb=lambda m: self._set_status_threadsafe(m))
            if not self.runner:
                self._set_status_threadsafe(
                    "Wine is required and could not be installed "
                    "automatically. Install it with: "
                    "flatpak install flathub org.winehq.Wine")
                self.after(0, lambda: self.install_btn.state(["!disabled"]))
                self.after(0, lambda: self.collection_cb.config(
                    state="normal"))
                return
            # Step 1: Gwent install path
            # The game client is NOT distributed by this installer. The user
            # must point at their own legally-obtained Gwent 0.9.24.3 install.
            gwent_path = self.path_var.get().strip()
            if not gwent_path or not validate_gwent_install(gwent_path):
                self.after(0, self.install_error,
                           "No valid Gwent 0.9.24.3 installation found. Click "
                           "'Browse' and select your own Gwent install folder. "
                           "This installer does not download the game.")
                return

            # Step 2: Prefix + cert
            prefix_dir = self.prefix_var.get().strip() or DEFAULT_PREFIX
            self.after(0, self.set_status,
                       "Preparing Wine prefix (one-time)...")
            ensure_wine_prefix(self.runner, prefix_dir,
                               status_cb=lambda t: self.after(0,
                                                              self.set_status, t))
            install_cert_into_prefix(
                self.runner, prefix_dir,
                status_cb=lambda t: self.after(0, self.set_status, t),
            )
            install_dummy_service_in_prefix(
                self.runner, prefix_dir,
                status_cb=lambda t: self.after(0, self.set_status, t),
            )

            # Step 3: MelonLoader
            try:
                self.after(0, self.show_progress)
                install_melonloader(
                    gwent_path,
                    status_cb=lambda t: self.after(0, self.set_status, t),
                    progress_cb=lambda d, t: self.after(
                        0, self.update_progress, d, t),
                )
            except Exception as e:
                self.after(0, self.install_error,
                           f"MelonLoader install failed: {e}")
                return

            # Step 4: Mod + settings
            self.after(0, self.set_status, "Installing mod...")
            if not install_mod_dll(gwent_path):
                self.after(0, self.install_error,
                           "Mod DLL not found in bundle.")
                return
            install_settings(gwent_path)
            fix_game_file_owner(gwent_path)

            # Step 5: Register a new account OR sign in to an existing one.
            if self.auth_mode_var.get() == "signin":
                self.after(0, self.set_status, "Signing in...")
                raw_id = self.userid_var.get().strip()
                try:
                    claimed_id = int(raw_id)
                except ValueError:
                    self.after(0, self.install_error,
                               "User ID must be a number.")
                    return
                user_id, result = login_user(username, claimed_id, server_url=reg_url)
                if user_id is None:
                    self.after(0, self.install_error,
                               f"Sign-in failed: {result}")
                    return
            else:
                self.after(0, self.set_status, "Registering with server...")
                full_coll = self.full_collection_var.get()
                user_id, result = register_user(username, full_coll, server_url=reg_url)
                if user_id is None:
                    self.after(0, self.install_error,
                               f"Registration failed: {result}")
                    return
            username = result
            create_users_json(user_id, username)

            # Step 6: Save config
            self.cfg["gwent_path"] = gwent_path
            self.cfg["prefix"] = prefix_dir
            self.cfg["user_id"] = user_id
            self.cfg["username"] = username
            self.cfg["server_ip"] = self.server_var.get().strip() or SERVER_IP
            self.cfg["version"] = VERSION
            self.cfg["runner_kind"] = self.runner["kind"]
            self.cfg["runner_label"] = self.runner["label"]
            save_config(self.cfg)

            self.after(0, self.show_main_screen)
        except Exception as e:
            self.after(0, self.install_error, str(e))

    def install_error(self, msg):
        self.set_status(msg, ERROR_FG)
        self.install_btn.state(["!disabled"])
        self.collection_cb.config(state="normal")
        try:
            self.progress_bar.stop()
        except Exception:
            pass

    # ── Main screen ───────────────────────────────────────────────────────
    def show_main_screen(self):
        self._clear()
        if not self.runner:
            self.runner = detect_runner()

        frame = tk.Frame(self, bg=BG_DARK)
        frame.pack(fill="both", expand=True, padx=30, pady=20)
        tk.Label(frame, text="GWENT", font=("Sans", 32, "bold"),
                 fg=FG_TITLE, bg=BG_DARK).pack(pady=(10, 0))
        tk.Label(frame, text="The Witcher Card Game",
                 font=("Sans", 11), fg=FG_DIM, bg=BG_DARK).pack(pady=(0, 5))
        tk.Label(frame, text="0.9.24 Open Beta -- Private Server (Linux)",
                 font=("Sans", 9), fg=FG_DIM, bg=BG_DARK).pack(pady=(0, 15))

        username = self.cfg.get("username", "Player")
        info_frame = tk.Frame(frame, bg=BG_CARD, bd=0, highlightthickness=1,
                              highlightbackground="#2a2a4a")
        info_frame.pack(fill="x", pady=(0, 12), ipady=6, ipadx=15)
        info_top = tk.Frame(info_frame, bg=BG_CARD)
        info_top.pack(fill="x", padx=15, pady=(8, 2))
        self.welcome_label = tk.Label(info_top, text=f"Welcome, {username}",
                                      font=("Sans", 12),
                                      fg=FG_TEXT, bg=BG_CARD)
        self.welcome_label.pack(side="left")
        tk.Button(info_top, text="Sign Out", font=("Sans", 8),
                  bg=BG_INPUT, fg=FG_DIM, relief="flat",
                  command=self.sign_out).pack(side="right")
        tk.Button(info_top, text="Change", font=("Sans", 8),
                  bg=BG_INPUT, fg=FG_DIM, relief="flat",
                  command=self.show_change_username).pack(side="right",
                                                          padx=(0, 6))
        # User ID row -- needed to sign in on another device. Show + Copy.
        uid_row = tk.Frame(info_frame, bg=BG_CARD)
        uid_row.pack(fill="x", padx=15, pady=(0, 8))
        _uid = self.cfg.get("user_id", "")
        tk.Label(uid_row, text=f"User ID: {_uid}",
                 font=("Sans", 9), fg=FG_DIM, bg=BG_CARD).pack(side="left")
        tk.Button(uid_row, text="Copy", font=("Sans", 8),
                  bg=BG_INPUT, fg=FG_DIM, relief="flat",
                  command=self._copy_user_id).pack(side="right")

        runner_text = (
            f"Runner: {self.runner['label']} ({self.runner['kind']})"
            if self.runner else "Runner: NOT FOUND"
        )
        tk.Label(info_frame, text=runner_text,
                 font=("Sans", 9), fg=FG_DIM, bg=BG_CARD).pack(
            anchor="w", padx=15, pady=(0, 8))

        self.launch_status = tk.Label(frame, text="", font=("Sans", 9),
                                      fg=FG_DIM, bg=BG_DARK)
        self.launch_status.pack(pady=(0, 8))

        self.play_btn = tk.Button(
            frame, text="P L A Y", font=("Sans", 14, "bold"),
            bg=BTN_BG, fg=BTN_FG, activebackground=ACCENT_HOVER,
            activeforeground=BTN_FG, relief="flat", cursor="hand2",
            padx=50, pady=10, command=self.start_game,
        )
        self.play_btn.pack(pady=(0, 10))
        if not self.runner:
            self.play_btn.config(state="disabled", bg="#555555")

    def _copy_user_id(self):
        """Copy the current user_id to the clipboard."""
        try:
            self.clipboard_clear()
            self.clipboard_append(str(self.cfg.get("user_id", "")))
            self.launch_status.config(text="User ID copied to clipboard.",
                                      fg=SUCCESS_FG)
        except Exception:
            pass

    def sign_out(self):
        """Clear the cached identity and return to the setup/sign-in screen.

        Keeps gwent_path/prefix/runner so the user doesn't reinstall; only the
        account identity (user_id/username) is dropped. The single-user
        users.json is rewritten on the next sign-in/register via
        create_users_json, preserving the single-user commservice invariant.
        """
        for k in ("user_id", "username"):
            self.cfg.pop(k, None)
        save_config(self.cfg)
        try:
            if os.path.exists(USERS_JSON):
                os.remove(USERS_JSON)
        except Exception:
            pass
        self.show_setup_screen()

    # ── Username change ───────────────────────────────────────────────────
    def show_change_username(self):
        dialog = tk.Toplevel(self)
        dialog.title("Change Username")
        dialog.configure(bg=BG_DARK)
        dialog.resizable(False, False)
        w, h = 350, 200
        sx = self.winfo_x() + (self.winfo_width() - w) // 2
        sy = self.winfo_y() + (self.winfo_height() - h) // 2
        dialog.geometry(f"{w}x{h}+{sx}+{sy}")
        dialog.transient(self)
        dialog.grab_set()
        dialog.focus_force()

        tk.Label(dialog, text="New Username", font=("Sans", 10),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w", padx=20, pady=(15, 2))
        name_var = tk.StringVar(value=self.cfg.get("username", ""))
        name_entry = tk.Entry(dialog, textvariable=name_var,
                              font=("Sans", 12), bg=BG_INPUT, fg=FG_TEXT,
                              insertbackground=FG_TEXT, relief="flat", bd=0)
        name_entry.pack(fill="x", padx=20, ipady=6)
        name_entry.select_range(0, tk.END)
        name_entry.focus_set()

        status = tk.Label(dialog, text="", font=("Sans", 9),
                          fg=FG_DIM, bg=BG_DARK)
        status.pack(pady=(5, 0))

        save_btn = tk.Button(dialog, text="Save", font=("Sans", 10, "bold"),
                             bg=BTN_BG, fg=BTN_FG, relief="flat",
                             cursor="hand2",
                             activebackground=ACCENT_HOVER,
                             padx=20, pady=4)
        save_btn.pack(pady=(10, 0))

        def do_change():
            new_name = name_var.get().strip()
            if not new_name:
                status.config(text="Username cannot be empty.", fg=ERROR_FG)
                return
            if new_name == self.cfg.get("username"):
                dialog.destroy()
                return
            save_btn.config(state="disabled")
            status.config(text="Saving...", fg=FG_DIM)

            def _do():
                ok, result = change_username(self.cfg["user_id"], new_name)

                def _apply():
                    if ok:
                        self.cfg["username"] = result
                        save_config(self.cfg)
                        create_users_json(self.cfg["user_id"], result)
                        self.welcome_label.config(text=f"Welcome, {result}")
                        dialog.destroy()
                    else:
                        status.config(text=result, fg=ERROR_FG)
                        save_btn.config(state="normal")
                self.after(0, _apply)
            threading.Thread(target=_do, daemon=True).start()

        save_btn.config(command=do_change)
        name_entry.bind("<Return>", lambda e: do_change())

    # ── Game launch ───────────────────────────────────────────────────────
    def start_game(self):
        self.play_btn.config(state="disabled", bg="#555555")
        self.launch_status.config(text="Starting...", fg=FG_DIM)
        threading.Thread(target=self.run_game, daemon=True).start()

    def run_game(self):
        # SteamOS: drop the read-only root protection so we can edit
        # /etc/resolv.conf and /etc/hosts. No-op elsewhere.
        disable_steamos_readonly()

        gwent_path = self.cfg.get("gwent_path", "")
        gwent_exe = os.path.join(gwent_path, "Gwent.exe")
        prefix_dir = self.cfg.get("prefix", DEFAULT_PREFIX)
        server_ip = self.cfg.get("server_ip", SERVER_IP)

        if not os.path.isfile(gwent_exe):
            self.after(0, lambda: self.launch_status.config(
                text="Gwent.exe not found -- reinstall required", fg=ERROR_FG))
            self.after(0, lambda: self.play_btn.config(state="normal",
                                                      bg=BTN_BG))
            return

        try:
            # Always refresh mod DLL + settings.
            install_mod_dll(gwent_path)
            install_settings(gwent_path)
            fix_game_file_owner(gwent_path)
            # Make sure GalaxyCommunication service is registered + running
            # inside the prefix. Idempotent -- re-create errors are ignored.
            install_dummy_service_in_prefix(self.runner, prefix_dir)

            # ── HTTPS / broker / relay proxies ──────────────────────────
            if not self._proxies_started:
                # Resolve the server name to an IP ONCE here, while normal DNS is
                # still active (before write_resolv_localhost() below repoints it
                # at our DNS proxy). All proxies then connect by IP and never
                # depend on our own redirect to resolve the server hostname.
                # Falls back to the hostname if resolution fails (no regression).
                proxy_target = _resolve_to_ip(server_ip)

                self.after(0, lambda: self.launch_status.config(
                    text="Starting local HTTPS proxy...", fg=FG_DIM))
                try:
                    start_https_proxy_thread(CERT_FILE, KEY_FILE, proxy_target)
                    time.sleep(0.3)
                except Exception as e:
                    self.after(0, lambda: self.launch_status.config(
                        text=f"HTTPS proxy failed: {e}", fg=ERROR_FG))
                    return

                try:
                    start_broker_proxy_thread(CERT_FILE, KEY_FILE, proxy_target)
                    time.sleep(0.1)
                except Exception as e:
                    log(f"broker proxy failed: {e}")
                try:
                    start_relay_proxy_thread(proxy_target)
                    time.sleep(0.1)
                except Exception as e:
                    log(f"relay proxy failed: {e}")
            else:
                log("Proxies already running from previous match -- skipping start")

            # ── DNS setup ───────────────────────────────────────────────
            # The DNS PROXY THREAD is started once (it holds port 53; restarting
            # would hit Errno 98). But the actual REDIRECT STATE -- resolv.conf,
            # /etc/hosts, and the iptables DNAT -- is TORN DOWN by the cleanup
            # finally block after every match (restore_resolv/restore_hosts/
            # uninstall_iptables_dnat). So it MUST be re-applied on EVERY launch,
            # not just the first, or the 2nd launch has no redirect and the SDK's
            # auth.gog.com/token escapes to real gog -> 400 invalid_grant ->
            # "wrong username / version not supported". (This was exactly the
            # 2nd-launch bug.) Hence the resolv/hosts/DNAT calls below are
            # OUTSIDE the _proxies_started guard.
            if not self._proxies_started:
                self.after(0, lambda: self.launch_status.config(
                    text="Configuring DNS...", fg=FG_DIM))
                upstream = detect_upstream_dns()
                stop_systemd_resolved()
                try:
                    start_dns_proxy_thread("127.0.0.1", upstream)
                    time.sleep(0.4)
                except Exception as e:
                    self.after(0, lambda: self.launch_status.config(
                        text=f"DNS proxy failed: {e}", fg=ERROR_FG))
                    return

            # --- Re-apply redirects every launch (cleanup removed them) ---
            if not write_resolv_localhost():
                log("resolv.conf write/verify failed -- falling back "
                    "to /etc/hosts entries only.")
                self.after(0, lambda: self.launch_status.config(
                    text="resolv.conf locked; using /etc/hosts fallback",
                    fg=FG_DIM))
            else:
                mark_dns_modified()
            # /etc/hosts is the most reliable redirect on Steam Deck --
            # NetworkManager doesn't touch it and libc reads it before
            # contacting any DNS server. Always write our entries here
            # in addition to the DNS proxy approach.
            if not write_hosts_entries():
                self.after(0, lambda: self.launch_status.config(
                    text="WARNING: could not write /etc/hosts",
                    fg=ERROR_FG))

            # iptables DNAT fallback: redirect any TCP-443 destined for
            # the REAL gog.com IPs back to 127.0.0.1. This is what
            # actually saves us on the Steam Deck -- /etc/hosts and
            # /etc/resolv.conf get bypassed by Wine/Mono's resolver in
            # some cases, but iptables operates below all of that and
            # can't be defeated short of someone flushing the rules.
            self.after(0, lambda: self.launch_status.config(
                text="Installing iptables redirects...", fg=FG_DIM))
            try:
                self._iptables_ips = install_iptables_dnat()
                log(f"DNAT installed for {len(self._iptables_ips)} IPs")
            except Exception as e:
                log(f"install_iptables_dnat failed: {e}")

            # ── commservice ─────────────────────────────────────────────
            if not self._proxies_started:
                self.after(0, lambda: self.launch_status.config(
                    text="Starting auth service...", fg=FG_DIM))
                try:
                    self.comm_server = start_commservice_thread()
                except Exception as e:
                    self.after(0, lambda: self.launch_status.config(
                        text=f"Auth service failed: {e}", fg=ERROR_FG))
                # Mark everything started so subsequent Play clicks skip.
                self._proxies_started = True

            # Start the periodic state snapshot logger. Survives until the
            # game exits so we can audit what /etc/resolv.conf and /etc/hosts
            # actually looked like during the session.
            self._snapshot_stop = threading.Event()
            _start_snapshot_thread(self._snapshot_stop)
            # Keep the gog DNAT current as Fastly rotates anycast IPs, so the
            # SDK's auth call can never reach a real-gog IP we missed at launch.
            _start_dnat_refresh_thread(self._snapshot_stop,
                                       lambda: self._iptables_ips)

            # Sanity: are key ports listening?
            for port in (53, 443, 9977, 8445, 7777):
                if not wait_port_open("127.0.0.1", port, timeout=2.0):
                    log(f"WARN: 127.0.0.1:{port} not listening after start")

            # ── Launch Gwent under Wine ────────────────────────────────
            self.after(0, lambda: self.launch_status.config(
                text=f"Launching Gwent via {self.runner['label']}...",
                fg=SUCCESS_FG))
            self.after(0, self.iconify)

            env = prefix_env(self.runner, prefix_dir)
            if self.runner["kind"] == "flatpak-wine":
                fix_flatpak_runtime_perms()
                fix_prefix_owner(prefix_dir)
                cmd = flatpak_wine_cmd(env) + [gwent_exe]
            else:
                cmd = list(self.runner["cmd"]) + [gwent_exe]
            log(f"Launching: {' '.join(cmd)}")
            # Capture Gwent/wine stdout+stderr so we can see WHY the game exits
            # (e.g. flatpak wine missing GPU/display, DXVK errors). Written to
            # a file we can read after the session.
            game_out_path = os.path.join(DATA_DIR, "gwent_run.log")
            try:
                game_out = open(game_out_path, "w")
            except Exception:
                game_out = None
            self.game_proc = subprocess.Popen(
                cmd, cwd=gwent_path, env=env,
                stdout=(game_out or subprocess.DEVNULL),
                stderr=subprocess.STDOUT)
            _start_game_view_thread()
            # Do NOT run service-in-session under flatpak. ANY second
            # `flatpak run org.winehq.Wine ...` (even delayed) tears down
            # Gwent's instance -> rc=137. Flatpak refuses to share one
            # wineserver across two app instances the way plain wine does.
            # The service is created+started pre-launch; the SDK must find it
            # RUNNING via the dummy. (If it doesn't, the dummy itself is the
            # problem -- see notes; service-in-session can't help here.)
            if self.runner["kind"] != "flatpak-wine":
                _start_service_in_game_session(self.runner, prefix_dir)
            rc = self.game_proc.wait()
            log(f"Gwent.exe exited rc={rc}")
            try:
                if game_out:
                    game_out.flush(); game_out.close()
                with open(game_out_path, errors="replace") as gf:
                    tail = gf.read()[-3000:]
                log("---- gwent_run.log (tail) ----\n" + tail +
                    "\n---- end gwent_run.log ----")
            except Exception as e:
                log(f"could not read gwent_run.log: {e}")
        except Exception as e:
            log(f"run_game exception: {e}")
            self.after(0, lambda: self.launch_status.config(
                text=f"Error: {e}", fg=ERROR_FG))
        finally:
            # Stop snapshot logging.
            if self._snapshot_stop is not None:
                self._snapshot_stop.set()
            # NOTE: we intentionally DO NOT shut down commservice or any of the
            # proxy threads here. They stay alive for the lifetime of the
            # launcher process so subsequent matches can reuse the same ports
            # without hitting Errno 98 (Address already in use). They are all
            # daemon threads and will be cleaned up when the launcher exits.
            restore_resolv()
            restore_hosts()
            try:
                if self._iptables_ips:
                    uninstall_iptables_dnat(self._iptables_ips)
                    self._iptables_ips = set()
            except Exception as e:
                log(f"uninstall_iptables_dnat failed: {e}")
            # Don't restart systemd-resolved either -- our dns_proxy is still
            # holding port 53 for the next match.
            self.after(0, self.deiconify)
            self.after(0, lambda: self.launch_status.config(
                text="Gwent closed", fg=FG_DIM))
            self.after(0, lambda: self.play_btn.config(state="normal",
                                                      bg=BTN_BG))

    # ── Helpers ───────────────────────────────────────────────────────────
    def _clear(self):
        for w in self.winfo_children():
            w.destroy()

    def on_close(self):
        if self.comm_server:
            try:
                self.comm_server.shutdown()
            except Exception:
                pass
        if self.game_proc and self.game_proc.poll() is None:
            try:
                self.game_proc.terminate()
            except Exception:
                pass
        restore_resolv()
        restore_hosts()
        try:
            if self._iptables_ips:
                uninstall_iptables_dnat(self._iptables_ips)
                self._iptables_ips = set()
        except Exception:
            pass
        start_systemd_resolved()
        mark_dns_restored()
        self.destroy()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ensure_data_dir()
    ensure_root()
    # Self-heal any redirect state stranded by a previous hard kill before
    # we set anything up. No-op when nothing was left behind.
    reconcile_stale_redirects()
    app = GwentLinuxLauncher()
    app.mainloop()


if __name__ == "__main__":
    main()
