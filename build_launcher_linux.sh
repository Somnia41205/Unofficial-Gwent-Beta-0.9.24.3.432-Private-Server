#!/usr/bin/env bash
# Build the Linux / Steam Deck launcher as a single executable via PyInstaller.
#
# Output: dist/GwentBetaLauncher-Linux
#
# Run this on a Linux box. On a Steam Deck, switch to Desktop Mode and run
# from Konsole with `chmod +x build_launcher_linux.sh && ./build_launcher_linux.sh`.
#
# Requirements:
#   * Python 3.9+
#   * PyInstaller   (pip install --user pyinstaller)
#   * The bundled assets in this repo (fake.crt/key, mod DLL, settings/)

set -euo pipefail
cd "$(dirname "$0")"

OUT_NAME="GwentBetaLauncher-Linux"
ENTRY="linux_launcher.py"

# ── Pre-flight: required files exist ─────────────────────────────────────────
required=(
    "$ENTRY"
    "commservice.py"
    "dns_proxy.py"
    "https_proxy.py"
    "Nginx/conf/fake.crt"
    "Nginx/conf/fake.key"
    "GwentBetaRestorationMod/GwentBetaRestorationMod.dll"
    "comet-main/dummy-service/GalaxyCommunication.exe"
    "settings/config.json"
    "settings/Launch.cfg"
)
for f in "${required[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: missing required file: $f" >&2
        exit 1
    fi
done

# ── DLL freshness check ────────────────────────────────
DLL="GwentBetaRestorationMod/GwentBetaRestorationMod.dll"
echo "[ok] mod DLL present"

# ── Resolve the server host (kept out of source) ─────────────────────────────
# Priority: GWENT_SERVER_HOST env var, else the private server.txt file.
# Written to server_host.txt and bundled into the binary.
if [[ -n "${GWENT_SERVER_HOST:-}" ]]; then
    printf '%s\n' "$GWENT_SERVER_HOST" > server_host.txt
elif [[ -f server.txt ]]; then
    cp -f server.txt server_host.txt
else
    echo "ERROR: no server host configured." >&2
    echo "  Set GWENT_SERVER_HOST, or create server.txt with one line: your.host.or.ip" >&2
    echo "  (edit server.txt and put your server's url or ip on the first line)" >&2
    exit 1
fi
echo "[ok] server host resolved into server_host.txt"

# ── PyInstaller invocation ───────────────────────────────────────────────────
# On Linux the path separator for --add-data is ':' (Windows uses ';').

PI_FLAGS=(
    --onefile
    --name "$OUT_NAME"
    --add-data "commservice.py:."
    --add-data "dns_proxy.py:."
    --add-data "https_proxy.py:."
    --add-data "Nginx/conf/fake.crt:."
    --add-data "Nginx/conf/fake.key:."
    --add-data "$DLL:."
    --add-data "comet-main/dummy-service/GalaxyCommunication.exe:."
    --add-data "settings/config.json:settings"
    --add-data "settings/Launch.cfg:settings"
    --add-data "server_host.txt:."
    --hidden-import socketserver
    --hidden-import http.server
    --hidden-import http.client
    --hidden-import importlib.util
)

# Strip stale build artefacts so old data files don't sneak in.
rm -rf build dist "${OUT_NAME}.spec"

python3 -m PyInstaller "${PI_FLAGS[@]}" "$ENTRY"

# ── Package as a .tar.gz so the executable bit survives the download ──────────
# A bare binary or a .zip loses its +x permission when downloaded/extracted on
# most systems, forcing the user to run `chmod +x`. tar preserves the bit, so
# the extracted binary is runnable straight away — no chmod step for the user.
chmod +x "dist/${OUT_NAME}"
TARBALL="${OUT_NAME}.tar.gz"
tar -C dist -czf "dist/${TARBALL}" "${OUT_NAME}"

echo
echo "Built: dist/${OUT_NAME}"
echo "Shareable archive: dist/${TARBALL}  (preserves the executable bit)"
echo
echo "Give players the .tar.gz. To run it (incl. on a Steam Deck in Desktop Mode):"
echo "  tar -xf ${TARBALL}"
echo "  ./${OUT_NAME}        # no chmod needed; it will prompt for your sudo password"
echo
