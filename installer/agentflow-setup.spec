# PyInstaller spec for the AgentFlow Desktop self-contained installer.
#
# This bundle is the wizard AND the daemon — the same .exe spawns the GUI
# by default and the headless daemon when invoked with --daemon. We
# collect_all the runtime package + every transitive dep so the user
# never needs Python on their machine.
#
# Build:
#   python installer/make_icon.py
#   pyinstaller installer/agentflow-setup.spec
#
# Output:
#   dist/agentflow-desktop-setup.exe  (~70 MB — intentional, contains
#   full CPython 3.11 + agentflow_computer_mcp + all wheels)

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

a = Analysis(
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
