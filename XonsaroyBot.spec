# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Xonsaroy Support Bot desktop app.
# Build with:  pyinstaller XonsaroyBot.spec
# The resulting exe lives in dist/XonsaroyBot/ (or dist/XonsaroyBot.exe for --onefile).

from PyInstaller.utils.hooks import collect_submodules

hidden = []
hidden += collect_submodules("telegram")
hidden += collect_submodules("telegram.ext")
hidden += collect_submodules("github")
hidden += ["dotenv", "dotenv.main"]

block_cipher = None

a = Analysis(
    ["gui.py"],
    pathex=["."],
    binaries=[],
    datas=[
        (".env.example", "."),
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="XonsaroyBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,      # Windowed app (no console). Logs show in the Logs tab.
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="XonsaroyBot",
)
