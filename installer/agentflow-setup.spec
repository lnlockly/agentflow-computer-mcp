# PyInstaller spec for the AgentFlow Desktop setup wizard.
#
# Build:
#   python installer/make_icon.py
#   pyinstaller installer/agentflow-setup.spec
#
# Output:
#   dist/agentflow-desktop-setup.exe

from pathlib import Path

block_cipher = None

HERE = Path(SPECPATH).resolve()
ICON = HERE / "build_assets" / "agentflow.ico"

a = Analysis(
    [str(HERE / "setup_gui.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="agentflow-desktop-setup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)
