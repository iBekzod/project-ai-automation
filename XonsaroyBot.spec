# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Xonsaroy Support Bot desktop app.
# Build with:  pyinstaller XonsaroyBot.spec
# The resulting exe lives in dist/XonsaroyBot/ (or dist/XonsaroyBot.exe for --onefile).

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hidden = []
hidden += collect_submodules("telegram")
hidden += collect_submodules("telegram.ext")
hidden += collect_submodules("github")
hidden += collect_submodules("pystray")  # platform backend (win32/gtk/...)
hidden += collect_submodules("PIL")       # tray icon rendering
hidden += ["dotenv", "dotenv.main", "sv_ttk"]

# sv_ttk ships .tcl + .png theme files that PyInstaller does NOT auto-detect
# from `import sv_ttk`. Without these the GUI would crash with "couldn't read
# file 'sv.tcl'" when the bundled exe tries to apply the dark theme.
datas = [
    (".env.example", "."),
]
datas += collect_data_files("sv_ttk")

block_cipher = None

a = Analysis(
    ["gui.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
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
