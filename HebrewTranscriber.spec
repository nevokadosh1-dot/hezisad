# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Hebrew Lecture Transcriber desktop app.

Build via build_windows.py (recommended):
    python build_windows.py            # onedir (recommended)
    python build_windows.py --onefile  # single exe (slower startup)

Or directly:
    pyinstaller HebrewTranscriber.spec --noconfirm
    (set HEB_BUILD_ONEFILE=1 in the environment for a onefile build)

Notes:
  * onedir is the recommended, more reliable layout: dist/HebrewTranscriber/
    contains HebrewTranscriber.exe plus an _internal/ folder with DLLs.
  * The Whisper model is intentionally NOT bundled (1-3 GB); it downloads to
    the user's Hugging Face cache on first run.
  * If a bin/ folder with ffmpeg.exe + ffprobe.exe exists next to this spec,
    it is bundled and found automatically by the app at runtime.
"""

import os

from PyInstaller.utils.hooks import collect_all

ONEFILE = os.environ.get("HEB_BUILD_ONEFILE") == "1"
HERE = os.path.dirname(os.path.abspath(SPEC))

datas = [
    (os.path.join(HERE, "finance_glossary.txt"), "."),
    (os.path.join(HERE, "assets"), "assets"),
]
binaries = []
hiddenimports = []

# Bundle ffmpeg/ffprobe when the builder provides them in ./bin (optional).
if os.path.isdir(os.path.join(HERE, "bin")):
    datas.append((os.path.join(HERE, "bin"), "bin"))

# faster-whisper and its native dependencies ship data files and DLLs that
# PyInstaller's static analysis misses; collect them completely.
for package in ("faster_whisper", "ctranslate2", "tokenizers",
                "huggingface_hub", "onnxruntime", "av"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden
    except Exception:
        pass  # optional/absent packages are simply skipped

icon_path = os.path.join(HERE, "assets", "icon.ico")
icon = icon_path if os.path.exists(icon_path) else None

a = Analysis(
    ["gui_app.py"],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["torch", "tkinter", "matplotlib", "IPython", "jupyter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="HebrewTranscriber",
        debug=False,
        strip=False,
        upx=False,
        console=False,          # GUI app: no console window
        icon=icon,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="HebrewTranscriber",
        debug=False,
        strip=False,
        upx=False,
        console=False,          # GUI app: no console window
        icon=icon,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="HebrewTranscriber",
    )
