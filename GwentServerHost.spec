# -*- mode: python ; coding: utf-8 -*-
# Builds GwentServerHost.exe - a self-contained same-network Gwent private server.
# Bundles server/broker/relay/db + extraction/setup scripts + nginx + static
# templates + host nginx config. No Python install required on the host machine.

import os

block_cipher = None

datas = [
    # Server component modules (executed via --role re-exec).
    ('deploy/server.py', '.'),
    ('deploy/broker.py', '.'),
    ('deploy/relay.py', '.'),
    ('deploy/db.py', '.'),
    ('deploy/server_host_main.py', '.'),
    ('deploy/server_host_gui.py', '.'),
    # Setup / extraction helpers (so the host can run them from the bundle).
    ('deploy/extract_data_definitions.py', '.'),
    ('deploy/setup_local_server.py', '.'),
    ('deploy/run_local_server.py', '.'),
    # nginx + host config + TLS cert.
    ('Nginx', 'Nginx'),
    ('host_nginx.conf', '.'),
    # Blank static templates (shop/prices/config/news).
    ('deploy/static', 'static'),
]

a = Analysis(
    ['deploy/server_host_gui.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['socketserver', 'http.server', 'http.client', 'sqlite3',
                   'asyncio', 'ssl', 'xml.etree.ElementTree',
                   'websockets', 'websockets.server', 'websockets.legacy',
                   'websockets.legacy.server'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GwentServerHost',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # GUI front-end; role children log to file
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,          # binding :443
)
