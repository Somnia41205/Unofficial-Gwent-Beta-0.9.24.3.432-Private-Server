# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('commservice.py', '.'), ('dns_proxy.py', '.'), ('https_proxy.py', '.'), ('Nginx\\conf\\fake.crt', '.'), ('Nginx\\conf\\fake.key', '.'), ('comet-main\\dummy-service\\GalaxyCommunication.exe', '.'), ('GwentBetaRestorationMod\\GwentBetaRestorationMod.dll', '.'), ('settings', 'settings'), ('server_host.txt', '.')],
    hiddenimports=['socketserver', 'http.server', 'http.client'],
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
    name='GwentBetaLauncher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
)
