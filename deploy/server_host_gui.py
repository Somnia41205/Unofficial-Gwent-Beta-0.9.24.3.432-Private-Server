#!/usr/bin/env python3
"""
GUI for the Gwent server host. Single screen, themed to match the launcher:

  - Browse to your Gwent client folder (auto-detected if possible).
  - Your LAN IP shown with a Copy button (share it with a friend).
  - Start Server: runs setup (extract definitions + static + config) then
    launches server/broker/relay + nginx, and minimizes.
  - Stop Server: tears everything down.

Reuses the same logic as the console host (extract_data_definitions.run_extraction
and server_host_main's orchestration), so nothing is duplicated. Full server
output is written to a log file (shown in the status line) for debugging.

This is the GUI front-end; server_host_main.py remains the console/role entry.
"""

import os
import sys
import socket
import threading
import time
import datetime
import tkinter as tk
from tkinter import filedialog

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)
for _d in (HERE, BUNDLE):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import extract_data_definitions as _extractor
import setup_local_server as _setup
import server_host_main as _host

# Theme (matches launcher.py).
BG_DARK = "#1a1a2e"; BG_CARD = "#16213e"; BG_INPUT = "#0f3460"
FG_TEXT = "#e0e0e0"; FG_DIM = "#8888aa"; FG_TITLE = "#e8d5a3"
BTN_BG = "#c9a84c"; BTN_FG = "#1a1a2e"
ERROR_FG = "#ff6b6b"; SUCCESS_FG = "#51cf66"

LOG_PATH = os.path.join(HERE, "server_host.log")


def detect_lan_ip():
    """Best-effort LAN IP (the address a friend on your network would use)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the route's iface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def detect_client():
    try:
        return _extractor.find_client(None) or ""
    except SystemExit:
        return ""
    except Exception:
        return ""


class HostGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gwent Private Server Host")
        self.configure(bg=BG_DARK)
        self.geometry("520x340")
        self.resizable(False, False)

        self.running = False
        self.host_thread = None
        self.stop_event = threading.Event()

        tk.Label(self, text="Gwent Private Server Host", font=("Segoe UI", 14, "bold"),
                 fg=FG_TITLE, bg=BG_DARK).pack(pady=(16, 4))
        tk.Label(self, text="Host a match on this PC. Share your IP with a friend.",
                 font=("Segoe UI", 9), fg=FG_DIM, bg=BG_DARK).pack()

        # Client path
        pf = tk.Frame(self, bg=BG_DARK); pf.pack(fill="x", padx=20, pady=(16, 4))
        tk.Label(pf, text="Gwent Installation", font=("Segoe UI", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w")
        row = tk.Frame(pf, bg=BG_DARK); row.pack(fill="x", pady=(2, 0))
        self.path_var = tk.StringVar(value=detect_client())
        tk.Entry(row, textvariable=self.path_var, font=("Segoe UI", 10), bg=BG_INPUT,
                 fg=FG_TEXT, insertbackground=FG_TEXT, relief="flat", bd=0).pack(
                     side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
        tk.Button(row, text="Browse", font=("Segoe UI", 9), bg=BG_CARD, fg=FG_TEXT,
                  relief="flat", command=self.browse).pack(side="right", ipady=2, ipadx=8)

        # LAN IP + copy
        sf = tk.Frame(self, bg=BG_DARK); sf.pack(fill="x", padx=20, pady=(10, 4))
        tk.Label(sf, text="Your Server IP (share with a friend)", font=("Segoe UI", 9),
                 fg=FG_DIM, bg=BG_DARK).pack(anchor="w")
        row2 = tk.Frame(sf, bg=BG_DARK); row2.pack(fill="x", pady=(2, 0))
        self.ip_var = tk.StringVar(value=detect_lan_ip())
        tk.Entry(row2, textvariable=self.ip_var, font=("Segoe UI", 11, "bold"), bg=BG_INPUT,
                 fg=FG_TITLE, insertbackground=FG_TEXT, relief="flat", bd=0).pack(
                     side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
        tk.Button(row2, text="Copy", font=("Segoe UI", 9), bg=BG_CARD, fg=FG_TEXT,
                  relief="flat", command=self.copy_ip).pack(side="right", ipady=2, ipadx=8)

        # Start/Stop
        self.start_btn = tk.Button(self, text="Start Server", font=("Segoe UI", 11, "bold"),
                                   bg=BTN_BG, fg=BTN_FG, relief="flat", command=self.toggle)
        self.start_btn.pack(pady=(16, 6), ipady=6, ipadx=24)

        self.status = tk.Label(self, text="Stopped.", font=("Segoe UI", 9),
                               fg=FG_DIM, bg=BG_DARK, wraplength=480, justify="center")
        self.status.pack(pady=(2, 8))

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- UI helpers --
    def browse(self):
        d = filedialog.askdirectory(title="Select your Gwent install folder")
        if d:
            self.path_var.set(d)

    def copy_ip(self):
        self.clipboard_clear()
        self.clipboard_append(self.ip_var.get().strip())
        self.set_status("Copied " + self.ip_var.get().strip() + " to clipboard.", SUCCESS_FG)

    def set_status(self, msg, color=FG_DIM):
        self.status.config(text=msg, fg=color)
        self.update_idletasks()

    # -- start/stop --
    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        client = self.path_var.get().strip()
        ip = self.ip_var.get().strip() or "127.0.0.1"
        if not client or not _extractor.is_client(client):
            self.set_status("Select a valid Gwent install folder first.", ERROR_FG)
            return
        self.start_btn.config(state="disabled")
        self.set_status("Setting up (extracting card data)...", FG_TEXT)
        self.host_thread = threading.Thread(target=self._run, args=(client, ip), daemon=True)
        self.host_thread.start()

    def _run(self, client, ip):
        # Redirect server output to a log file for later debugging.
        try:
            log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)  # line-buffered
            log.write("\n==== host start %s ====\n" % datetime.datetime.now().isoformat())
            log.flush()
            sys.stdout = sys.stderr = log
        except Exception:
            log = None
        try:
            data_dir = os.path.join(HERE, "data")
            data_def = os.path.join(data_dir, "Data_definitions")
            os.makedirs(data_def, exist_ok=True)
            _setup.run_extractor(client, data_def)
            _setup.ensure_static(data_dir)
            _setup.ensure_rewards()
            _setup.write_run_config(data_dir, ip)
            self.after(0, lambda: self.set_status(
                "Server running on " + ip + ". Log: " + LOG_PATH, SUCCESS_FG))
            self.after(0, lambda: self.start_btn.config(text="Stop Server", state="normal"))
            self.after(0, lambda: setattr(self, "running", True))
            self.after(400, self.iconify)  # minimize once up
            _host.orchestrate(self.stop_event)  # blocks until stop_event set
        except SystemExit as e:
            self.after(0, lambda: self.set_status("Setup failed: " + str(e) +
                                                  " (see " + LOG_PATH + ")", ERROR_FG))
            self.after(0, lambda: self.start_btn.config(text="Start Server", state="normal"))
            self.after(0, lambda: setattr(self, "running", False))
        except Exception as e:
            self.after(0, lambda: self.set_status("Error: " + str(e) +
                                                  " (see " + LOG_PATH + ")", ERROR_FG))
            self.after(0, lambda: self.start_btn.config(text="Start Server", state="normal"))
            self.after(0, lambda: setattr(self, "running", False))
        finally:
            if log:
                try:
                    log.flush()
                except Exception:
                    pass

    def stop(self):
        self.set_status("Stopping...", FG_TEXT)
        self.start_btn.config(state="disabled")
        self.stop_event.set()  # orchestrate() sees this, tears down nginx + children
        # Give teardown a moment, then reset the UI.
        def _finish():
            self.running = False
            self.stop_event = threading.Event()
            self.start_btn.config(text="Start Server", state="normal")
            self.set_status("Stopped.", FG_DIM)
        self.after(2500, _finish)

    def on_close(self):
        # Signal teardown so nginx gets `-s quit` and children are terminated.
        try:
            self.stop_event.set()
        except Exception:
            pass
        time.sleep(1.5)
        try:
            self.destroy()
        finally:
            os._exit(0)  # belt-and-suspenders: no lingering children


def main():
    # The frozen exe is ALSO the role/setup child (orchestrate re-execs this same
    # exe with --role/--setup). Dispatch those BEFORE opening any window, or each
    # re-exec would just spawn another GUI instead of running a server role.
    if "--role" in sys.argv:
        i = sys.argv.index("--role")
        try:
            role = sys.argv[i + 1]
        except IndexError:
            sys.exit("ERROR: --role needs a value (server|broker|relay)")
        _host.run_role(role)
        return
    if "--setup" in sys.argv:
        _host.run_setup()
        return
    HostGUI().mainloop()


if __name__ == "__main__":
    main()
