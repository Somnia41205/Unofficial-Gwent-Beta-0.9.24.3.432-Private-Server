#!/usr/bin/env python3
"""
One-shot setup for a SAME-PC (localhost / LAN) Gwent private server.

  1. Extracts card data-definitions from YOUR OWN Gwent client into the server
     data dir. Nothing CDPR-owned is shipped - it's copied from your install.
  2. Ensures the blank static files (shop/prices/config/news) are present.
  3. Ensures a rewards.json stub exists next to the server.
  4. Creates the data dir and writes a small run config.

Usage:
    python setup_local_server.py
    python setup_local_server.py "D:\\path\\to\\Gwent The Witcher Card Game"
    python setup_local_server.py --server-ip 192.168.1.50
"""

import os
import sys
import json
import shutil
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))


def _log(msg):
    print("[setup] " + msg)


def run_extractor(client_arg, data_def_dir):
    """Extract definitions IN-PROCESS by importing the extractor.

    We deliberately do NOT shell out to `sys.executable extract_data_definitions.py`:
    inside a frozen exe sys.executable is the host exe itself, not a Python
    interpreter, so that subprocess call silently fails. Importing and calling
    the function works in both source and frozen modes.
    """
    _log("Extracting card definitions from your client...")
    for d in (HERE, getattr(sys, "_MEIPASS", HERE)):
        if d not in sys.path:
            sys.path.insert(0, d)
    try:
        import extract_data_definitions as extractor
    except ImportError:
        sys.exit("ERROR: extract_data_definitions module not found in the bundle.")
    extractor.run_extraction(client_arg, data_def_dir)


def ensure_static(data_dir):
    src = os.path.join(HERE, "static")
    dst = os.path.join(data_dir, "static")
    if not os.path.isdir(src):
        _log("WARN: ./static template folder missing - skipping static files.")
        return
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(out, exist_ok=True)
        for f in files:
            s, d = os.path.join(root, f), os.path.join(out, f)
            if not os.path.exists(d):
                shutil.copy2(s, d)
    _log("Static files ready at " + dst + " (blank - populate for a non-empty shop).")


def ensure_rewards():
    path = os.path.join(HERE, "rewards.json")
    if os.path.exists(path):
        return
    stub = {"items": [], "total_count": 0, "limit": 2000,
            "page_token": "1", "next_page_token": None}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stub, f, indent=4)
    _log("Wrote blank rewards.json stub.")


def write_run_config(data_dir, server_ip):
    cfg = {
        "GWENT_DATA_DIR": data_dir,
        "GWENT_USE_SQLITE": "1",
        "GWENT_SERVER_IP": server_ip,
    }
    path = os.path.join(HERE, "local_server.cfg.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    _log("Wrote run config: " + path)
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Set up a same-PC Gwent private server.")
    ap.add_argument("client", nargs="?", help="Path to your Gwent install folder")
    ap.add_argument("--data-dir", default=os.path.join(HERE, "data"),
                    help="Where gwent.db and Data_definitions live (default: ./data)")
    ap.add_argument("--server-ip", default="127.0.0.1",
                    help="IP the server advertises. 127.0.0.1 for solo; your LAN "
                         "IP (e.g. 192.168.1.x) so a friend on your network can join.")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    data_def_dir = os.path.join(data_dir, "Data_definitions")
    os.makedirs(data_def_dir, exist_ok=True)
    _log("Data dir: " + data_dir)

    run_extractor(args.client, data_def_dir)
    ensure_static(data_dir)
    ensure_rewards()
    cfg = write_run_config(data_dir, args.server_ip)

    print()
    _log("Setup complete.")
    _log("  Server IP advertised : " + cfg["GWENT_SERVER_IP"])
    _log("  Database             : " + os.path.join(data_dir, "gwent.db"))
    _log("  Card definitions     : " + data_def_dir)
    print()
    _log("Next: start the server with  python run_local_server.py")
    if cfg["GWENT_SERVER_IP"] == "127.0.0.1":
        _log("(localhost only - re-run with --server-ip <your-LAN-IP> to let a friend join.)")


if __name__ == "__main__":
    main()
