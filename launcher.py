#!/usr/bin/env python3
"""
Gwent Beta Private Server - One-Click Installer & Launcher

Single exe (via PyInstaller) with a tkinter GUI. Handles:
  - First run: find/download Gwent, install MelonLoader + mod, register user,
    install dummy service, trust cert, install VC++ redist
  - Every launch: run commservice + DNS proxy in-process (threads), launch Gwent,
    cleanup on exit
  - No Python installation required on the target machine

Requires Administrator privileges (for service install and DNS config).
"""

import atexit
import ctypes
import json
import os
import signal
import shutil
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
VERSION = "1.0.0"


def _resolve_server_host():
    """Server host/IP, resolved at runtime so it is never hardcoded in source.

    Resolution order (first hit wins):
      1. GWENT_SERVER_HOST environment variable.
      2. A bundled `server_host.txt` (added at build time from your private
         server.txt; see build_launcher.bat / build_launcher_linux.sh).
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


def _is_local_server(host):
    """True if `host` refers to THIS machine (so the user is hosting + playing on
    the same PC). In that case the local server stack (nginx + server/broker/relay)
    already listens on every port the game needs, and the client must NOT start its
    own proxies or they collide on 443/7777/8445/8447 (WinError 10061)."""
    import socket as _socket
    h = (host or "").strip().lower()
    if h in ("127.0.0.1", "localhost", "::1", ""):
        return True
    try:
        own = set()
        own.add(_socket.gethostbyname(_socket.gethostname()))
        # Also the primary outbound-route IP.
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        own.add(s.getsockname()[0])
        s.close()
        return h in own
    except Exception:
        return False
MELONLOADER_URL = "https://github.com/LavaGang/MelonLoader/releases/download/v0.5.7/MelonLoader.x64.zip"
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

# Hide all subprocess console windows
_SW_HIDE = subprocess.CREATE_NO_WINDOW

# GitHub mod update (placeholder)
GITHUB_MOD_RELEASE_URL = ""  # Set this when repo is ready
GITHUB_MOD_DLL_NAME = "GwentBetaRestorationMod.dll"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.argv[0])))
EXE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))


def _appdata_data_dir():
    """Per-user data dir: %USERPROFILE%\AppData\LocalLow\CDProjektRED\Gwent\Gwent Beta Launcher.

    LocalLow has no dedicated environment variable, so derive it from
    USERPROFILE (falling back to the parent of LOCALAPPDATA, then a temp dir).
    Keeping launcher state out of the exe folder means the exe can run in place
    from anywhere with no install-location prompt.
    """
    base = os.environ.get("USERPROFILE")
    if not base:
        local = os.environ.get("LOCALAPPDATA")
        base = os.path.dirname(local) if local else os.path.expanduser("~")
    return os.path.join(base, "AppData", "LocalLow", "CDProjektRED",
                        "Gwent", "Gwent Beta Launcher")


DATA_DIR = _appdata_data_dir()

CONFIG_FILE = os.path.join(DATA_DIR, "gwent_launcher.json")
CERT_FILE = os.path.join(BUNDLE_DIR, "fake.crt")
KEY_FILE = os.path.join(BUNDLE_DIR, "fake.key")
GALAXY_COMM_EXE = os.path.join(BUNDLE_DIR, "GalaxyCommunication.exe")
MOD_DLL = os.path.join(BUNDLE_DIR, "GwentBetaRestorationMod.dll")
USERS_JSON = os.path.join(DATA_DIR, "users.json")
LOG_FILE = os.path.join(DATA_DIR, "launcher_debug.log")


def ensure_data_dir():
    """Create the data subdirectory if it doesn't exist."""
    os.makedirs(DATA_DIR, exist_ok=True)


# Bundled Python scripts (imported in-process, not spawned)
COMMSERVICE_PY = os.path.join(BUNDLE_DIR, "commservice.py")
DNS_PROXY_PY = os.path.join(BUNDLE_DIR, "dns_proxy.py")
HTTPS_PROXY_PY = os.path.join(BUNDLE_DIR, "https_proxy.py")

# Game settings files (copied to Gwent_Data/StreamingAssets/Settings/)
SETTINGS_CONFIG = os.path.join(BUNDLE_DIR, "settings", "config.json")
SETTINGS_LAUNCH = os.path.join(BUNDLE_DIR, "settings", "Launch.cfg")

# Service
SERVICE_DIR = r"C:\ProgramData\GOG.com\Galaxy\redists"
SERVICE_PATH = os.path.join(SERVICE_DIR, "GalaxyCommunication.exe")
SERVICE_NAME = "GalaxyCommunication"
GALAXY_CACHE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    r"GOG.com\Galaxy\Applications\48242550540196492\RemoteConfigCache"
)
SERVICE_DACL = (
    "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)"
    "(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)"
    "(A;;CCLCSWLOCRRC;;;IU)"
    "(A;;CCLCSWLOCRRC;;;SU)"
    "(A;;RPWPDTLO;;;S-1-1-0)"
)

GWENT_SEARCH_PATHS = [
    r"C:\Program Files (x86)\GOG Galaxy\Games\Gwent The Witcher Card Game",
    r"C:\Program Files\GOG Galaxy\Games\Gwent The Witcher Card Game",
    r"C:\GOG Games\Gwent The Witcher Card Game",
    r"D:\GOG Games\Gwent The Witcher Card Game",
    r"D:\Games\Gwent The Witcher Card Game",
    r"E:\GOG Games\Gwent The Witcher Card Game",
    r"C:\Program Files (x86)\Gwent The Witcher Card Game",
    r"D:\Program Files (x86)\GOG Galaxy\Games\Gwent The Witcher Card Game",
]

# ---------------------------------------------------------------------------
# Theme colors
# ---------------------------------------------------------------------------
BG_DARK = "#1a1a2e"
BG_CARD = "#16213e"
BG_INPUT = "#0f3460"
FG_TEXT = "#e0e0e0"
FG_DIM = "#8888aa"
FG_TITLE = "#e8d5a3"  # Gold
ACCENT = "#c9a84c"
ACCENT_HOVER = "#dfc06a"
BTN_BG = "#c9a84c"
BTN_FG = "#1a1a2e"
ERROR_FG = "#ff6b6b"
SUCCESS_FG = "#51cf66"
PROGRESS_BG = "#0f3460"
PROGRESS_FG = "#c9a84c"


# ---------------------------------------------------------------------------
# Utility functions (no GUI)
# ---------------------------------------------------------------------------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def ssl_context():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def load_config():
    ensure_data_dir()
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def save_config(cfg):
    ensure_data_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def validate_gwent_install(path):
    gwent_exe = os.path.join(path, "Gwent.exe")
    asm_dll = os.path.join(path, "Gwent_Data", "Managed", "Assembly-CSharp.dll")
    return os.path.isfile(gwent_exe) and os.path.isfile(asm_dll)


def find_gwent():
    for path in GWENT_SEARCH_PATHS:
        if validate_gwent_install(path):
            return path
    return None


def is_melonloader_installed(gwent_path):
    ml_dir = os.path.join(gwent_path, "MelonLoader")
    version_dll = os.path.join(gwent_path, "version.dll")
    return os.path.isdir(ml_dir) and os.path.isfile(version_dll)


def clear_galaxy_cache():
    if os.path.isdir(GALAXY_CACHE_DIR):
        try:
            shutil.rmtree(GALAXY_CACHE_DIR)
        except Exception:
            pass


def is_vcredist_installed():
    """Check if Visual C++ 2015-2022 redistributable is installed."""
    sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    return (os.path.isfile(os.path.join(sys32, "MSVCP140.dll")) and
            os.path.isfile(os.path.join(sys32, "VCRUNTIME140.dll")))


def install_vcredist(progress_callback=None):
    """Download and silently install the VC++ 2015-2022 redistributable."""
    tmp_path = os.path.join(os.environ.get("TEMP", "."), "vc_redist.x64.exe")
    download_file(VCREDIST_URL, tmp_path, progress_callback=progress_callback)
    # /install /quiet /norestart - silent install
    # Do NOT use CREATE_NO_WINDOW here - the installer needs a proper session
    ret = subprocess.run(
        [tmp_path, "/install", "/quiet", "/norestart"],
        capture_output=True
    )
    try:
        os.remove(tmp_path)
    except Exception:
        pass
    # 0 = success, 3010 = success but reboot needed, 1638 = newer version already installed
    return ret.returncode in (0, 3010, 1638)


def install_dummy_service():
    """Register the GalaxyCommunication service if it doesn't exist at all."""
    ret = subprocess.run(["sc", "query", SERVICE_NAME],
                         capture_output=True, text=True, creationflags=_SW_HIDE)
    if ret.returncode == 0:
        # Service already exists (GOG Galaxy or previous install) - just set DACL
        subprocess.run(["sc", "sdset", SERVICE_NAME, SERVICE_DACL],
                       capture_output=True, creationflags=_SW_HIDE)
        return
    # Service doesn't exist - create it with our dummy exe
    os.makedirs(SERVICE_DIR, exist_ok=True)
    if os.path.exists(GALAXY_COMM_EXE):
        shutil.copy2(GALAXY_COMM_EXE, SERVICE_PATH)
    elif not os.path.exists(SERVICE_PATH):
        return
    subprocess.run(["sc", "create", SERVICE_NAME, f"binpath={SERVICE_PATH}"],
                   capture_output=True, creationflags=_SW_HIDE)
    subprocess.run(["sc", "sdset", SERVICE_NAME, SERVICE_DACL],
                   capture_output=True, creationflags=_SW_HIDE)


BACKUP_PATH = os.path.join(SERVICE_DIR, "GalaxyCommunication.exe.gog_backup")


def swap_service_binary():
    """Stop the service, swap GOG's exe for our dummy, set DACL.
    Returns True if a backup was made (GOG's exe was present and different)."""
    log_path = LOG_FILE

    # Stop the service first
    subprocess.run(["sc", "stop", SERVICE_NAME],
                   capture_output=True, creationflags=_SW_HIDE)
    # Also kill any lingering GalaxyCommunication processes
    subprocess.run(["taskkill", "/F", "/IM", "GalaxyCommunication.exe"],
                   capture_output=True, creationflags=_SW_HIDE)
    time.sleep(1.0)

    os.makedirs(SERVICE_DIR, exist_ok=True)

    # Check if the current exe is GOG's (different from ours)
    made_backup = False
    if os.path.exists(SERVICE_PATH) and not os.path.exists(BACKUP_PATH):
        try:
            current_size = os.path.getsize(SERVICE_PATH)
            our_size = os.path.getsize(GALAXY_COMM_EXE) if os.path.exists(GALAXY_COMM_EXE) else 0
            with open(log_path, "a") as f:
                f.write(f"[swap] Current exe size: {current_size}, our dummy size: {our_size}\n")
            if current_size != our_size:
                shutil.copy2(SERVICE_PATH, BACKUP_PATH)
                made_backup = True
                with open(log_path, "a") as f:
                    f.write(f"[swap] Backed up GOG's exe to {BACKUP_PATH}\n")
        except Exception as e:
            with open(log_path, "a") as f:
                f.write(f"[swap] Backup failed: {e}\n")

    # Copy our dummy exe into place
    if os.path.exists(GALAXY_COMM_EXE):
        try:
            shutil.copy2(GALAXY_COMM_EXE, SERVICE_PATH)
            new_size = os.path.getsize(SERVICE_PATH)
            with open(log_path, "a") as f:
                f.write(f"[swap] Copied dummy exe, new size: {new_size}\n")
        except Exception as e:
            with open(log_path, "a") as f:
                f.write(f"[swap] Copy FAILED: {e}\n")
    else:
        with open(log_path, "a") as f:
            f.write(f"[swap] Dummy exe not found at {GALAXY_COMM_EXE}\n")

    # Ensure DACL is set
    subprocess.run(["sc", "sdset", SERVICE_NAME, SERVICE_DACL],
                   capture_output=True, creationflags=_SW_HIDE)

    return made_backup


def restore_service_binary():
    """Restore GOG's original exe if we backed it up, and restart the service."""
    # Stop our dummy service
    subprocess.run(["sc", "stop", SERVICE_NAME],
                   capture_output=True, creationflags=_SW_HIDE)
    time.sleep(0.3)

    if os.path.exists(BACKUP_PATH):
        try:
            shutil.copy2(BACKUP_PATH, SERVICE_PATH)
            os.remove(BACKUP_PATH)
        except Exception:
            pass

    # Restart the service (whether it's GOG's original or our dummy)
    subprocess.run(["sc", "start", SERVICE_NAME],
                   capture_output=True, creationflags=_SW_HIDE)


def install_cert():
    if not os.path.exists(CERT_FILE):
        return
    # Copy cert next to the exe so it persists after _MEIPASS cleanup
    persistent_cert = os.path.join(DATA_DIR, "fake.crt")
    if not os.path.exists(persistent_cert):
        shutil.copy2(CERT_FILE, persistent_cert)
    # Install via certutil (legacy method)
    subprocess.run(["certutil", "-addstore", "root", CERT_FILE],
                   capture_output=True, creationflags=_SW_HIDE)
    # Also install via PowerShell Import-Certificate (more reliable for native SDK)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f'Import-Certificate -FilePath "{CERT_FILE}" -CertStoreLocation Cert:\\LocalMachine\\Root'],
        capture_output=True, creationflags=_SW_HIDE
    )


def install_mod_dll(gwent_path):
    if not os.path.exists(MOD_DLL):
        return False
    mods_dir = os.path.join(gwent_path, "Mods")
    os.makedirs(mods_dir, exist_ok=True)
    shutil.copy2(MOD_DLL, os.path.join(mods_dir, "GwentBetaRestorationMod.dll"))
    return True


def install_settings(gwent_path):
    """Copy config.json and Launch.cfg to StreamingAssets/Settings to skip license check."""
    settings_dir = os.path.join(gwent_path, "Gwent_Data", "StreamingAssets", "Settings")
    os.makedirs(settings_dir, exist_ok=True)
    if os.path.exists(SETTINGS_CONFIG):
        shutil.copy2(SETTINGS_CONFIG, os.path.join(settings_dir, "config.json"))
    if os.path.exists(SETTINGS_LAUNCH):
        shutil.copy2(SETTINGS_LAUNCH, os.path.join(settings_dir, "Launch.cfg"))


def register_user(username, full_collection=False, server_url=None):
    base = server_url or SERVER_URL
    payload = json.dumps({"username": username, "full_collection": full_collection}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/register", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
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
    payload = json.dumps({"username": username, "id": user_id}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/login", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
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


def create_users_json(user_id, username):
    ensure_data_dir()
    users = [{"id": user_id, "username": username}]
    with open(USERS_JSON, "w") as f:
        json.dump(users, f, indent=2)


def change_username(user_id, new_username):
    """Change username on the server. Returns (success, message)."""
    log_path = LOG_FILE
    payload = json.dumps({"user_id": user_id, "new_username": new_username}).encode("utf-8")
    url = f"{SERVER_URL}/change_username"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        ensure_data_dir()
        with open(log_path, "a") as f:
            f.write(f"[USERNAME] POST {url} user_id={user_id} new_username={new_username!r}\n")
        resp = urllib.request.urlopen(req, context=ssl_context(), timeout=10)
        raw = resp.read()
        data = json.loads(raw)
        with open(log_path, "a") as f:
            f.write(f"[USERNAME] Success: {data}\n")
        return True, data.get("username", new_username)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        with open(log_path, "a") as f:
            f.write(f"[USERNAME] HTTP {e.code}: {body}\n")
        try:
            err = json.loads(body)
            return False, err.get("error", body)
        except Exception:
            return False, f"Server error {e.code}: {body}"
    except Exception as e:
        with open(log_path, "a") as f:
            f.write(f"[USERNAME] Exception: {e}\n")
        return False, f"Connection failed: {e}"


def check_mod_update(current_version):
    """Placeholder. Check GitHub for a newer mod DLL. Returns (new_version, download_url) or (None, None)."""
    if not GITHUB_MOD_RELEASE_URL:
        return None, None
    try:
        req = urllib.request.Request(GITHUB_MOD_RELEASE_URL)
        req.add_header("User-Agent", "GwentBetaLauncher")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        tag = data.get("tag_name", "")
        assets = data.get("assets", [])
        for asset in assets:
            if asset.get("name", "") == GITHUB_MOD_DLL_NAME:
                dl_url = asset.get("browser_download_url", "")
                if tag and tag != current_version and dl_url:
                    return tag, dl_url
        return None, None
    except Exception:
        return None, None


def download_mod_update(download_url, gwent_path, progress_callback=None):
    """Placeholder. Download and install an updated mod DLL from GitHub."""
    mods_dir = os.path.join(gwent_path, "Mods")
    os.makedirs(mods_dir, exist_ok=True)
    dest = os.path.join(mods_dir, GITHUB_MOD_DLL_NAME)
    download_file(download_url, dest, progress_callback=progress_callback)
    return True


def _get_active_adapters():
    """Get names of ALL active network adapters."""
    adapters = []
    try:
        ret = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True, text=True, timeout=5,
            creationflags=_SW_HIDE
        )
        for line in ret.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "Enabled" and parts[1] == "Connected":
                adapters.append(" ".join(parts[3:]))
    except Exception:
        pass
    return adapters


def detect_upstream_dns():
    """Detect the current DNS server before we override it."""
    try:
        for adapter in _get_active_adapters():
            ret = subprocess.run(
                ["netsh", "interface", "ip", "show", "dns", f"name={adapter}"],
                capture_output=True, text=True, timeout=5,
                creationflags=_SW_HIDE
            )
            for line in ret.stdout.splitlines():
                line = line.strip()
                if line and line[0].isdigit() and "." in line:
                    ip = line.split()[0]
                    if ip != "127.0.0.1":
                        return ip
    except Exception:
        pass
    return "8.8.8.8"


def stop_dns_client_service():
    """Stop the Windows DNS Client service so we can bind to port 53."""
    subprocess.run(["sc", "stop", "Dnscache"],
                   capture_output=True, creationflags=_SW_HIDE)
    time.sleep(0.5)


def start_dns_client_service():
    """Restart the Windows DNS Client service."""
    subprocess.run(["sc", "start", "Dnscache"],
                   capture_output=True, creationflags=_SW_HIDE)


def disable_ipv6(adapter_names):
    """Disable IPv6 on all adapters to prevent DNS leaking to real GOG servers."""
    if not adapter_names:
        return
    if isinstance(adapter_names, str):
        adapter_names = [adapter_names]
    for adapter in adapter_names:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f'Set-NetAdapterBinding -Name "{adapter}" -ComponentID ms_tcpip6 -Enabled $false'],
            capture_output=True, creationflags=_SW_HIDE
        )


def enable_ipv6(adapter_names):
    """Re-enable IPv6 on all adapters."""
    if not adapter_names:
        return
    if isinstance(adapter_names, str):
        adapter_names = [adapter_names]
    for adapter in adapter_names:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f'Set-NetAdapterBinding -Name "{adapter}" -ComponentID ms_tcpip6 -Enabled $true'],
            capture_output=True, creationflags=_SW_HIDE
        )


def set_dns_to_localhost():
    """Set ALL active network adapters' DNS to 127.0.0.1.
    Returns list of adapter names that were changed."""
    adapters = _get_active_adapters()
    changed = []
    for adapter in adapters:
        try:
            subprocess.run(
                ["netsh", "interface", "ip", "set", "dns",
                 f"name={adapter}", "static", "127.0.0.1"],
                capture_output=True,
                creationflags=_SW_HIDE
            )
            changed.append(adapter)
        except Exception:
            pass
    return changed if changed else None


def _clear_stale_loopback_nameservers():
    """Set any adapter NameServer left pointing at 127.0.0.1 back to empty.
    netsh '... set dns ... dhcp' flips the DHCP flag but can leave a stale
    static NameServer in the registry (seen on a physical Realtek NIC). DHCP
    wins at runtime so internet still works, but Network Settings shows
    "IP Assignment: 127.0.0.1" and a cold boot can honour it. Empty-string
    write is used rather than delete: a delete is silently ignored on some
    builds, while writing "" reliably clears it (verified on this machine)."""
    try:
        import winreg
    except ImportError:
        return
    base = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               base + "\\" + sub, 0,
                               winreg.KEY_READ | winreg.KEY_SET_VALUE)
        except OSError:
            continue
        try:
            val, _ = winreg.QueryValueEx(k, "NameServer")
            if val and "127.0.0.1" in str(val):
                winreg.SetValueEx(k, "NameServer", 0, winreg.REG_SZ, "")
        except OSError:
            pass
        finally:
            winreg.CloseKey(k)
    winreg.CloseKey(root)


def restore_dns(adapter_names):
    """Restore DNS to DHCP on all modified adapters and restart the DNS client service."""
    if not adapter_names:
        _clear_stale_loopback_nameservers()
        start_dns_client_service()
        return
    if isinstance(adapter_names, str):
        adapter_names = [adapter_names]
    for adapter in adapter_names:
        try:
            subprocess.run(
                ["netsh", "interface", "ip", "set", "dns",
                 f"name={adapter}", "dhcp"],
                capture_output=True,
                creationflags=_SW_HIDE
            )
        except Exception:
            pass
    _clear_stale_loopback_nameservers()
    start_dns_client_service()


# ---------------------------------------------------------------------------
# DNS safety net - ensure DNS is ALWAYS restored, even on crash
# ---------------------------------------------------------------------------
# Global state tracked so the cleanup functions can access it without the GUI
_dns_modified = False
_dns_adapter = None


def _emergency_dns_cleanup():
    """Last-resort cleanup registered via atexit and signal handlers."""
    global _dns_modified, _dns_adapter
    if _dns_modified:
        restore_service_binary()
        enable_ipv6(_dns_adapter)
        restore_dns(_dns_adapter)
        _dns_modified = False


# Register cleanup for normal exit
atexit.register(_emergency_dns_cleanup)

# Register cleanup for termination signals
for _sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(_sig, lambda s, f: (_emergency_dns_cleanup(), sys.exit(1)))
    except (OSError, ValueError):
        pass  # Some signals can't be caught on Windows

# On Windows, also handle console close / logoff / shutdown
if sys.platform == "win32":
    try:
        _kernel32 = ctypes.windll.kernel32
        _CTRL_HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def _console_handler(event):
            _emergency_dns_cleanup()
            return 0

        _kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER(_console_handler), True)
    except Exception:
        pass


_RUNONCE_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"
_RUNONCE_NAME = "GwentBetaDnsRestore"

# PowerShell run once at next boot (before login). Scoped to be SAFE: it only
# touches adapters whose DNS is actually our loopback redirect (127.0.0.1) and
# leaves any DNS the user set deliberately untouched. It resets just those
# adapters to DHCP, strips any leftover loopback NameServer in the registry,
# then restarts the DNS client and flushes. Depends only on built-in tooling,
# so it works even if the launcher exe is gone. A no-console GUI process gets
# no reliable shutdown notification, so this RunOnce is the only dependable
# mid-game-shutdown recovery (they reboot, it heals
# silently before login, then Windows auto-deletes the RunOnce entry).
_RUNONCE_IFACE_KEY = r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
_RUNONCE_PS = (
    "$ErrorActionPreference='SilentlyContinue';"
    # Only reset adapters that currently have 127.0.0.1 as a DNS server.
    "Get-DnsClientServerAddress -AddressFamily IPv4 | "
    "Where-Object { $_.ServerAddresses -contains '127.0.0.1' } | "
    "ForEach-Object { Set-DnsClientServerAddress -InterfaceIndex "
    "$_.InterfaceIndex -ResetServerAddresses };"
    # Strip any leftover static loopback NameServer in the registry.
    "Get-ChildItem '" + _RUNONCE_IFACE_KEY + "' | ForEach-Object { "
    "if ((Get-ItemProperty $_.PSPath).NameServer -match '127.0.0.1') { "
    "Set-ItemProperty $_.PSPath -Name NameServer -Value '' } };"
    "Start-Service Dnscache; ipconfig /flushdns"
)


def _arm_runonce_dns_restore():
    """Write the RunOnce boot-time DNS-restore command."""
    if sys.platform != "win32":
        return
    try:
        import winreg
        cmd = ('powershell -NoProfile -WindowStyle Hidden -Command "%s"'
               % _RUNONCE_PS)
        k = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, _RUNONCE_KEY)
        winreg.SetValueEx(k, _RUNONCE_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(k)
    except Exception:
        pass


def _disarm_runonce_dns_restore():
    """Delete the RunOnce entry after a clean restore so it never fires."""
    if sys.platform != "win32":
        return
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _RUNONCE_KEY, 0,
                           winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(k, _RUNONCE_NAME)
        except OSError:
            pass
        winreg.CloseKey(k)
    except Exception:
        pass


def mark_dns_modified(adapter_name):
    """Call when DNS has been changed - arms the safety net."""
    global _dns_modified, _dns_adapter
    _dns_modified = True
    _dns_adapter = adapter_name
    _arm_runonce_dns_restore()


def mark_dns_restored():
    """Call when DNS has been restored - disarms the safety net."""
    global _dns_modified
    _dns_modified = False
    _disarm_runonce_dns_restore()


# ---------------------------------------------------------------------------
# In-process service runners
# ---------------------------------------------------------------------------
def start_commservice_thread():
    """Run the commservice TCP server in a daemon thread. Returns the server object."""
    log_path = LOG_FILE
    try:
        sys.path.insert(0, BUNDLE_DIR)

        import importlib.util
        spec = importlib.util.spec_from_file_location("commservice", COMMSERVICE_PY)
        comm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(comm)
        # Patch USERS_FILE AFTER exec_module (exec overwrites any pre-set value)
        comm.USERS_FILE = USERS_JSON
        # Route commservice's [COMM] print() output into launcher_debug.log so we
        # can see the per-connection wire handshake (sort/type, connect/disconnect)
        # after a session. On a windowed .exe its stdout is otherwise discarded.
        # Mirrors the Linux launcher's redirect. commservice.py calls the bare
        # name `print`, which resolves to this module-level override.
        _comm_log_lock = threading.Lock()
        def _comm_print(*args, **kwargs):
            try:
                line = " ".join(str(a) for a in args)
                with _comm_log_lock:
                    with open(log_path, "a") as cf:
                        cf.write(line + "\n")
            except Exception:
                pass
        comm.print = _comm_print
        # Reload users so CommServiceHandler sees the right identities
        comm.load_users()

        srv = comm.ThreadedTCPServer(("127.0.0.1", 9977), comm.CommServiceHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        with open(log_path, "a") as f:
            f.write(f"[commservice] Started on 127.0.0.1:9977\n")
        return srv
    except Exception as e:
        import traceback
        with open(log_path, "a") as f:
            f.write(f"[commservice] FAILED: {e}\n")
            traceback.print_exc(file=f)
        raise


def start_dns_proxy_thread(server_ip, upstream_dns):
    """Run the DNS proxy in a daemon thread."""
    log_path = LOG_FILE
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("dns_proxy", DNS_PROXY_PY)
        dns_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dns_mod)

        def _run_proxy():
            try:
                dns_mod.run_dns_proxy(server_ip, upstream_dns, 53, "127.0.0.1")
            except Exception as e:
                import traceback
                with open(log_path, "a") as f:
                    f.write(f"[dns_proxy] CRASHED: {e}\n")
                    traceback.print_exc(file=f)

        t = threading.Thread(target=_run_proxy, daemon=True)
        t.start()
        with open(log_path, "a") as f:
            f.write(f"[dns_proxy] Started for {server_ip} (upstream={upstream_dns})\n")
        return t
    except Exception as e:
        import traceback
        with open(log_path, "a") as f:
            f.write(f"[dns_proxy] FAILED: {e}\n")
            traceback.print_exc(file=f)
        raise


def start_https_proxy_thread(cert_file, key_file, remote_server):
    """Run the local HTTPS reverse proxy in a daemon thread."""
    log_path = LOG_FILE
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("https_proxy", HTTPS_PROXY_PY)
        proxy_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proxy_mod)

        def _run_https():
            try:
                proxy_mod.run_https_proxy(cert_file, key_file, remote_server, 443)
            except Exception as e:
                import traceback
                with open(log_path, "a") as f:
                    f.write(f"[https_proxy] CRASHED: {e}\n")
                    traceback.print_exc(file=f)

        t = threading.Thread(target=_run_https, daemon=True)
        t.start()

        with open(log_path, "a") as f:
            f.write(f"[https_proxy] Started on 127.0.0.1:443 -> {remote_server}\n")
        return t
    except Exception as e:
        import traceback
        with open(log_path, "a") as f:
            f.write(f"[https_proxy] FAILED: {e}\n")
            traceback.print_exc(file=f)
        raise


def start_relay_proxy_thread(remote_server):
    """Run the local TCP proxy for the game relay (port 7777)."""
    log_path = LOG_FILE
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("https_proxy", HTTPS_PROXY_PY)
        proxy_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proxy_mod)

        def _run_relay():
            try:
                proxy_mod.run_relay_proxy(remote_server, 7777, 7777)
            except Exception as e:
                import traceback
                with open(log_path, "a") as f:
                    f.write(f"[relay_proxy] CRASHED: {e}\n")
                    traceback.print_exc(file=f)

        t = threading.Thread(target=_run_relay, daemon=True)
        t.start()

        with open(log_path, "a") as f:
            f.write(f"[relay_proxy] Started on 127.0.0.1:7777 -> {remote_server}:7777\n")
        return t
    except Exception as e:
        import traceback
        with open(log_path, "a") as f:
            f.write(f"[relay_proxy] FAILED: {e}\n")
            traceback.print_exc(file=f)
        raise


def start_internal_proxy_thread(remote_server):
    """Run a plain TCP proxy for the internal game-invite HTTP listener (port 8447)."""
    log_path = LOG_FILE
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("https_proxy", HTTPS_PROXY_PY)
        proxy_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proxy_mod)

        def _run():
            try:
                proxy_mod.run_relay_proxy(remote_server, 8447, 8447)
            except Exception as e:
                import traceback
                with open(log_path, "a") as f:
                    f.write(f"[internal_proxy] CRASHED: {e}\n")
                    traceback.print_exc(file=f)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        with open(log_path, "a") as f:
            f.write(f"[internal_proxy] Started on 127.0.0.1:8447 -> {remote_server}:8447\n")
        return t
    except Exception as e:
        import traceback
        with open(log_path, "a") as f:
            f.write(f"[internal_proxy] FAILED: {e}\n")
            traceback.print_exc(file=f)
        raise


def start_broker_proxy_thread(cert_file, key_file, remote_server):
    """Run the local TCP proxy for broker WebSocket connections."""
    log_path = LOG_FILE
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("https_proxy", HTTPS_PROXY_PY)
        proxy_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proxy_mod)

        def _run_broker():
            try:
                proxy_mod.run_broker_proxy(cert_file, key_file, remote_server, 8445, 8445)
            except Exception as e:
                import traceback
                with open(log_path, "a") as f:
                    f.write(f"[broker_proxy] CRASHED: {e}\n")
                    traceback.print_exc(file=f)

        t = threading.Thread(target=_run_broker, daemon=True)
        t.start()

        with open(log_path, "a") as f:
            f.write(f"[broker_proxy] Started on 127.0.0.1:8445 -> {remote_server}:8445\n")
        return t
    except Exception as e:
        import traceback
        with open(log_path, "a") as f:
            f.write(f"[broker_proxy] FAILED: {e}\n")
            traceback.print_exc(file=f)
        raise


# ---------------------------------------------------------------------------
# Download helper with callback
# ---------------------------------------------------------------------------
def download_file(url, dest_path, progress_callback=None, ctx=None):
    """Download a file. Calls progress_callback(downloaded, total) periodically."""
    if ctx is None:
        ctx = ssl_context()
    req = urllib.request.Request(url)
    resp = urllib.request.urlopen(req, context=ctx, timeout=60)
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    block_size = 256 * 1024

    with open(dest_path, "wb") as f:
        while True:
            chunk = resp.read(block_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress_callback:
                progress_callback(downloaded, total)
    return dest_path


# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------
class GwentLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gwent Beta - Private Server")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)

        # Window size and centering
        w, h = 520, 560
        sx = (self.winfo_screenwidth() - w) // 2
        sy = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{sx}+{sy}")

        self.cfg = load_config()
        self.comm_server = None
        self.adapter_name = None
        self._cleaned_up = False

        # Configure ttk styles
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.style.configure("Gold.TButton",
                             background=BTN_BG, foreground=BTN_FG,
                             font=("Segoe UI", 13, "bold"),
                             padding=(40, 12))
        self.style.map("Gold.TButton",
                       background=[("active", ACCENT_HOVER), ("disabled", "#555555")],
                       foreground=[("disabled", "#888888")])
        self.style.configure("Gold.Horizontal.TProgressbar",
                             troughcolor=PROGRESS_BG, background=PROGRESS_FG,
                             thickness=18)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if "user_id" in self.cfg and self.cfg.get("gwent_path"):
            self.show_main_screen()
        else:
            self.show_setup_screen()

    # ── Setup Screen ──────────────────────────────────────────────────────

    def show_setup_screen(self):
        self._clear()
        self.geometry("540x740")

        frame = tk.Frame(self, bg=BG_DARK)
        frame.pack(fill="both", expand=True, padx=30, pady=20)

        # Title
        tk.Label(frame, text="GWENT", font=("Segoe UI", 28, "bold"),
                 fg=FG_TITLE, bg=BG_DARK).pack(pady=(5, 0))
        tk.Label(frame, text="Private Server Setup", font=("Segoe UI", 12),
                 fg=FG_DIM, bg=BG_DARK).pack(pady=(0, 20))

        # --- Gwent path ---
        path_frame = tk.Frame(frame, bg=BG_DARK)
        path_frame.pack(fill="x", pady=(0, 12))
        tk.Label(path_frame, text="Gwent Installation", font=("Segoe UI", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w")

        path_row = tk.Frame(path_frame, bg=BG_DARK)
        path_row.pack(fill="x", pady=(2, 0))

        self.path_var = tk.StringVar(value=find_gwent() or "")
        self.path_entry = tk.Entry(path_row, textvariable=self.path_var,
                                   font=("Segoe UI", 10), bg=BG_INPUT,
                                   fg=FG_TEXT, insertbackground=FG_TEXT,
                                   relief="flat", bd=0)
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))

        browse_btn = tk.Button(path_row, text="Browse", font=("Segoe UI", 9),
                               bg=BG_CARD, fg=FG_TEXT, relief="flat",
                               activebackground=BG_INPUT, activeforeground=FG_TEXT,
                               command=self.browse_gwent)
        browse_btn.pack(side="right", ipady=3, ipadx=8)

        self.path_status = tk.Label(path_frame, text="", font=("Segoe UI", 8),
                                    fg=FG_DIM, bg=BG_DARK, anchor="w")
        self.path_status.pack(anchor="w", pady=(2, 0))

        # Auto-detect status
        if self.path_var.get():
            self.path_status.config(text="Found existing installation", fg=SUCCESS_FG)
        else:
            self.path_status.config(
                text="Not found - click Browse and select your Gwent install folder", fg=FG_DIM)

        # --- Server address: which server to connect to (user-supplied) ---
        server_frame = tk.Frame(frame, bg=BG_DARK)
        server_frame.pack(fill="x", pady=(0, 12))
        tk.Label(server_frame, text="Server Address", font=("Segoe UI", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w")

        # "Host on this PC" - when ticked, the user is running GwentServerHost
        # on this same machine, so the address is just localhost. Saves them
        # needing to know/enter any IP.
        # Default the host checkbox ON only when there is no real server to point
        # at: i.e. no saved server_ip AND no community server baked into the build.
        _has_baked_server = (SERVER_IP not in ("", "127.0.0.1"))
        _saved = self.cfg.get("server_ip", "")
        self.host_here_var = tk.BooleanVar(
            value=(_saved in ("", "127.0.0.1") and not _has_baked_server))
        tk.Checkbutton(server_frame,
                       text="I'm hosting the server on this PC",
                       variable=self.host_here_var, command=self._toggle_host_here,
                       font=("Segoe UI", 9), fg=FG_TEXT, bg=BG_DARK,
                       activebackground=BG_DARK, activeforeground=FG_TEXT,
                       selectcolor=BG_INPUT, anchor="w").pack(anchor="w", pady=(2, 4))

        self.server_var = tk.StringVar(
            value=self.cfg.get("server_ip", "") or (SERVER_IP if SERVER_IP != "127.0.0.1" else ""))
        self.server_entry = tk.Entry(server_frame, textvariable=self.server_var,
                                     font=("Segoe UI", 10), bg=BG_INPUT,
                                     fg=FG_TEXT, insertbackground=FG_TEXT,
                                     relief="flat", bd=0)
        self.server_entry.pack(fill="x", ipady=6, pady=(2, 0))
        self.server_hint = tk.Label(server_frame,
                 text="Host or IP of the server you're joining (ask whoever runs it).",
                 font=("Segoe UI", 8), fg=FG_DIM, bg=BG_DARK, anchor="w")
        self.server_hint.pack(anchor="w", pady=(2, 0))
        self._toggle_host_here()  # apply initial state

        # --- Auth mode: create a new account vs sign in to an existing one ---
        self.auth_mode_var = tk.StringVar(value="create")
        mode_row = tk.Frame(frame, bg=BG_DARK)
        mode_row.pack(fill="x", pady=(0, 4))
        tk.Radiobutton(
            mode_row, text="Create account", variable=self.auth_mode_var,
            value="create", command=self._update_auth_mode,
            font=("Segoe UI", 10), fg=FG_TEXT, bg=BG_DARK,
            selectcolor=BG_INPUT, activebackground=BG_DARK,
            activeforeground=FG_TEXT, highlightthickness=0
        ).pack(side="left", padx=(0, 16))
        tk.Radiobutton(
            mode_row, text="Sign in", variable=self.auth_mode_var,
            value="signin", command=self._update_auth_mode,
            font=("Segoe UI", 10), fg=FG_TEXT, bg=BG_DARK,
            selectcolor=BG_INPUT, activebackground=BG_DARK,
            activeforeground=FG_TEXT, highlightthickness=0
        ).pack(side="left")

        # --- Username ---
        tk.Label(frame, text="Username", font=("Segoe UI", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w", pady=(0, 2))
        self.username_var = tk.StringVar()
        name_entry = tk.Entry(frame, textvariable=self.username_var,
                              font=("Segoe UI", 12), bg=BG_INPUT,
                              fg=FG_TEXT, insertbackground=FG_TEXT,
                              relief="flat", bd=0)
        name_entry.pack(fill="x", ipady=8, pady=(0, 12))
        name_entry.focus_set()

        # --- User ID (Sign in only; hidden in Create mode) ---
        # Acts as a shared secret so opponents who see your username in-match
        # can't sign in as you. Shown by _update_auth_mode in sign-in mode.
        self.userid_var = tk.StringVar()
        self.userid_frame = tk.Frame(frame, bg=BG_DARK)
        tk.Label(self.userid_frame, text="User ID", font=("Segoe UI", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w", pady=(0, 2))
        tk.Entry(self.userid_frame, textvariable=self.userid_var,
                 font=("Segoe UI", 12), bg=BG_INPUT, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", bd=0).pack(
            fill="x", ipady=8)
        tk.Label(self.userid_frame,
                 text="The numeric ID shown on your account screen.",
                 font=("Segoe UI", 8), fg=FG_DIM, bg=BG_DARK).pack(
            anchor="w", pady=(2, 8))
        # Hidden by default (Create mode).

        # --- Full collection toggle (new-account only) ---
        self.full_collection_var = tk.BooleanVar(value=True)
        check_frame = tk.Frame(frame, bg=BG_DARK)
        check_frame.pack(fill="x", pady=(0, 8))

        self.collection_cb = tk.Checkbutton(
            check_frame,
            text="Start with full collection, max currencies & starter decks",
            variable=self.full_collection_var,
            font=("Segoe UI", 10), fg=FG_TEXT, bg=BG_DARK,
            selectcolor=BG_INPUT, activebackground=BG_DARK,
            activeforeground=FG_TEXT, highlightthickness=0
        )
        self.collection_cb.pack(anchor="w")

        tk.Label(check_frame,
                 text="Uncheck to start from scratch with no cards or decks",
                 font=("Segoe UI", 8), fg=FG_DIM, bg=BG_DARK).pack(anchor="w", padx=(24, 0))
        # New-account-only options live in this frame; hidden in sign-in mode.
        self.new_account_frame = check_frame

        # --- Progress area ---
        self.progress_frame = tk.Frame(frame, bg=BG_DARK)
        self.progress_frame.pack(fill="x", pady=(8, 0))

        self.status_label = tk.Label(self.progress_frame, text="",
                                     font=("Segoe UI", 9), fg=FG_DIM, bg=BG_DARK)
        self.status_label.pack(anchor="w")

        self.progress_bar = ttk.Progressbar(self.progress_frame,
                                            style="Gold.Horizontal.TProgressbar",
                                            mode="determinate", length=460)
        self.progress_bar.pack(fill="x", pady=(4, 0))
        self.progress_bar.pack_forget()  # Hidden until needed

        # --- Install button ---
        self.install_btn = ttk.Button(frame, text="Install & Play",
                                      style="Gold.TButton",
                                      command=self.start_install)
        self.install_btn.pack(pady=(18, 0), ipadx=20, ipady=4)

        # Apply initial auth-mode layout (Create by default).
        self._update_auth_mode()

    def _toggle_host_here(self):
        if self.host_here_var.get():
            self.server_var.set("127.0.0.1")
            self.server_entry.config(state="disabled")
            self.server_hint.config(
                text="Hosting locally: the launcher will use this PC's server.")
        else:
            if self.server_var.get().strip() == "127.0.0.1":
                self.server_var.set("")
            self.server_entry.config(state="normal")
            self.server_hint.config(
                text="Host or IP of the server you're joining (ask whoever runs it).")

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
                self.new_account_frame.pack(fill="x", pady=(0, 8),
                                            before=self.progress_frame)
                self.install_btn.config(text="Install & Play")
        except Exception:
            pass

    def browse_gwent(self):
        path = filedialog.askdirectory(title="Select Gwent installation folder")
        if path:
            if validate_gwent_install(path):
                self.path_var.set(path)
                self.path_status.config(text="Valid Gwent installation", fg=SUCCESS_FG)
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
            self.status_label.config(text=f"Downloading... {mb_done:.0f} / {mb_total:.0f} MB")
        else:
            mb_done = downloaded / (1024 * 1024)
            self.status_label.config(text=f"Downloading... {mb_done:.0f} MB")
        self.update_idletasks()

    def start_install(self):
        username = self.username_var.get().strip()
        if not username:
            self.set_status("Please enter a username.", ERROR_FG)
            return

        if self.auth_mode_var.get() == "signin" and not self.userid_var.get().strip():
            self.set_status("Please enter your User ID to sign in.", ERROR_FG)
            return

        if not self.server_var.get().strip():
            self.set_status("Please enter the server address to connect to.", ERROR_FG)
            return

        self.install_btn.state(["disabled"])
        self.collection_cb.config(state="disabled")
        threading.Thread(target=self.run_install, args=(username,), daemon=True).start()

    def run_install(self, username):
        """Run the full install sequence in a background thread."""
        # Register/login must target the server the user ENTERED, not the
        # baked-in SERVER_URL (127.0.0.1 in a neutral build). The DNS/proxy
        # redirect isn't up during install, so hit the server's real IP.
        _entered = (self.server_var.get().strip() if hasattr(self, "server_var") else "")
        reg_url = ("https://" + _entered) if _entered else SERVER_URL
        try:
            # Sign-in fast path: if signing in to an existing account and this
            # machine already has a completed install (valid game path), skip
            # all one-time setup (VC++, MelonLoader, mod, cert,
            # service) -- just authenticate and go straight to the main screen.
            # Windows has no wine prefix, so a valid gwent_path is the only
            # "completed install" check needed.
            if self.auth_mode_var.get() == "signin":
                cfg_path = self.cfg.get("gwent_path") or self.path_var.get().strip()
                if cfg_path and validate_gwent_install(cfg_path):
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
                    # Persist the resolved game path so run_game() can find
                    # Gwent.exe -- cfg_path may have come from path_var (auto-
                    # detected) rather than an existing config entry.
                    self.cfg["gwent_path"] = cfg_path
                    self.cfg["user_id"] = user_id
                    self.cfg["username"] = result
                    self.cfg["server_ip"] = self.server_var.get().strip() or SERVER_IP
                    self.cfg["version"] = VERSION
                    save_config(self.cfg)
                    self.after(0, self.show_main_screen)
                    return

            # Step 0: VC++ Redistributable
            if not is_vcredist_installed():
                self.after(0, self.set_status, "Installing Visual C++ runtime...")
                self.after(0, self.show_progress)
                try:
                    ok = install_vcredist(
                        progress_callback=lambda d, t: self.after(0, self.update_progress, d, t)
                    )
                    if not ok:
                        self.after(0, self.install_error,
                                   "VC++ runtime install failed. Try installing manually.")
                        return
                except Exception as e:
                    self.after(0, self.install_error, f"VC++ runtime install failed: {e}")
                    return

            # Step 1: Gwent path
            # The game client is NOT distributed by this installer. The user
            # must point at their own legally-obtained Gwent 0.9.24.3.432 install.
            gwent_path = self.path_var.get().strip()
            if not gwent_path or not validate_gwent_install(gwent_path):
                self.after(0, self.install_error,
                           "No valid Gwent 0.9.24.3 installation found. Click "
                           "'Browse' and select your own Gwent install folder "
                           "(the folder containing Gwent.exe). This installer "
                           "does not download the game.")
                return

            # Step 2: MelonLoader
            self.after(0, self.set_status, "Installing MelonLoader...")
            if not is_melonloader_installed(gwent_path):
                self.after(0, self.show_progress)
                self.after(0, lambda: self.progress_bar.configure(mode="determinate"))
                zip_path = os.path.join(gwent_path, "ml_download.zip")
                try:
                    download_file(
                        MELONLOADER_URL, zip_path,
                        progress_callback=lambda d, t: self.after(0, self.update_progress, d, t)
                    )
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(gwent_path)
                    os.remove(zip_path)
                except Exception as e:
                    self.after(0, self.install_error, f"MelonLoader install failed: {e}")
                    return

            # Step 3: Mod DLL + settings
            self.after(0, self.set_status, "Installing mod...")
            if not install_mod_dll(gwent_path):
                self.after(0, self.install_error, "Mod DLL not found in bundle.")
                return
            install_settings(gwent_path)

            # Step 4: Register a new account OR sign in to an existing one
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
                    self.after(0, self.install_error, f"Sign-in failed: {result}")
                    return
            else:
                self.after(0, self.set_status, "Registering with server...")
                full_coll = self.full_collection_var.get()
                user_id, result = register_user(username, full_coll, server_url=reg_url)
                if user_id is None:
                    self.after(0, self.install_error, f"Registration failed: {result}")
                    return
            # result is the confirmed username from the server
            username = result

            # Step 5: System setup
            self.after(0, self.set_status, "Configuring system components...")
            create_users_json(user_id, username)
            install_dummy_service()
            install_cert()
            clear_galaxy_cache()

            # Step 6: Save config
            self.cfg["gwent_path"] = gwent_path
            self.cfg["user_id"] = user_id
            self.cfg["username"] = username
            self.cfg["server_ip"] = self.server_var.get().strip() or SERVER_IP
            self.cfg["version"] = VERSION
            save_config(self.cfg)

            # Done - show main screen
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

    # ── Main Screen ───────────────────────────────────────────────────────

    def show_main_screen(self):
        self._clear()
        self.geometry("420x440")

        frame = tk.Frame(self, bg=BG_DARK)
        frame.pack(fill="both", expand=True, padx=30, pady=20)

        # Title
        tk.Label(frame, text="GWENT", font=("Segoe UI", 32, "bold"),
                 fg=FG_TITLE, bg=BG_DARK).pack(pady=(10, 0))
        tk.Label(frame, text="The Witcher Card Game", font=("Segoe UI", 11),
                 fg=FG_DIM, bg=BG_DARK).pack(pady=(0, 5))

        # Subtitle
        tk.Label(frame, text="0.9.24 Open Beta - Private Server",
                 font=("Segoe UI", 9), fg=FG_DIM, bg=BG_DARK).pack(pady=(0, 15))

        # User info card
        username = self.cfg.get("username", "Player")
        info_frame = tk.Frame(frame, bg=BG_CARD, bd=0, highlightthickness=1,
                              highlightbackground="#2a2a4a")
        info_frame.pack(fill="x", pady=(0, 12), ipady=6, ipadx=15)

        info_top = tk.Frame(info_frame, bg=BG_CARD)
        info_top.pack(fill="x", padx=15, pady=(8, 2))

        self.welcome_label = tk.Label(info_top, text=f"Welcome, {username}",
                 font=("Segoe UI", 12), fg=FG_TEXT, bg=BG_CARD)
        self.welcome_label.pack(side="left")

        sign_out_btn = tk.Button(info_top, text="Sign Out", font=("Segoe UI", 8),
                                 bg=BG_INPUT, fg=FG_DIM, relief="flat",
                                 activebackground=BG_DARK, activeforeground=FG_TEXT,
                                 cursor="hand2", command=self.sign_out)
        sign_out_btn.pack(side="right")

        change_name_btn = tk.Button(info_top, text="Change", font=("Segoe UI", 8),
                                     bg=BG_INPUT, fg=FG_DIM, relief="flat",
                                     activebackground=BG_DARK, activeforeground=FG_TEXT,
                                     cursor="hand2", command=self.show_change_username)
        change_name_btn.pack(side="right", padx=(0, 6))

        # User ID row -- needed to sign in on another device. Show + Copy.
        uid_row = tk.Frame(info_frame, bg=BG_CARD)
        uid_row.pack(fill="x", padx=15, pady=(0, 8))
        _uid = self.cfg.get("user_id", "")
        tk.Label(uid_row, text=f"User ID: {_uid}",
                 font=("Segoe UI", 9), fg=FG_DIM, bg=BG_CARD).pack(side="left")
        tk.Button(uid_row, text="Copy", font=("Segoe UI", 8),
                  bg=BG_INPUT, fg=FG_DIM, relief="flat",
                  activebackground=BG_DARK, activeforeground=FG_TEXT,
                  cursor="hand2", command=self._copy_user_id).pack(side="right")

        # Launch status
        self.launch_status = tk.Label(frame, text="", font=("Segoe UI", 9),
                                      fg=FG_DIM, bg=BG_DARK)
        self.launch_status.pack(pady=(0, 8))

        # Play button - large and prominent
        self.play_btn = tk.Button(frame, text="P L A Y", font=("Segoe UI", 14, "bold"),
                                  bg=BTN_BG, fg=BTN_FG, activebackground=ACCENT_HOVER,
                                  activeforeground=BTN_FG, relief="flat", cursor="hand2",
                                  padx=50, pady=10, command=self.start_game)
        self.play_btn.pack(pady=(0, 10))

        # Check for mod updates in the background
        threading.Thread(target=self._check_mod_update, daemon=True).start()

    def _copy_user_id(self):
        """Copy the current user_id to the clipboard."""
        try:
            self.clipboard_clear()
            self.clipboard_append(str(self.cfg.get("user_id", "")))
            try:
                self.launch_status.config(text="User ID copied to clipboard.",
                                          fg=SUCCESS_FG)
            except Exception:
                pass
        except Exception:
            pass

    def sign_out(self):
        """Clear the cached identity and return to the setup/sign-in screen.

        Keeps gwent_path so the user doesn't reinstall; only the account
        identity (user_id/username) is dropped. The single-user users.json is
        rewritten on the next sign-in/register via create_users_json,
        preserving the single-user commservice invariant.
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

    # ── Username Change ───────────────────────────────────────────────────

    def show_change_username(self):
        """Show a dialog to change the username."""
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

        tk.Label(dialog, text="New Username", font=("Segoe UI", 10),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w", padx=20, pady=(15, 2))

        name_var = tk.StringVar(value=self.cfg.get("username", ""))
        name_entry = tk.Entry(dialog, textvariable=name_var, font=("Segoe UI", 12),
                              bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
                              relief="flat", bd=0)
        name_entry.pack(fill="x", padx=20, ipady=6)
        name_entry.select_range(0, tk.END)
        name_entry.focus_set()

        status_label = tk.Label(dialog, text="", font=("Segoe UI", 9),
                                fg=FG_DIM, bg=BG_DARK)
        status_label.pack(pady=(5, 0))

        save_btn = tk.Button(dialog, text="Save", font=("Segoe UI", 10, "bold"),
                  bg=BTN_BG, fg=BTN_FG, relief="flat", cursor="hand2",
                  activebackground=ACCENT_HOVER, padx=20, pady=4)
        save_btn.pack(pady=(10, 0))

        def do_change():
            new_name = name_var.get().strip()
            if not new_name:
                status_label.config(text="Username cannot be empty.", fg=ERROR_FG)
                return
            if new_name == self.cfg.get("username"):
                dialog.destroy()
                return

            save_btn.config(state="disabled")
            status_label.config(text="Saving...", fg=FG_DIM)
            dialog.update_idletasks()

            def _do():
                user_id = self.cfg.get("user_id")
                try:
                    success, result = change_username(user_id, new_name)
                except Exception as e:
                    success, result = False, str(e)
                def _apply():
                    if success:
                        self.cfg["username"] = result
                        save_config(self.cfg)
                        create_users_json(user_id, result)
                        self.welcome_label.config(text=f"Welcome, {result}")
                        dialog.destroy()
                    else:
                        status_label.config(text=result, fg=ERROR_FG)
                        save_btn.config(state="normal")
                self.after(0, _apply)

            threading.Thread(target=_do, daemon=True).start()

        save_btn.config(command=do_change)
        name_entry.bind("<Return>", lambda e: do_change())

    # ── Mod Update Check ──────────────────────────────────────────────────

    def _check_mod_update(self):
        """Placeholder - Check for mod DLL updates from GitHub in the background."""
        current_ver = self.cfg.get("mod_version", "")
        new_ver, dl_url = check_mod_update(current_ver)
        if new_ver and dl_url:
            self.after(0, lambda: self.launch_status.config(
                text=f"Mod update available: {new_ver}", fg=ACCENT))
            self.cfg["_pending_mod_update"] = {"version": new_ver, "url": dl_url}

    def _apply_mod_update(self, gwent_path):
        """Placeholder - Download and install a pending mod update."""
        pending = self.cfg.get("_pending_mod_update")
        if not pending:
            return
        try:
            dl_url = pending["url"]
            new_ver = pending["version"]
            download_mod_update(dl_url, gwent_path)
            self.cfg["mod_version"] = new_ver
            self.cfg.pop("_pending_mod_update", None)
            save_config(self.cfg)
        except Exception:
            pass  # Non-critical -- bundled DLL still works

    def start_game(self):
        self.play_btn.config(state="disabled", bg="#555555")
        self.launch_status.config(text="Starting...", fg=FG_DIM)
        threading.Thread(target=self.run_game, daemon=True).start()

    def run_game(self):
        """Launch the game with all services, wait for exit, cleanup."""
        gwent_path = self.cfg.get("gwent_path", "")
        gwent_exe = os.path.join(gwent_path, "Gwent.exe")
        server_ip = self.cfg.get("server_ip", SERVER_IP)
        host_mode = _is_local_server(server_ip)  # hosting + playing on same PC

        if not os.path.isfile(gwent_exe):
            self.after(0, lambda: self.launch_status.config(
                text="Gwent.exe not found - reinstall required", fg=ERROR_FG))
            self.after(0, lambda: self.play_btn.config(state="normal", bg=BTN_BG))
            return

        try:
            # Update mod DLL + settings + check for GitHub updates
            install_mod_dll(gwent_path)
            install_settings(gwent_path)
            self._apply_mod_update(gwent_path)

            # Local proxies. SKIP them entirely when hosting + playing on the
            # same PC: the local server stack (nginx :443, relay :7777, broker
            # :8445, internal :8447) already listens on those ports, so starting
            # the client proxies would collide (WinError 10061). DNS redirect
            # below still points the game's *.gog.com at 127.0.0.1, where the
            # server stack answers directly.
            if host_mode:
                self.after(0, lambda: self.launch_status.config(
                    text="Hosting on this PC: using local server directly.", fg=FG_DIM))
            else:
                # Start local HTTPS reverse proxy (solves Galaxy SDK TLS issue)
                self.after(0, lambda: self.launch_status.config(text="Starting local proxy...", fg=FG_DIM))
                try:
                    start_https_proxy_thread(CERT_FILE, KEY_FILE, server_ip)
                    time.sleep(0.3)
                except Exception as e:
                    self.after(0, lambda: self.launch_status.config(
                        text=f"HTTPS proxy failed: {e}", fg=ERROR_FG))

                # Start broker proxy (forwards WebSocket connections to remote server)
                try:
                    start_broker_proxy_thread(CERT_FILE, KEY_FILE, server_ip)
                    time.sleep(0.1)
                except Exception as e:
                    self.after(0, lambda: self.launch_status.config(
                        text=f"Broker proxy failed: {e}", fg=ERROR_FG))

                # Start relay proxy (forwards game relay on port 7777)
                try:
                    start_relay_proxy_thread(server_ip)
                    time.sleep(0.1)
                except Exception as e:
                    self.after(0, lambda: self.launch_status.config(
                        text=f"Relay proxy failed: {e}", fg=ERROR_FG))

                # Start internal proxy (plain HTTP for game invitations on port 8447)
                try:
                    start_internal_proxy_thread(server_ip)
                    time.sleep(0.1)
                except Exception as e:
                    self.after(0, lambda: self.launch_status.config(
                        text=f"Internal proxy failed: {e}", fg=ERROR_FG))

            # Detect upstream DNS before we change anything
            self.after(0, lambda: self.launch_status.config(text="Configuring DNS...", fg=FG_DIM))
            upstream_dns = detect_upstream_dns()

            # Stop Windows DNS Client so we can bind port 53
            stop_dns_client_service()

            # Start DNS proxy - resolves .gog.com to 127.0.0.1 (our local proxy)
            try:
                self.dns_thread = start_dns_proxy_thread("127.0.0.1", upstream_dns)
                time.sleep(0.5)  # Give it time to bind
            except Exception as e:
                self.after(0, lambda: self.launch_status.config(
                    text=f"DNS proxy failed: {e}", fg=ERROR_FG))

            # Set system DNS to 127.0.0.1 so all lookups go through our proxy
            self.adapter_name = set_dns_to_localhost()
            mark_dns_modified(self.adapter_name)  # Arm the safety net
            if not self.adapter_name:
                self.after(0, lambda: self.launch_status.config(
                    text="Warning: Could not set DNS - connection may fail", fg=ERROR_FG))

            # Disable IPv6 to prevent DNS leaking to real GOG servers
            disable_ipv6(self.adapter_name)

            # Swap GalaxyCommunication service binary (backs up GOG's if present)
            # Must happen FIRST - GOG's real service holds port 9977
            self.after(0, lambda: self.launch_status.config(text="Configuring services...", fg=FG_DIM))
            swap_service_binary()
            # Wait for port 9977 to be released after service stop
            for _ in range(20):
                ret = subprocess.run(["netstat", "-an"], capture_output=True,
                                     text=True, creationflags=_SW_HIDE)
                if ":9977" not in ret.stdout:
                    break
                time.sleep(0.3)
            # Start the dummy service so the SDK's ServiceManager finds it running
            subprocess.run(["sc", "start", SERVICE_NAME],
                           capture_output=True, creationflags=_SW_HIDE)
            time.sleep(0.3)

            # Start commservice in-process
            self.after(0, lambda: self.launch_status.config(text="Starting auth service...", fg=FG_DIM))
            try:
                self.comm_server = start_commservice_thread()
            except Exception as e:
                self.after(0, lambda: self.launch_status.config(
                    text=f"Auth service failed: {e}", fg=ERROR_FG))

            # Clear SDK cache
            clear_galaxy_cache()

            # Launch Gwent
            self.after(0, lambda: self.launch_status.config(text="Gwent is running", fg=SUCCESS_FG))

            # Minimize launcher while game runs
            self.after(0, self.iconify)

            game_proc = subprocess.Popen([gwent_exe], cwd=gwent_path)
            game_proc.wait()

        except Exception as e:
            self.after(0, lambda: self.launch_status.config(text=f"Error: {e}", fg=ERROR_FG))
        finally:
            # Cleanup (shared with on_close / shutdown blocker). After a normal
            # match we must re-arm for the NEXT launch, so reset the guard and
            # adapter afterwards.
            self._do_cleanup()
            self.adapter_name = None
            self._cleaned_up = False

            self.after(0, self.deiconify)
            self.after(0, lambda: self.launch_status.config(text="Gwent closed", fg=FG_DIM))
            self.after(0, lambda: self.play_btn.config(state="normal", bg=BTN_BG))

    # ── Helpers ───────────────────────────────────────────────────────────

    def _clear(self):
        for widget in self.winfo_children():
            widget.destroy()

    def _do_cleanup(self):
        """Idempotent teardown: stop commservice, restore the GalaxyComm
        service binary, re-enable IPv6, restore DNS. Safe to call more than
        once (restore_dns / the registry sweep are no-ops when already clean)."""
        if getattr(self, "_cleaned_up", False):
            return
        if self.comm_server:
            try:
                self.comm_server.shutdown()
            except Exception:
                pass
            self.comm_server = None
        try:
            restore_service_binary()
        except Exception:
            pass
        try:
            enable_ipv6(self.adapter_name)
        except Exception:
            pass
        try:
            restore_dns(self.adapter_name)
        except Exception:
            pass
        mark_dns_restored()
        self._cleaned_up = True

    def on_close(self):
        self._do_cleanup()
        self.destroy()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not is_admin():
        # Re-launch elevated
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            " ".join(f'"{a}"' for a in sys.argv),
            None, 1
        )
        sys.exit(0)
        
    # The launcher runs in place from wherever the exe lives; all state is kept
    # under %USERPROFILE%\AppData\LocalLow\CDProjektRED\Gwent\Gwent Beta
    # Launcher (DATA_DIR), so
    # there is no separate install-location step. GwentLauncher shows the setup
    # screen on first run and the main screen once an account is configured.

    app = GwentLauncher()
    app.mainloop()


if __name__ == "__main__":
    main()