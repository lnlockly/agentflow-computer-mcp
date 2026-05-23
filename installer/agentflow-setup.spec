# PyInstaller spec for the AgentFlow Desktop self-contained installer.
#
# This bundle ships TWO onefile binaries from a single Analysis+PYZ:
#
#   dist/agentflow-desktop-setup.exe  — wizard (default) AND daemon
#                                       (with `--daemon` flag). Built
#                                       from `setup_gui.py`.
#   dist/agentflow-tray.exe           — pystray system-tray app. Built
#                                       from `tray_entry.py`, runs
#                                       `agentflow_computer_mcp.winapp`.
#
# Both share the same CPython 3.11 + `agentflow_computer_mcp` + every
# transitive dep, so the user never needs Python on their machine and
# the two .exes can launch each other without extra runtime hops.
#
# Build:
#   python installer/make_icon.py
#   pyinstaller installer/agentflow-setup.spec
#
# Output:
#   dist/agentflow-desktop-setup.exe  (~50 MB — wizard + daemon)
#   dist/agentflow-tray.exe           (~50 MB — same bundle, tray entry)

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

HERE = Path(SPECPATH).resolve()
ICON = HERE / "build_assets" / "agentflow.ico"

# --- Collect runtime package + every transitive dep ---------------------
# Each collect_all returns (datas, binaries, hiddenimports). We merge
# them and feed PyInstaller a single flat list. Missing a package here
# means a runtime ImportError on the user's PC, so be generous — the
# extra MB on disk are cheaper than a support ticket.

datas = []
binaries = []
hiddenimports = []

BUNDLE_PACKAGES = [
    "agentflow_computer_mcp",
    "mcp",
    "pyautogui",
    "pymsgbox",
    "pyperclip",
    "pytweening",
    "mouseinfo",
    "pygetwindow",
    "pyrect",
    "pyscreeze",
    "mss",
    "PIL",
    "websockets",
    "psutil",
    "win32com",
    "win32api",
    "win32con",
    "win32gui",
    "pywintypes",
    "tomli",
    "anyio",
    "sniffio",
    "httpx",
    "httpcore",
    "h11",
    "certifi",
    "pydantic",
    "pydantic_core",
    "typing_extensions",
    "annotated_types",
    "starlette",
    "uvicorn",
    "click",
    # winapp/ tray deps — bundled so agentflow-tray.exe can boot pystray
    # on a fresh user machine without a separate `pip install`. See
    # `docs/specs/2026-05-23-setup-exe-v050.md`.
    "pystray",
]

for pkg in BUNDLE_PACKAGES:
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hiddenimports
    except Exception as exc:
        # Optional deps (e.g. uvicorn is only pulled in by mcp[cli]) may
        # be missing on the build host. Skip with a warning rather than
        # blowing up the whole build — PyInstaller will surface a real
        # ImportError later if the runtime actually needs them.
        print(f"[spec] skip collect_all({pkg!r}): {exc}")

# Belt-and-suspenders: explicitly list the daemon submodules so any
# lazy `importlib.import_module` inside desktop_cli still resolves.
hiddenimports += collect_submodules("agentflow_computer_mcp")

# ---- Bundle 1: agentflow-desktop-setup.exe (wizard + daemon) -----------
a_setup = Analysis(
    [str(HERE / "setup_gui.py")],
    pathex=[str(HERE), str(HERE.parent / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz_setup = PYZ(a_setup.pure, a_setup.zipped_data, cipher=block_cipher)

exe_setup = EXE(
    pyz_setup,
    a_setup.scripts,
    a_setup.binaries,
    a_setup.zipfiles,
    a_setup.datas,
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

# ---- Bundle 2: agentflow-tray.exe (pystray system-tray) ----------------
# Second onefile from the same package set. Separate Analysis so the EXE
# can have its own entry point + windowed console flag. Reuses the same
# binaries/datas/hiddenimports lists computed above — keeps both .exes
# byte-equivalent on every Python dep, so an auto-update that rolls one
# never leaves the other on a stale wheel.
a_tray = Analysis(
    [str(HERE / "tray_entry.py")],
    pathex=[str(HERE), str(HERE.parent / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz_tray = PYZ(a_tray.pure, a_tray.zipped_data, cipher=block_cipher)

exe_tray = EXE(
    pyz_tray,
    a_tray.scripts,
    a_tray.binaries,
    a_tray.zipfiles,
    a_tray.datas,
    [],
    name="agentflow-tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # console=False → no flicker of a cmd window when pystray boots from
    # the Run-key autostart on user logon.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)
