# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Automation Studio backend (onedir).
# Bundles the orchestrator + humanbrowser + automations + their heavy deps,
# crucially the patchright Node driver (collect_all pulls driver/ as data).
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas, binaries, hiddenimports = [], [], []

# Packages whose data files / submodules must be collected fully.
for pkg in [
    "patchright", "uvicorn", "fastapi", "starlette", "aiohttp", "psutil",
    "anyio", "sniffio", "platformdirs", "click", "h11",
    "pydantic", "pydantic_core",
    "humanbrowser", "automations", "orchestrator",
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# ensure humanbrowser's injected JS is bundled
datas += collect_data_files("humanbrowser", includes=["*.js"])

a = Analysis(
    ["_pyi_entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["uvicorn.logging", "uvicorn.loops.auto",
                                   "uvicorn.protocols.http.auto",
                                   "uvicorn.protocols.websockets.auto",
                                   "uvicorn.lifespan.on"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="automation-backend",
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="automation-backend")
