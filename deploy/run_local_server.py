#!/usr/bin/env python3
"""
Run the same-PC Gwent private server: server.py + broker.py + relay.py, plus
nginx if a bundled copy is available.

Reads local_server.cfg.json (written by setup_local_server.py) for the data dir,
SQLite flag, and advertised server IP. Starts each component as a child process
with the right environment, streams their output, and shuts them all down
cleanly on Ctrl+C / exit.

Run setup_local_server.py first.

Usage:
    python run_local_server.py
"""

import os
import sys
import json
import signal
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "local_server.cfg.json")

# Server components (script, optional args). commservice.py is CLIENT-side and
# is intentionally NOT started here.
COMPONENTS = [
    ("server.py", []),
    ("broker.py", []),
    ("relay.py", []),
]


def load_cfg():
    if not os.path.isfile(CFG_PATH):
        sys.exit("ERROR: local_server.cfg.json not found. Run setup_local_server.py first.")
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def find_nginx():
    """Look for a bundled nginx (Windows nginx.exe or a system nginx)."""
    candidates = [
        os.path.join(HERE, "Nginx", "nginx.exe"),
        os.path.join(HERE, "..", "Nginx", "nginx.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    # System nginx (Linux/mac), optional.
    from shutil import which
    return which("nginx")


def start_component(script, extra_args, env):
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        sys.exit(f"ERROR: {script} not found in {HERE}.")
    print(f"[run] starting {script} ...")
    return subprocess.Popen([sys.executable, path, *extra_args], env=env, cwd=HERE)


def start_nginx(nginx_path):
    """Start nginx with the host config. Prefix points at the Nginx/ folder so
    the relative paths in host_nginx.conf resolve."""
    conf = os.path.join(HERE, "..", "host_nginx.conf")
    conf = os.path.abspath(conf)
    prefix = os.path.dirname(nginx_path)
    if not os.path.isfile(conf):
        print(f"[run] WARN: host_nginx.conf not found at {conf} — skipping nginx. "
              "The game needs nginx for vhost routing; start it manually.")
        return None
    print(f"[run] starting nginx ({nginx_path}) ...")
    try:
        return subprocess.Popen([nginx_path, "-p", prefix, "-c", conf])
    except Exception as e:
        print(f"[run] WARN: could not start nginx: {e}")
        return None


def main():
    cfg = load_cfg()
    env = os.environ.copy()
    env["GWENT_DATA_DIR"] = cfg.get("GWENT_DATA_DIR", os.path.join(HERE, "data"))
    env["GWENT_USE_SQLITE"] = cfg.get("GWENT_USE_SQLITE", "1")
    env["GWENT_SERVER_IP"] = cfg.get("GWENT_SERVER_IP", "127.0.0.1")

    print(f"[run] data dir   : {env['GWENT_DATA_DIR']}")
    print(f"[run] server IP  : {env['GWENT_SERVER_IP']}")
    print(f"[run] sqlite     : {env['GWENT_USE_SQLITE']}")

    procs = []
    nginx_proc = None
    try:
        for script, args in COMPONENTS:
            procs.append((script, start_component(script, args, env)))
            time.sleep(0.4)

        nginx_path = find_nginx()
        if nginx_path:
            nginx_proc = start_nginx(nginx_path)
        else:
            print("[run] WARN: no nginx found. The client needs nginx's vhost routing on "
                  "port 443; install/bundle nginx and re-run, or start it yourself.")

        print()
        print("[run] All components running. Press Ctrl+C to stop.")
        if env["GWENT_SERVER_IP"] == "127.0.0.1":
            print("[run] (localhost only. For a friend to join, re-run setup with "
                  "--server-ip <your-LAN-IP>.)")
        else:
            print(f"[run] Share this IP with your friend: {env['GWENT_SERVER_IP']}")

        # Wait until interrupted or a component dies.
        while True:
            time.sleep(1)
            for name, p in procs:
                if p.poll() is not None:
                    print(f"[run] {name} exited (code {p.returncode}). Shutting down.")
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\n[run] stopping...")
    finally:
        if nginx_proc is not None:
            try:
                nginx_path = find_nginx()
                subprocess.run([nginx_path, "-p", os.path.dirname(nginx_path), "-s", "quit"],
                               timeout=10)
            except Exception:
                try:
                    nginx_proc.terminate()
                except Exception:
                    pass
        for name, p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for name, p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        print("[run] all components stopped.")


if __name__ == "__main__":
    main()
