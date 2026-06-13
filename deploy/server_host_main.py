#!/usr/bin/env python3
"""
Frozen-aware entrypoint for the bundled Gwent server host (GwentServerHost.exe).

PyInstaller freezes everything into one exe with no separate .py files to shell
out to, and server.py/broker.py/relay.py each run blocking code at import time.
So this entrypoint uses the standard "re-exec self with a role flag" pattern:

  - `GwentServerHost.exe --role server`  -> runs server.py's code
  - `GwentServerHost.exe --role broker`  -> runs broker.py's code
  - `GwentServerHost.exe --role relay`   -> runs relay.py's code
  - `GwentServerHost.exe` (no role)      -> ORCHESTRATOR: sets up env, spawns the
                                            three role children + nginx, supervises,
                                            tears down cleanly.

Running from source (not frozen) works too: the orchestrator shells out to this
same file with `--role`, and the role brnach imports the sibling module.

Run `setup_local_server.py` first to produce local_server.cfg.json.
"""

import os
import sys
import json
import time
import runpy
import subprocess

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
# When frozen, data files are unpacked to sys._MEIPASS.
BUNDLE = getattr(sys, "_MEIPASS", HERE)


# ---------------------------------------------------------------------------
# Role children: run a single component's code in this process.
# ---------------------------------------------------------------------------
def run_role(role):
    mod = {"server": "server", "broker": "broker", "relay": "relay"}.get(role)
    if not mod:
        sys.exit(f"unknown role: {role}")
    # In a windowed (GUI) build the child has no console; send its output to the
    # shared log file so failures are debuggable.
    try:
        logf = open(os.path.join(HERE, "server_host.log"), "a", encoding="utf-8")
        sys.stdout = sys.stderr = logf
        print(f"\n---- role={role} start ----")
        logf.flush()
    except Exception:
        pass
    # The component modules live next to this file (source) or in the bundle.
    search = [HERE, BUNDLE]
    for d in search:
        cand = os.path.join(d, f"{mod}.py")
        if os.path.isfile(cand):
            # Execute the module as __main__ so its top-level server loop runs.
            sys.argv = [cand]
            runpy.run_path(cand, run_name="__main__")
            return
    sys.exit(f"ERROR: {mod}.py not found in {search}")


# ---------------------------------------------------------------------------
# Orchestrator: env, spawn children + nginx, supervise, teardown.
# ---------------------------------------------------------------------------
def cfg_path():
    for d in (HERE, BUNDLE):
        p = os.path.join(d, "local_server.cfg.json")
        if os.path.isfile(p):
            return p
    return os.path.join(HERE, "local_server.cfg.json")


def load_cfg():
    p = cfg_path()
    if not os.path.isfile(p):
        sys.exit("ERROR: local_server.cfg.json not found. Run setup_local_server.py first.")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def self_cmd(role):
    """Command to launch this same program in a given role."""
    if FROZEN:
        return [sys.executable, "--role", role]
    return [sys.executable, os.path.abspath(__file__), "--role", role]


def find_nginx():
    for c in (os.path.join(HERE, "Nginx", "nginx.exe"),
              os.path.join(BUNDLE, "Nginx", "nginx.exe"),
              os.path.join(HERE, "..", "Nginx", "nginx.exe")):
        if os.path.isfile(c):
            return os.path.abspath(c)
    from shutil import which
    return which("nginx")


def find_host_conf():
    for c in (os.path.join(HERE, "host_nginx.conf"),
              os.path.join(BUNDLE, "host_nginx.conf"),
              os.path.join(HERE, "..", "host_nginx.conf")):
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


def materialize_nginx_conf(src_conf, prefix, data_dir):
    """Rewrite the host nginx conf with ABSOLUTE paths and return the new path.

    nginx on Windows does not reliably resolve relative ssl_certificate / root /
    temp paths against the -p prefix (it uses the prefix compiled into nginx.exe).
    So we bake absolute paths in: cert + logs + temp under the bundled Nginx
    prefix, and the static html roots under the per-run data dir's static folder.
    """
    def abs(*parts):
        # Double-quote: Windows paths contain spaces, and an unquoted path makes
        # nginx see multiple arguments ("invalid number of arguments").
        return '"' + os.path.abspath(os.path.join(*parts)).replace("\\", "/") + '"' 

    with open(src_conf, "r", encoding="utf-8") as f:
        conf = f.read()

    nginx_dir = prefix                       # ...\Nginx
    static_dir = os.path.join(data_dir, "static")
    os.makedirs(os.path.join(nginx_dir, "logs"), exist_ok=True)
    os.makedirs(os.path.join(nginx_dir, "temp"), exist_ok=True)

    repl = {
        "conf/fake.crt": abs(nginx_dir, "conf", "fake.crt"),
        "conf/fake.key": abs(nginx_dir, "conf", "fake.key"),
        "error_log  logs/error.log": "error_log  " + abs(nginx_dir, "logs", "error.log"),
        "pid        logs/nginx.pid": "pid        " + abs(nginx_dir, "logs", "nginx.pid"),
        "access_log             logs/access.log":
            "access_log             " + abs(nginx_dir, "logs", "access.log"),
        "temp/client_body_temp": abs(nginx_dir, "temp", "client_body_temp"),
        "temp/proxy_temp": abs(nginx_dir, "temp", "proxy_temp"),
        "temp/fastcgi_temp": abs(nginx_dir, "temp", "fastcgi_temp"),
        "temp/uwsgi_temp": abs(nginx_dir, "temp", "uwsgi_temp"),
        "temp/scgi_temp": abs(nginx_dir, "temp", "scgi_temp"),
        "root html/shop": "root " + abs(static_dir, "shop"),
        "alias html/card_definitions/prices": "alias " + abs(static_dir, "card_definitions", "prices"),
        "root html": "root " + abs(static_dir),
    }
    for k, v in repl.items():
        conf = conf.replace(k, v)

    out = os.path.join(nginx_dir, "host_nginx.resolved.conf")
    with open(out, "w", encoding="utf-8") as f:
        f.write(conf)
    return out


def open_firewall():
    """Add inbound Windows Firewall rules so friends on the LAN can connect.

    The server binds 0.0.0.0, but fresh Windows blocks inbound on these ports,
    which a remote client sees as 'connection refused'. The host exe runs
    elevated (uac_admin), so we can add the rules. Idempotent: delete then add.
    Best-effort; failures are logged, not fatal (host-only play still works).
    """
    if os.name != "nt":
        return
    ports = [("GwentBeta 443 (HTTPS)", 443),
             ("GwentBeta 7777 (relay)", 7777),
             ("GwentBeta 8445 (broker)", 8445),
             ("GwentBeta 8447 (invites)", 8447)]
    for name, port in ports:
        try:
            subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule",
                            f"name={name}"], capture_output=True, timeout=10)
            r = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule",
                                f"name={name}", "dir=in", "action=allow",
                                "protocol=TCP", f"localport={port}", "profile=any"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                print(f"[host] firewall: allowed inbound TCP {port}")
            else:
                print(f"[host] firewall: could not open {port}: {r.stdout.strip()} {r.stderr.strip()}")
        except Exception as e:
            print(f"[host] firewall: error opening {port}: {e}")


def orchestrate(stop_event=None):
    # Ensure our prints reach the (possibly redirected) log promptly.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    cfg = load_cfg()
    env = os.environ.copy()
    env["GWENT_DATA_DIR"] = cfg.get("GWENT_DATA_DIR", os.path.join(HERE, "data"))
    env["GWENT_USE_SQLITE"] = cfg.get("GWENT_USE_SQLITE", "1")
    env["GWENT_SERVER_IP"] = cfg.get("GWENT_SERVER_IP", "127.0.0.1")
    # Point server.py at the bundled cert explicitly (frozen cwd is unreliable).
    _certdir = os.path.join(BUNDLE, "Nginx", "conf")
    if os.path.isfile(os.path.join(_certdir, "fake.crt")):
        env["GWENT_CERT_DIR"] = _certdir

    print(f"[host] data dir  : {env['GWENT_DATA_DIR']}")
    print(f"[host] server IP : {env['GWENT_SERVER_IP']}")

    # Open the LAN firewall so friends can reach this host.
    open_firewall()

    procs, nginx_proc, nginx_path = [], None, None
    try:
        for role in ("server", "broker", "relay"):
            print(f"[host] starting {role} ...")
            procs.append((role, subprocess.Popen(self_cmd(role), env=env, cwd=HERE)))
            time.sleep(0.4)

        nginx_path = find_nginx()
        conf = find_host_conf()
        if nginx_path and conf:
            prefix = os.path.dirname(nginx_path)
            try:
                conf = materialize_nginx_conf(conf, prefix, env["GWENT_DATA_DIR"])
                print(f"[host] resolved nginx conf -> {conf}")
            except Exception as e:
                print(f"[host] WARN: could not materialize nginx conf: {e}")
            print(f"[host] starting nginx (prefix={prefix}) ...")
            # Validate config first so problems are visible instead of silent.
            try:
                test = subprocess.run([nginx_path, "-p", prefix, "-c", conf, "-t"],
                                      capture_output=True, text=True, timeout=15)
                if test.returncode != 0:
                    print("[host] ERROR: nginx config test failed:")
                    print(test.stdout)
                    print(test.stderr)
            except Exception as e:
                print(f"[host] WARN: could not test nginx config: {e}")
            try:
                nginx_proc = subprocess.Popen([nginx_path, "-p", prefix, "-c", conf])
                time.sleep(1.0)
                # Surface any nginx startup error.
                errlog = os.path.join(prefix, "logs", "error.log")
                if os.path.isfile(errlog):
                    try:
                        with open(errlog, "r", encoding="utf-8", errors="replace") as ef:
                            tail = ef.readlines()[-15:]
                        if tail:
                            print("[host] nginx error.log (tail):")
                            for line in tail:
                                print("    " + line.rstrip())
                    except Exception:
                        pass
            except Exception as e:
                print(f"[host] WARN: nginx failed to start: {e}")
        else:
            print("[host] WARN: nginx or host_nginx.conf missing — vhost routing on :443 "
                  "won't work. The client needs it.")

        print("\n[host] Server running. Ctrl+C to stop.")
        if env["GWENT_SERVER_IP"] == "127.0.0.1":
            print("[host] localhost only. Re-run setup with --server-ip <LAN IP> for a friend to join.")
        else:
            print(f"[host] Share this IP with your friend: {env['GWENT_SERVER_IP']}")
        try:
            sys.stdout.flush()
        except Exception:
            pass

        while True:
            time.sleep(1)
            if stop_event is not None and stop_event.is_set():
                print("[host] stop requested.")
                break
            for name, p in procs:
                if p.poll() is not None:
                    print(f"[host] {name} exited (code {p.returncode}). Shutting down.")
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\n[host] stopping...")
    finally:
        if nginx_proc is not None and nginx_path:
            try:
                subprocess.run([nginx_path, "-p", os.path.dirname(nginx_path), "-s", "quit"], timeout=10)
            except Exception:
                try:
                    nginx_proc.terminate()
                except Exception:
                    pass
        for _, p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for _, p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        print("[host] stopped.")


def run_setup():
    """Run the setup step in-process (so the frozen exe needs no separate Python).

    Passes through any extra args after --setup (e.g. the client path,
    --server-ip, --data-dir) to setup_local_server.py's argument parser.
    """
    for d in (HERE, BUNDLE):
        cand = os.path.join(d, "setup_local_server.py")
        if os.path.isfile(cand):
            # Strip the --setup flag; leave the rest as argv for the setup parser.
            extra = [a for a in sys.argv[1:] if a != "--setup"]
            sys.argv = [cand] + extra
            runpy.run_path(cand, run_name="__main__")
            return
    sys.exit("ERROR: setup_local_server.py not found in bundle.")


def main():
    # Setup dispatch (frozen exe can run setup without a separate Python).
    if "--setup" in sys.argv:
        run_setup()
        return
    # Role dispatch.
    if "--role" in sys.argv:
        i = sys.argv.index("--role")
        try:
            role = sys.argv[i + 1]
        except IndexError:
            sys.exit("ERROR: --role needs a value (server|broker|relay)")
        run_role(role)
        return
    orchestrate()


if __name__ == "__main__":
    main()
