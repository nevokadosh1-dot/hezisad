#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_windows.py — build the Hebrew Lecture Transcriber desktop executable.

Usage:
    python build_windows.py            # onedir build (RECOMMENDED)
    python build_windows.py --onefile  # single-file exe (slower startup)
    python build_windows.py --check    # verify environment only, no build
    python build_windows.py --skip-tests   # build without running self-tests

Output (onedir):  dist/HebrewTranscriber/HebrewTranscriber.exe
Output (onefile): dist/HebrewTranscriber.exe

The Whisper model is NOT bundled (1-3 GB) — the app downloads and caches it
on first use. To bundle ffmpeg, place ffmpeg.exe + ffprobe.exe in ./bin
before building; otherwise the app finds a system-installed ffmpeg at runtime.
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "HebrewTranscriber"


def info(msg):
    print(f"[build] {msg}")


def fail(msg):
    print(f"[build] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, **kwargs):
    info("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=HERE, **kwargs).returncode


def check_environment() -> bool:
    """Verify build prerequisites. Returns True when everything essential
    is present; prints warnings for optional items."""
    ok = True
    info(f"Python {sys.version.split()[0]} at {sys.executable}")
    if sys.version_info < (3, 9):
        fail("Python 3.9+ is required.")

    for module, package in [
        ("PySide6", "requirements-gui.txt"),
        ("PyInstaller", "requirements-gui.txt"),
        ("faster_whisper", "requirements.txt"),
        ("psutil", "requirements.txt"),
    ]:
        try:
            __import__(module)
            info(f"{module}: OK")
        except ImportError:
            print(f"[build] MISSING: {module} — run: pip install -r {package}")
            ok = False

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        info("ffmpeg/ffprobe: found on PATH (users still need their own, "
             "or bundle it in ./bin)")
    else:
        info("ffmpeg/ffprobe: not on PATH — fine for building, but needed "
             "to run/test the app")

    bundled = os.path.join(HERE, "bin", "ffmpeg.exe")
    if os.path.exists(bundled):
        info("Bundled ffmpeg found in ./bin — it will ship inside the app.")
    else:
        info("No ./bin/ffmpeg.exe — the app will look for a system ffmpeg "
             "at runtime (winget/PATH). To bundle it, place ffmpeg.exe + "
             "ffprobe.exe in ./bin and rebuild.")

    if sys.platform != "win32":
        info(f"NOTE: building on {sys.platform} — PyInstaller produces a "
             f"binary for THIS platform. Build on Windows to get a .exe.")
    return ok


def run_tests() -> None:
    info("Running engine self-test (--skip-model)...")
    if run([sys.executable, "transcribe.py", "--self-test", "--skip-model"]) != 0:
        fail("engine self-test failed — fix transcribe.py before building")
    info("Running GUI smoke test (offscreen)...")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    rc = subprocess.run([sys.executable, "gui_app.py", "--smoke-test"],
                        cwd=HERE, env=env).returncode
    if rc != 0:
        fail("GUI smoke test failed")


def make_icon() -> None:
    """Convert assets/icon.svg to assets/icon.ico when Pillow is available.
    Optional: the build proceeds without an icon otherwise."""
    ico = os.path.join(HERE, "assets", "icon.ico")
    if os.path.exists(ico):
        info("assets/icon.ico already present")
        return
    try:
        from PIL import Image
    except ImportError:
        info("Pillow not installed — building without an embedded exe icon "
             "(optional; pip install pillow to enable)")
        return
    try:
        # Rasterize the SVG via Qt (already a build dependency), then let
        # Pillow write the multi-size .ico.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication, QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer

        app = QGuiApplication.instance() or QGuiApplication([])
        renderer = QSvgRenderer(os.path.join(HERE, "assets", "icon.svg"))
        png_path = os.path.join(HERE, "assets", "icon_256.png")
        image = QImage(256, 256, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        image.save(png_path)
        Image.open(png_path).save(
            ico, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
        os.remove(png_path)
        info(f"Generated {ico}")
    except Exception as exc:
        info(f"Icon generation skipped ({exc}) — building without exe icon")


def clean() -> None:
    for path in ("build", "dist"):
        full = os.path.join(HERE, path)
        if os.path.isdir(full):
            info(f"Removing old {path}/")
            shutil.rmtree(full, ignore_errors=True)


def build(onefile: bool) -> None:
    env = dict(os.environ)
    env["HEB_BUILD_ONEFILE"] = "1" if onefile else "0"
    info(f"Building ({'onefile' if onefile else 'onedir — recommended'})...")
    rc = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "HebrewTranscriber.spec",
         "--noconfirm", "--clean"],
        cwd=HERE, env=env).returncode
    if rc != 0:
        fail("PyInstaller build failed — see output above")

    exe_name = APP_NAME + (".exe" if sys.platform == "win32" else "")
    if onefile:
        exe_path = os.path.join(HERE, "dist", exe_name)
    else:
        exe_path = os.path.join(HERE, "dist", APP_NAME, exe_name)
    if not os.path.exists(exe_path):
        fail(f"build finished but {exe_path} was not created")

    size_mb = _tree_size(os.path.dirname(exe_path) if not onefile else exe_path) / 1e6
    info("")
    info("BUILD SUCCESSFUL")
    info(f"  App: {exe_path}")
    info(f"  Size: ~{size_mb:.0f} MB "
         f"({'single file' if onefile else 'folder — zip it to share'})")
    info("  Reminder: the Whisper model (~1-3 GB) downloads on first run.")


def _tree_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--onefile", action="store_true",
                        help="single-file exe (onedir is recommended)")
    parser.add_argument("--check", action="store_true",
                        help="check the build environment and exit")
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the pre-build self-tests")
    args = parser.parse_args()

    ok = check_environment()
    if args.check:
        info("Environment check " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    if not ok:
        fail("environment check failed — install the missing packages first")

    if not args.skip_tests:
        run_tests()
    make_icon()
    clean()
    build(args.onefile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
