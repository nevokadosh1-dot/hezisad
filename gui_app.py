#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui_app.py — Windows desktop GUI for the Hebrew Lecture Transcriber.

A PySide6 wrapper around the existing transcribe.py engine. The engine is
the source of truth: this app builds the same CLI arguments a terminal user
would type and runs the engine in a child process (QProcess), reading
machine-readable '@@EVENT {json}' progress lines (enabled via the
HEB_TRANSCRIBE_EVENTS=1 environment variable). Because the engine runs in
its own process:

  * the UI never freezes,
  * Stop/Cancel is always safe (the engine's crash-safe JSONL/partial files
    survive, so --resume works), and
  * the CLI keeps working exactly as before.

Run in development:      python gui_app.py
Headless smoke test:     python gui_app.py --smoke-test
Engine passthrough:      HebrewTranscriber.exe --cli <transcribe.py args>
                         (used internally by the packaged app to run the
                          engine as a child of the same executable)
"""

import json
import os
import platform
import sys
import time
import webbrowser

APP_NAME = "Hebrew Lecture Transcriber"
APP_VERSION = "1.0.0"

VIDEO_AUDIO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv",
                    ".ts", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".flac",
                    ".ogg", ".opus", ".wma"}

FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/"
WINGET_FFMPEG_CMD = "winget install Gyan.FFmpeg"


# ---------------------------------------------------------------------------
# Engine passthrough — MUST run before Qt imports so the packaged .exe can
# act as its own transcription CLI without loading any GUI libraries.
# ---------------------------------------------------------------------------
def _maybe_run_cli() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        import transcribe
        sys.exit(transcribe.main(sys.argv[2:]))


_maybe_run_cli()

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QRadioButton, QScrollArea, QSpinBox, QSplitter, QToolButton, QVBoxLayout,
    QWidget,
)

import transcribe as engine


# ---------------------------------------------------------------------------
# Paths, logging
# ---------------------------------------------------------------------------
def app_dir() -> str:
    """Directory of the app source (dev) or the frozen executable."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(relative: str) -> str:
    """Path to a bundled resource (works in dev, onedir and onefile)."""
    base = getattr(sys, "_MEIPASS", None) or app_dir()
    return os.path.join(base, relative)


def app_data_dir() -> str:
    """Per-user writable data dir (never Program Files, no admin needed)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "HebrewTranscriber")
    else:
        path = os.path.expanduser("~/.hebrew_transcriber")
    os.makedirs(path, exist_ok=True)
    return path


def logs_dir() -> str:
    path = os.path.join(app_data_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path


class AppLog:
    """Simple UTF-8 app log file: settings, detection results, errors,
    created files. Transcript text itself is never logged."""

    def __init__(self):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(logs_dir(), f"session_{stamp}.log")
        self.write(f"{APP_NAME} v{APP_VERSION} — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.write(f"Python {platform.python_version()} on {platform.platform()}")

    def write(self, line: str) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
        except OSError:
            pass


def user_glossary_path() -> str:
    """Editable copy of the bundled finance glossary in the user data dir.
    (Bundled files live inside the app folder and may be read-only or
    replaced on update, so the editable copy lives in %LOCALAPPDATA%.)"""
    dest = os.path.join(app_data_dir(), "finance_glossary.txt")
    if not os.path.exists(dest):
        bundled = resource_path("finance_glossary.txt")
        try:
            with open(bundled, encoding="utf-8") as src, \
                    open(dest, "w", encoding="utf-8") as out:
                out.write(src.read())
        except OSError:
            with open(dest, "w", encoding="utf-8") as out:
                out.write(engine.DEFAULT_FINANCE_PROMPT + "\n")
    return dest


def open_in_file_manager(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


# ---------------------------------------------------------------------------
# Dark theme
# ---------------------------------------------------------------------------
DARK_QSS = """
* { font-family: "Segoe UI", "Noto Sans", sans-serif; font-size: 13px; }
QMainWindow, QWidget { background: #12151a; color: #e8eaed; }
QLabel#appTitle { font-size: 19px; font-weight: 700; color: #f3f6fa; }
QLabel#appSubtitle { color: #8b98a5; font-size: 12px; }
QLabel#hwLine { color: #8b98a5; font-size: 12px; }
QLabel#cardTitle { font-size: 13px; font-weight: 700; color: #9fb4c7;
                   letter-spacing: 1px; }
QLabel#hint { color: #7d8a97; font-size: 12px; }
QLabel#stageLabel { font-size: 15px; font-weight: 600; color: #e8eaed; }
QLabel#statValue { font-size: 14px; font-weight: 600; color: #dbe4ec; }
QLabel#statCaption { color: #7d8a97; font-size: 11px; }
QFrame#card { background: #1a1f26; border: 1px solid #2a323d;
              border-radius: 10px; }
QFrame#banner { background: #3a2b13; border: 1px solid #8a6d1f;
                border-radius: 10px; }
QLabel#bannerText { color: #f4d47c; }
QFrame#dropArea { background: #161b22; border: 2px dashed #3b4654;
                  border-radius: 10px; }
QFrame#dropArea[dragActive="true"] { border-color: #4f9cf9;
                                     background: #182234; }
QLabel#dropTitle { font-size: 14px; font-weight: 600; color: #c9d5e0; }
QPushButton { background: #262d37; color: #e8eaed; border: 1px solid #38424f;
              border-radius: 8px; padding: 7px 14px; }
QPushButton:hover { background: #2e3743; }
QPushButton:pressed { background: #232a33; }
QPushButton:disabled { color: #5b6672; background: #1d232b;
                       border-color: #2a323d; }
QPushButton#primary { background: #2563eb; border-color: #2563eb;
                      font-weight: 600; font-size: 14px; padding: 10px 20px; }
QPushButton#primary:hover { background: #3b76f0; }
QPushButton#primary:disabled { background: #1d3a6e; color: #7d94b9;
                               border-color: #1d3a6e; }
QPushButton#danger { background: #7f1d1d; border-color: #9b2c2c; }
QPushButton#danger:hover { background: #9b2c2c; }
QPushButton#danger:disabled { background: #3c1d1d; color: #8a6b6b;
                              border-color: #3c1d1d; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {
    background: #10141a; border: 1px solid #2e3947; border-radius: 6px;
    padding: 5px 8px; selection-background-color: #2563eb; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus { border-color: #4f9cf9; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #1a1f26; border: 1px solid #2e3947;
                              selection-background-color: #2563eb; }
QPlainTextEdit#logView { font-family: Consolas, "Courier New", monospace;
                         font-size: 12px; background: #0c0f13; }
QCheckBox, QRadioButton { spacing: 8px; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }
QProgressBar { background: #10141a; border: 1px solid #2e3947;
               border-radius: 7px; height: 16px; text-align: center;
               color: #dbe4ec; font-size: 11px; }
QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #2563eb, stop:1 #4f9cf9); border-radius: 6px; }
QToolButton#sectionToggle { background: transparent; border: none;
                            color: #9fb4c7; font-weight: 700;
                            letter-spacing: 1px; padding: 4px; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #12151a; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #333d4a; border-radius: 5px;
                              min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSplitter::handle { background: #12151a; width: 8px; }
"""

STAGE_TEXT = {
    "checking": "Checking dependencies…",
    "extracting_audio": "Extracting audio…",
    "loading_model": "Loading model… (first run downloads it — may take a while)",
    "transcribing": "Transcribing…",
    "writing_transcript": "Writing transcript…",
    "part_files": "Creating part files…",
    "done": "Done",
}


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------
def make_card(title: str) -> (QFrame, QVBoxLayout):
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    label = QLabel(title.upper())
    label.setObjectName("cardTitle")
    layout.addWidget(label)
    return card, layout


class DropArea(QFrame):
    """Drag-and-drop target for video/audio files."""

    def __init__(self, on_file, parent=None):
        super().__init__(parent)
        self.setObjectName("dropArea")
        self.setAcceptDrops(True)
        self.setMinimumHeight(72)
        self._on_file = on_file
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        title = QLabel("Drag a lecture video/audio file here")
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignCenter)
        hint = QLabel("mp4 · mkv · mov · avi · mp3 · wav · m4a …")
        hint.setObjectName("hint")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(hint)

    def _set_active(self, active: bool) -> None:
        self.setProperty("dragActive", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if urls and os.path.splitext(urls[0].toLocalFile())[1].lower() in VIDEO_AUDIO_EXTS:
            self._set_active(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_active(False)

    def dropEvent(self, event):
        self._set_active(False)
        urls = event.mimeData().urls()
        if urls:
            self._on_file(urls[0].toLocalFile())
        event.acceptProposedAction()


class CollapsibleSection(QWidget):
    """A toggle header that shows/hides advanced settings."""

    def __init__(self, title: str, content: QWidget, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._button = QToolButton()
        self._button.setObjectName("sectionToggle")
        self._button.setText("▸  " + title.upper())
        self._button.setCheckable(True)
        self._button.setCursor(Qt.PointingHandCursor)
        self._content = content
        self._content.setVisible(False)
        self._title = title.upper()
        self._button.toggled.connect(self._toggle)
        layout.addWidget(self._button)
        layout.addWidget(self._content)

    def _toggle(self, checked: bool) -> None:
        self._button.setText(("▾  " if checked else "▸  ") + self._title)
        self._content.setVisible(checked)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — v{APP_VERSION}")
        self.resize(1180, 760)
        icon_path = resource_path(os.path.join("assets", "icon.svg"))
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.log = AppLog()
        self.defaults = engine.parse_args([])  # engine's own CLI defaults
        self.process: QProcess = None
        self._stdout_buf = ""
        self._stderr_buf = ""
        self._stopping = False
        self._run_started = 0.0
        self._done_info = None
        self._error_message = None
        self._last_transcript = None
        self._ffmpeg_ok = True

        self._build_ui()
        QTimer.singleShot(50, self._startup_checks)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # --- Header ---------------------------------------------------
        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        app_title = QLabel(APP_NAME)
        app_title.setObjectName("appTitle")
        subtitle = QLabel("Hebrew finance-lecture transcription for AI summarization")
        subtitle.setObjectName("appSubtitle")
        title_col.addWidget(app_title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch(1)
        self.hw_label = QLabel("Detecting hardware…")
        self.hw_label.setObjectName("hwLine")
        self.hw_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.hw_label)
        about_btn = QPushButton("About")
        about_btn.clicked.connect(self._show_about)
        header.addWidget(about_btn)
        root.addLayout(header)

        # --- ffmpeg-missing banner (hidden unless needed) ---------------
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        banner_layout = QHBoxLayout(self.banner)
        self.banner_text = QLabel()
        self.banner_text.setObjectName("bannerText")
        self.banner_text.setWordWrap(True)
        banner_layout.addWidget(self.banner_text, 1)
        copy_cmd = QPushButton("Copy install command")
        copy_cmd.clicked.connect(lambda: (
            QGuiApplication.clipboard().setText(WINGET_FFMPEG_CMD),
            self._append_log(f"Copied to clipboard: {WINGET_FFMPEG_CMD}")))
        dl_btn = QPushButton("Download page")
        dl_btn.clicked.connect(lambda: webbrowser.open(FFMPEG_DOWNLOAD_URL))
        recheck = QPushButton("Re-check")
        recheck.clicked.connect(self._startup_checks)
        banner_layout.addWidget(copy_cmd)
        banner_layout.addWidget(dl_btn)
        banner_layout.addWidget(recheck)
        self.banner.setVisible(False)
        root.addWidget(self.banner)

        # --- Main area: settings (left) | progress + log (right) --------
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_settings_panel())
        splitter.addWidget(self._build_progress_panel())
        splitter.setStretchFactor(0, 11)
        splitter.setStretchFactor(1, 9)
        root.addWidget(splitter, 1)

        # --- Action bar --------------------------------------------------
        actions = QHBoxLayout()
        self.sample_btn = QPushButton("▶  Run 5-min Sample")
        self.sample_btn.setToolTip("Transcribe a short sample first to check "
                                   "quality and speed (recommended)")
        self.sample_btn.clicked.connect(lambda: self._start(sample=True))
        self.start_btn = QPushButton("▶  Start Transcription")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(lambda: self._start(sample=False))
        self.resume_btn = QPushButton("⟳  Resume")
        self.resume_btn.setToolTip("Continue an interrupted run from its "
                                   "segments file instead of starting over")
        self.resume_btn.clicked.connect(lambda: self._start(sample=False, resume=True))
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        self.open_transcript_btn = QPushButton("Open Transcript")
        self.open_transcript_btn.setEnabled(False)
        self.open_transcript_btn.clicked.connect(self._open_transcript)
        self.open_folder_btn = QPushButton("Open Output Folder")
        self.open_folder_btn.setEnabled(False)
        self.open_folder_btn.clicked.connect(self._open_output_folder)
        for b in (self.sample_btn, self.start_btn, self.resume_btn,
                  self.stop_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.open_transcript_btn)
        actions.addWidget(self.open_folder_btn)
        root.addLayout(actions)

        self.setCentralWidget(central)

    def _build_settings_panel(self) -> QWidget:
        panel = QWidget()
        col = QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(10)

        # File card ------------------------------------------------------
        card, lay = make_card("Lecture file")
        self.drop_area = DropArea(self._set_input_file)
        lay.addWidget(self.drop_area)
        row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("No file selected…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_file)
        row.addWidget(self.file_edit, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        col.addWidget(card)

        # Output card ------------------------------------------------------
        card, lay = make_card("Output")
        row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(
            "Default: <lecture name>_transcript.txt next to the video")
        out_browse = QPushButton("Choose…")
        out_browse.clicked.connect(self._browse_output)
        row.addWidget(self.output_edit, 1)
        row.addWidget(out_browse)
        lay.addLayout(row)
        col.addWidget(card)

        # Mode card ---------------------------------------------------------
        card, lay = make_card("Transcription mode")
        self.mode_group = QButtonGroup(self)
        modes = [
            ("accurate", "Accurate", "Best quality (default)"),
            ("long-safe", "Long-safe", "Recommended for 1–3 hour lectures — "
                                        "resists repetition loops"),
            ("fast", "Fast", "Quicker, slightly lower quality"),
        ]
        for value, label, tip in modes:
            radio = QRadioButton(label)
            radio.setToolTip(tip)
            radio.setProperty("modeValue", value)
            self.mode_group.addButton(radio)
            hint = QLabel(tip)
            hint.setObjectName("hint")
            hint.setIndent(24)
            lay.addWidget(radio)
            lay.addWidget(hint)
            if value == "long-safe":
                radio.setChecked(True)  # best default for the target user
        col.addWidget(card)

        # Sample card ---------------------------------------------------------
        card, lay = make_card("Sample mode")
        self.sample_check = QCheckBox("Transcribe a sample only")
        lay.addWidget(self.sample_check)
        grid = QGridLayout()
        grid.addWidget(QLabel("Sample minutes:"), 0, 0)
        self.sample_minutes = QDoubleSpinBox()
        self.sample_minutes.setRange(0.5, 180.0)
        self.sample_minutes.setValue(5.0)
        self.sample_minutes.setDecimals(1)
        grid.addWidget(self.sample_minutes, 0, 1)
        grid.addWidget(QLabel("Start at minute:"), 0, 2)
        self.sample_start = QDoubleSpinBox()
        self.sample_start.setRange(0.0, 10000.0)
        self.sample_start.setValue(0.0)
        self.sample_start.setDecimals(1)
        grid.addWidget(self.sample_start, 0, 3)
        grid.setColumnStretch(4, 1)
        lay.addLayout(grid)
        hint = QLabel("Recommended before a full lecture — a 5-minute sample "
                      "shows quality and speed before you commit hours.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        col.addWidget(card)

        # Glossary card ---------------------------------------------------------
        card, lay = make_card("Finance glossary / prompt")
        self.builtin_prompt_check = QCheckBox(
            "Use built-in Hebrew finance prompt (terms + tickers)")
        self.builtin_prompt_check.setChecked(True)
        lay.addWidget(self.builtin_prompt_check)
        row = QHBoxLayout()
        self.glossary_edit = QLineEdit()
        self.glossary_edit.setPlaceholderText("Optional: custom glossary file…")
        gl_browse = QPushButton("Browse…")
        gl_browse.clicked.connect(self._browse_glossary)
        gl_edit = QPushButton("Edit glossary")
        gl_edit.setToolTip("Open your editable finance_glossary.txt")
        gl_edit.clicked.connect(self._edit_glossary)
        row.addWidget(self.glossary_edit, 1)
        row.addWidget(gl_browse)
        row.addWidget(gl_edit)
        lay.addLayout(row)
        self.prompt_edit = QLineEdit()
        self.prompt_edit.setPlaceholderText(
            "Optional: custom prompt text (overrides glossary and built-in)")
        lay.addWidget(self.prompt_edit)
        col.addWidget(card)

        # Advanced (collapsible) --------------------------------------------
        adv = QWidget()
        grid = QGridLayout(adv)
        grid.setContentsMargins(4, 2, 4, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        def add_row(r, label, widget, label2=None, widget2=None):
            grid.addWidget(QLabel(label), r, 0)
            grid.addWidget(widget, r, 1)
            if label2:
                grid.addWidget(QLabel(label2), r, 2)
                grid.addWidget(widget2, r, 3)

        self.device_combo = QComboBox()
        self.device_combo.addItems(["auto", "cpu", "cuda"])
        self.compute_combo = QComboBox()
        self.compute_combo.addItems(["auto", "int8", "float16",
                                     "int8_float16", "float32"])
        add_row(0, "Device:", self.device_combo, "Compute type:", self.compute_combo)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 128)
        self.threads_spin.setSpecialValueText("all cores")
        self.beam_spin = QSpinBox()
        self.beam_spin.setRange(0, 10)
        self.beam_spin.setSpecialValueText("mode default")
        add_row(1, "CPU threads:", self.threads_spin, "Beam size:", self.beam_spin)

        self.vad_check = QCheckBox("VAD filter (skip silence)")
        self.vad_check.setChecked(True)
        self.normalize_check = QCheckBox("Normalize audio (quiet recordings)")
        grid.addWidget(self.vad_check, 2, 0, 1, 2)
        grid.addWidget(self.normalize_check, 2, 2, 1, 2)

        self.vad_silence_spin = QSpinBox()
        self.vad_silence_spin.setRange(100, 10000)
        self.vad_silence_spin.setValue(self.defaults.vad_min_silence_duration_ms)
        self.vad_silence_spin.setSuffix(" ms")
        self.vad_pad_spin = QSpinBox()
        self.vad_pad_spin.setRange(0, 2000)
        self.vad_pad_spin.setValue(self.defaults.vad_speech_pad_ms)
        self.vad_pad_spin.setSuffix(" ms")
        add_row(3, "VAD min silence:", self.vad_silence_spin,
                "VAD speech pad:", self.vad_pad_spin)

        self.audio_filter_edit = QLineEdit()
        self.audio_filter_edit.setPlaceholderText(
            "Advanced ffmpeg audio filter, e.g. highpass=f=80,loudnorm")
        grid.addWidget(QLabel("Audio filter:"), 4, 0)
        grid.addWidget(self.audio_filter_edit, 4, 1, 1, 3)

        self.model_edit = QLineEdit()
        self.model_edit.setText(self.defaults.model)
        self.model_edit.setToolTip("Model name from Hugging Face or a local "
                                   "pre-downloaded model folder")
        model_browse = QPushButton("Local folder…")
        model_browse.clicked.connect(self._browse_model_dir)
        grid.addWidget(QLabel("Model:"), 5, 0)
        grid.addWidget(self.model_edit, 5, 1, 1, 2)
        grid.addWidget(model_browse, 5, 3)

        self.keep_audio_check = QCheckBox("Keep extracted WAV")
        self.segments_check = QCheckBox("Save segments JSONL (needed for Resume)")
        self.segments_check.setChecked(True)
        self.report_check = QCheckBox("Quality report")
        self.report_check.setChecked(True)
        grid.addWidget(self.keep_audio_check, 6, 0, 1, 2)
        grid.addWidget(self.segments_check, 6, 2, 1, 2)
        grid.addWidget(self.report_check, 7, 0, 1, 2)

        col.addWidget(CollapsibleSection("Advanced settings", adv))
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        return scroll

    def _build_progress_panel(self) -> QWidget:
        panel = QWidget()
        col = QVBoxLayout(panel)
        col.setContentsMargins(6, 0, 0, 0)
        col.setSpacing(10)

        card, lay = make_card("Progress")
        self.stage_label = QLabel("Ready")
        self.stage_label.setObjectName("stageLabel")
        lay.addWidget(self.stage_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        lay.addWidget(self.progress_bar)
        stats = QGridLayout()
        self.stat_labels = {}
        for i, (key, caption) in enumerate([
                ("processed", "Processed"), ("pct", "Percent"),
                ("elapsed", "Elapsed"), ("eta", "Remaining")]):
            value = QLabel("—")
            value.setObjectName("statValue")
            cap = QLabel(caption)
            cap.setObjectName("statCaption")
            stats.addWidget(value, 0, i)
            stats.addWidget(cap, 1, i)
            self.stat_labels[key] = value
        lay.addLayout(stats)
        col.addWidget(card)

        card, lay = make_card("Log")
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        lay.addWidget(self.log_view, 1)
        row = QHBoxLayout()
        copy_btn = QPushButton("Copy logs")
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(
            self.log_view.toPlainText()))
        save_btn = QPushButton("Save logs…")
        save_btn.clicked.connect(self._save_logs)
        row.addStretch(1)
        row.addWidget(copy_btn)
        row.addWidget(save_btn)
        lay.addLayout(row)
        col.addWidget(card, 1)
        return panel

    # ------------------------------------------------------ startup checks
    def _startup_checks(self) -> None:
        hw = {
            "os": f"{platform.system()} {platform.release()}",
            "cpu_cores": os.cpu_count() or 0,
            "ram_gb": engine.get_ram_gb(),
            "cuda": engine.detect_cuda(),
        }
        device = "cuda" if hw["cuda"] else "cpu"
        compute = "float16" if hw["cuda"] else "int8"
        ram = f"{hw['ram_gb']:.0f} GB" if hw["ram_gb"] else "?"
        self.hw_label.setText(
            f"{hw['os']}  ·  {hw['cpu_cores']} cores  ·  {ram} RAM  ·  "
            f"{'CUDA GPU ✓' if hw['cuda'] else 'no CUDA GPU'}  ·  "
            f"{device}/{compute}")
        self.log.write(f"Hardware: {hw} -> default {device}/{compute}")

        ffmpeg = engine.find_tool("ffmpeg")
        ffprobe = engine.find_tool("ffprobe")
        self._ffmpeg_ok = bool(ffmpeg and ffprobe)
        self.log.write(f"ffmpeg: {ffmpeg or 'NOT FOUND'} | "
                       f"ffprobe: {ffprobe or 'NOT FOUND'}")
        if self._ffmpeg_ok:
            self.banner.setVisible(False)
            self._append_log(f"ffmpeg found: {ffmpeg}")
        else:
            self.banner_text.setText(
                "ffmpeg was not found. It is required to read video files.  "
                f"Install it with:   {WINGET_FFMPEG_CMD}   (then restart the "
                "app), or download it and place ffmpeg.exe + ffprobe.exe in a "
                "'bin' folder next to the app.")
            self.banner.setVisible(True)
            self._append_log("ffmpeg NOT found — transcription is disabled "
                             "until it is installed.")
        self._set_idle_state()

    # ------------------------------------------------------------ helpers
    def _append_log(self, line: str) -> None:
        line = line.rstrip()
        if line:
            self.log_view.appendPlainText(line)

    def _set_input_file(self, path: str) -> None:
        self.file_edit.setText(path)
        self._append_log(f"Selected file: {path}")

    def _browse_file(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_AUDIO_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose lecture video/audio", "",
            f"Video/Audio files ({exts});;All files (*)")
        if path:
            self._set_input_file(path)

    def _browse_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose output transcript", "", "Text files (*.txt)")
        if path:
            self.output_edit.setText(path)

    def _browse_glossary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose glossary file", "", "Text files (*.txt);;All files (*)")
        if path:
            self.glossary_edit.setText(path)

    def _browse_model_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose a pre-downloaded model folder")
        if path:
            self.model_edit.setText(path)

    def _edit_glossary(self) -> None:
        path = user_glossary_path()
        if not self.glossary_edit.text().strip():
            self.glossary_edit.setText(path)
        open_in_file_manager(path)

    def _selected_mode(self) -> str:
        btn = self.mode_group.checkedButton()
        return btn.property("modeValue") if btn else "long-safe"

    def _expected_output_path(self, sample: bool) -> str:
        """Mirror the engine's default output naming for overwrite checks."""
        custom = self.output_edit.text().strip()
        if custom:
            return os.path.abspath(custom)
        stem, _ = os.path.splitext(os.path.abspath(self.file_edit.text().strip()))
        if sample:
            return f"{stem}_sample_{self.sample_minutes.value():g}min_transcript.txt"
        return f"{stem}_transcript.txt"

    # -------------------------------------------------------- CLI building
    def build_cli_args(self, sample: bool, resume: bool) -> list:
        """Translate UI state into transcribe.py arguments. Only non-default
        values are passed, so engine defaults always stay in charge."""
        d = self.defaults
        args = [self.file_edit.text().strip()]
        out = self.output_edit.text().strip()
        if out:
            args += ["--output", out]
        args += ["--mode", self._selected_mode()]
        if sample:
            args += ["--sample-minutes", f"{self.sample_minutes.value():g}"]
            if self.sample_start.value() > 0:
                args += ["--sample-start-minute", f"{self.sample_start.value():g}"]
        if resume:
            args += ["--resume"]

        prompt = self.prompt_edit.text().strip()
        glossary = self.glossary_edit.text().strip()
        if prompt:
            args += ["--initial-prompt", prompt]
        elif glossary:
            args += ["--glossary", glossary]
        elif not self.builtin_prompt_check.isChecked():
            args += ["--no-default-prompt"]

        if self.device_combo.currentText() != "auto":
            args += ["--device", self.device_combo.currentText()]
        if self.compute_combo.currentText() != "auto":
            args += ["--compute-type", self.compute_combo.currentText()]
        if self.threads_spin.value() > 0:
            args += ["--cpu-threads", str(self.threads_spin.value())]
        if self.beam_spin.value() > 0:
            args += ["--beam-size", str(self.beam_spin.value())]
        if not self.vad_check.isChecked():
            args += ["--no-vad"]
        else:
            if self.vad_silence_spin.value() != d.vad_min_silence_duration_ms:
                args += ["--vad-min-silence-duration-ms",
                         str(self.vad_silence_spin.value())]
            if self.vad_pad_spin.value() != d.vad_speech_pad_ms:
                args += ["--vad-speech-pad-ms", str(self.vad_pad_spin.value())]
        audio_filter = self.audio_filter_edit.text().strip()
        if audio_filter:
            args += ["--audio-filter", audio_filter]
        elif self.normalize_check.isChecked():
            args += ["--normalize-audio"]
        model = self.model_edit.text().strip()
        if model and model != d.model:
            args += ["--model", model]
        if self.keep_audio_check.isChecked():
            args += ["--keep-audio"]
        if not self.segments_check.isChecked():
            args += ["--no-save-segments"]
        if not self.report_check.isChecked():
            args += ["--no-quality-report"]
        return args

    def _engine_command(self, cli_args: list) -> (str, list):
        """Program + argv to run the engine as a child process. Frozen app:
        re-invoke this same executable with --cli; dev: run transcribe.py."""
        if getattr(sys, "frozen", False):
            return sys.executable, ["--cli"] + cli_args
        script = os.path.join(app_dir(), "transcribe.py")
        return sys.executable, [script] + cli_args

    # ------------------------------------------------------------- running
    def _start(self, sample: bool, resume: bool = False) -> None:
        if self.process is not None:
            return
        if not self._ffmpeg_ok:
            QMessageBox.warning(self, "ffmpeg missing",
                                "ffmpeg is required. Install it (see the "
                                "yellow banner) and click Re-check.")
            return
        video = self.file_edit.text().strip()
        if not video:
            QMessageBox.warning(self, "No file", "Choose a lecture file first.")
            return
        if not os.path.isfile(video):
            QMessageBox.warning(self, "File not found",
                                f"This file does not exist:\n{video}")
            return
        if not sample and self.sample_check.isChecked() and not resume:
            sample = True  # the checkbox turns the main button into sample mode

        # CPU heads-up before committing to a full multi-hour run.
        if not sample and not resume and not engine.detect_cuda() \
                and self.device_combo.currentText() != "cuda":
            answer = QMessageBox.question(
                self, "CPU transcription",
                "A 1–3 hour lecture on CPU may take a long time (often "
                "1.5–4 hours for a 2-hour lecture).\n\nRecommended: run a "
                "5-minute sample first.\n\nStart the full transcription "
                "anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return

        # Overwrite confirmation (skip on resume — it continues that output).
        expected = self._expected_output_path(sample)
        if not resume and os.path.exists(expected):
            answer = QMessageBox.question(
                self, "Overwrite transcript?",
                f"This transcript already exists:\n{expected}\n\nOverwrite it?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return

        cli_args = self.build_cli_args(sample, resume)
        program, argv = self._engine_command(cli_args)
        self.log.write(f"Starting run: {argv}")
        self._append_log("─" * 60)
        self._append_log(("Sample run" if sample else
                          "Resume run" if resume else "Full transcription")
                         + " starting…")

        self.process = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("HEB_TRANSCRIBE_EVENTS", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        self.process.setProcessEnvironment(env)
        self.process.setProgram(program)
        self.process.setArguments(argv)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_process_error)

        self._stopping = False
        self._done_info = None
        self._error_message = None
        self._run_started = time.monotonic()
        self._set_running_state()
        self._set_stage("checking")
        self.process.start()

    def _stop(self) -> None:
        if self.process is None:
            return
        answer = QMessageBox.question(
            self, "Stop transcription?",
            "Stop now?\n\nProgress is saved continuously — you can continue "
            "later with the Resume button instead of starting over.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes or self.process is None:
            return
        self._stopping = True
        self._append_log("Stopping…")
        self.process.kill()  # crash-safe files survive; --resume continues

    # ------------------------------------------------------- process output
    def _read_stdout(self) -> None:
        data = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        self._stdout_buf += data
        while "\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.startswith("@@EVENT "):
                try:
                    self._handle_event(json.loads(line[len("@@EVENT "):]))
                except json.JSONDecodeError:
                    pass
            elif line.strip():
                self._append_log(line)

    def _read_stderr(self) -> None:
        data = bytes(self.process.readAllStandardError()).decode("utf-8", "replace")
        self._stderr_buf += data
        while True:  # progress lines use \r; treat \r and \n as line breaks
            cut_n, cut_r = self._stderr_buf.find("\n"), self._stderr_buf.find("\r")
            cut = min(c for c in (cut_n, cut_r) if c >= 0) if max(cut_n, cut_r) >= 0 else -1
            if cut < 0:
                break
            line = self._stderr_buf[:cut].strip()
            self._stderr_buf = self._stderr_buf[cut + 1:]
            # Skip raw progress lines — the @@EVENT stream renders them nicely.
            if line and not line.startswith("elapsed "):
                self._append_log(line)
                if "ERROR" in line or "WARNING" in line:
                    self.log.write(line)

    def _handle_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "stage":
            self._set_stage(event.get("stage", ""))
        elif kind == "progress":
            pct = event.get("pct") or 0
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(int(pct))
            self.stat_labels["pct"].setText(f"{pct:.1f}%")
            self.stat_labels["processed"].setText(
                f"{engine.format_hms(event.get('current') or 0)} / "
                f"{engine.format_hms(event.get('window_end') or 0)}")
            self.stat_labels["elapsed"].setText(
                engine.format_hms(event.get("elapsed") or 0))
            eta = event.get("eta")
            self.stat_labels["eta"].setText(
                engine.format_hms(eta) if eta is not None else "—")
        elif kind == "model_loaded":
            self._append_log(f"Model ready: {event.get('model')}")
            self.log.write(f"Model loaded: {event.get('model')}")
        elif kind == "hardware":
            self.log.write(f"Engine hardware: {event}")
        elif kind == "done":
            self._done_info = event
        elif kind == "error":
            self._error_message = event.get("message")

    def _set_stage(self, stage: str) -> None:
        text = STAGE_TEXT.get(stage, stage or "Working…")
        self.stage_label.setText(text)
        if stage in ("checking", "extracting_audio", "loading_model"):
            self.progress_bar.setRange(0, 0)  # indeterminate spinner
        elif stage == "transcribing":
            self.progress_bar.setRange(0, 100)

    # ------------------------------------------------------------- finishing
    def _on_process_error(self, error) -> None:
        # FailedToStart is the only case finished() won't follow.
        if self.process and error == QProcess.FailedToStart:
            self._append_log("ERROR: the transcription process failed to start.")
            self._cleanup_process()
            self._set_idle_state()

    def _on_finished(self, exit_code, _status) -> None:
        elapsed = engine.format_hms(time.monotonic() - self._run_started)
        done, err = self._done_info, self._error_message
        stopped = self._stopping
        self._cleanup_process()
        self._set_idle_state()

        if stopped:
            self._set_stage_text("Stopped — progress saved")
            self.log.write("Run stopped by user.")
            QMessageBox.information(
                self, "Stopped",
                "Transcription stopped.\n\nAll progress was saved (segments "
                ".jsonl + partial text). Click Resume to continue from where "
                "it stopped — nothing needs to be retranscribed.")
            return

        if exit_code == 0 and done:
            self._set_stage_text("Done")
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self._last_transcript = done.get("transcript")
            files = [("Transcript", done.get("transcript"))]
            files += [(f"Part {i}", p) for i, p in
                      enumerate(done.get("parts") or [], start=1)]
            if done.get("report"):
                files.append(("Quality report", done.get("report")))
            if done.get("segments"):
                files.append(("Segments JSONL", done.get("segments")))
            for label, path in files:
                self._append_log(f"Created: {path}")
                self.log.write(f"Created {label}: {path}")
            self.open_transcript_btn.setEnabled(True)
            self.open_folder_btn.setEnabled(True)

            box = QMessageBox(self)
            box.setWindowTitle("Transcription complete")
            box.setIcon(QMessageBox.Information)
            box.setText(f"Done in {elapsed}  ·  {done.get('chars', 0):,} "
                        f"characters, {done.get('paragraphs', 0)} paragraphs.")
            box.setInformativeText("\n".join(f"{label}:  {path}"
                                             for label, path in files if path))
            open_btn = box.addButton("Open transcript", QMessageBox.AcceptRole)
            folder_btn = box.addButton("Open folder", QMessageBox.ActionRole)
            copy_btn = box.addButton("Copy path", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Close)
            box.exec()
            if box.clickedButton() is open_btn:
                self._open_transcript()
            elif box.clickedButton() is folder_btn:
                self._open_output_folder()
            elif box.clickedButton() is copy_btn:
                QGuiApplication.clipboard().setText(self._last_transcript or "")
            return

        # Failure path: keep all partial/JSONL files, explain clearly.
        self._set_stage_text("Failed")
        tail = "\n".join(self.log_view.toPlainText().splitlines()[-12:])
        message = err or f"The transcription process exited with code {exit_code}."
        self.log.write(f"Run failed: {message}")
        hint = ""
        if "403" in message or "download" in message.lower() or "connect" in message.lower():
            hint = ("\n\nThis looks like a model-download problem — check "
                    "your internet connection. The model (~1–3 GB) is only "
                    "downloaded once.")
        QMessageBox.critical(
            self, "Transcription failed",
            f"{message}{hint}\n\nPartial progress files were kept, so Resume "
            f"can continue this run.\n\nLast log lines:\n{tail}")

    def _set_stage_text(self, text: str) -> None:
        self.stage_label.setText(text)

    def _cleanup_process(self) -> None:
        if self.process:
            self.process.deleteLater()
        self.process = None
        self._stdout_buf = self._stderr_buf = ""

    # -------------------------------------------------------- state helpers
    def _set_running_state(self) -> None:
        for b in (self.sample_btn, self.start_btn, self.resume_btn):
            b.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _set_idle_state(self) -> None:
        running = self.process is not None
        for b in (self.sample_btn, self.start_btn, self.resume_btn):
            b.setEnabled(not running and self._ffmpeg_ok)
        self.stop_btn.setEnabled(running)

    # ------------------------------------------------------------- actions
    def _open_transcript(self) -> None:
        if self._last_transcript and os.path.exists(self._last_transcript):
            open_in_file_manager(self._last_transcript)

    def _open_output_folder(self) -> None:
        target = self._last_transcript
        if not target and self.file_edit.text().strip():
            target = self._expected_output_path(False)
        if target:
            open_in_file_manager(os.path.dirname(target))

    def _save_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save logs", "transcriber_logs.txt", "Text files (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.log_view.toPlainText())

    def _show_about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> v{APP_VERSION}<br><br>"
            "Transcribes Hebrew finance lectures into clean, AI-ready text "
            "using faster-whisper and the ivrit.ai Hebrew models.<br><br>"
            "The first transcription downloads the model (~1–3 GB, cached "
            "afterwards) — internet is required once.<br><br>"
            f"App logs: {logs_dir()}")

    def closeEvent(self, event) -> None:
        if self.process is not None:
            answer = QMessageBox.question(
                self, "Transcription running",
                "A transcription is still running. Stop it and quit?\n"
                "(Progress is saved — Resume can continue later.)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.process.kill()
            self.process.waitForFinished(3000)
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def create_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)
    font = QFont("Segoe UI" if os.name == "nt" else "Noto Sans", 10)
    app.setFont(font)
    return app


def smoke_test() -> int:
    """Headless sanity check used by build_windows.py and CI:
    instantiate the full window offscreen and exercise CLI-arg building."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = create_app()
    win = MainWindow()
    win.file_edit.setText("/tmp/דוגמה lecture.mp4")
    args = win.build_cli_args(sample=True, resume=False)
    assert args[0].endswith("lecture.mp4")
    assert "--mode" in args and "long-safe" in args
    assert "--sample-minutes" in args
    win.normalize_check.setChecked(True)
    win.vad_check.setChecked(False)
    args = win.build_cli_args(sample=False, resume=True)
    assert "--resume" in args and "--normalize-audio" in args and "--no-vad" in args
    assert "--sample-minutes" not in args
    program, argv = win._engine_command(["x.mp4"])
    assert argv[-1] == "x.mp4"
    win.close()
    del app
    print("GUI smoke test OK")
    return 0


def main() -> int:
    if "--smoke-test" in sys.argv:
        return smoke_test()
    app = create_app()
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
