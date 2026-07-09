# Hebrew Lecture Transcriber — Windows Desktop App

A polished Windows desktop app (PySide6) around the existing `transcribe.py`
engine. The GUI never replaces the engine — it builds the same command-line
arguments a terminal user would type and runs the engine in a separate
process, so the CLI keeps working exactly as before and the UI never freezes.

- Dark, card-based interface with drag-and-drop
- Live progress: stage, percent, processed timestamp, elapsed, ETA
- Live log panel with copy/save
- Sample mode, long-safe mode, glossary, resume, quality report — all engine
  features exposed
- Safe Stop: progress is saved continuously; Resume continues where it left
  off
- Hebrew filenames, Hebrew Windows user folders, and paths with spaces all
  work; everything is UTF-8
- No admin rights needed; user files go next to your video, app data goes to
  `%LOCALAPPDATA%\HebrewTranscriber`

App version: **v1.0.0** (shown in the title bar, About dialog, and logs).

## 1. Install build dependencies

Requires Python 3.9+ on Windows 10/11.

```powershell
pip install -r requirements.txt
pip install -r requirements-gui.txt
```

ffmpeg is also required to *run* the app (see §6).

## 2. Run the GUI in development

```powershell
python gui_app.py
```

Quick headless sanity check (no window shown):

```powershell
python gui_app.py --smoke-test
```

## 3. Build the .exe

```powershell
python build_windows.py
```

This cleans old artifacts, runs the engine self-test and GUI smoke test,
optionally generates the exe icon (if Pillow is installed), and builds with
PyInstaller.

Single-file variant (works, but slower to start and more antivirus-prone —
**onedir above is recommended**):

```powershell
python build_windows.py --onefile
```

Environment check without building:

```powershell
python build_windows.py --check
```

## 4. Run the built app

```text
dist\HebrewTranscriber\HebrewTranscriber.exe
```

Share the app by zipping the whole `dist\HebrewTranscriber` folder — the
`.exe` needs the `_internal` folder beside it. (`--onefile` produces a single
`dist\HebrewTranscriber.exe` instead.)

### Bundling ffmpeg into the app (optional but nice for end users)

Before building, create a `bin` folder next to `build_windows.py` containing
`ffmpeg.exe` and `ffprobe.exe` (from <https://www.gyan.dev/ffmpeg/builds/>,
the "essentials" build is enough). The build bundles them and the app finds
them automatically. Without bundling, the app searches: bundled `bin/` →
system PATH → winget install locations → `C:\ffmpeg\bin`, and shows friendly
install instructions if nothing is found. If you redistribute ffmpeg, note
the gyan.dev builds are GPL — keep the license text alongside.

## 5. What to expect

- **First model download:** the first transcription downloads the ivrit.ai
  Whisper model (~1–3 GB) from Hugging Face and caches it. Internet is
  required once; afterwards it works offline. The app does NOT bundle the
  model (it would make the exe huge). Advanced settings let you point to a
  pre-downloaded model folder instead.
- **CPU speed:** a 2-hour lecture typically takes 1.5–4 hours on CPU. The
  app warns you and recommends a 5-minute sample first. An NVIDIA GPU is
  5–15× faster and is used automatically when detected.
- **Long-safe mode** (the default selection): disables
  condition-on-previous-text, which prevents the repetition loops Whisper
  can fall into on multi-hour recordings.
- **Sample mode:** transcribes only N minutes (optionally starting mid-
  lecture) so you can check quality and estimate speed before committing.
- **Resume:** progress streams to a `..._segments.jsonl` file. After a stop,
  crash, or power loss, the Resume button continues from the last completed
  timestamp — nothing is retranscribed.
- **Glossary:** a built-in Hebrew finance prompt biases the model toward
  market terms and tickers. "Edit glossary" opens your personal editable
  copy (stored in `%LOCALAPPDATA%\HebrewTranscriber`); you can also select
  any UTF-8 glossary file or type a custom prompt.
- **Output files:** `<name>_transcript.txt` next to your video, plus
  numbered `_partN.txt` chunks (for pasting into an AI chatbot) when the
  transcript is long, a `_quality_report.txt` flagging suspicious sections,
  and the `_segments.jsonl` used by Resume.

## 6. Troubleshooting

**"ffmpeg was not found" banner**
Install ffmpeg: `winget install Gyan.FFmpeg`, then click **Re-check** (or
restart the app). Alternatively put `ffmpeg.exe` + `ffprobe.exe` in a `bin`
folder next to `HebrewTranscriber.exe`.

**Model download fails**
Check your internet connection/proxy and disk space (the model is 1–3 GB),
then start again — downloads resume. You can also download the model on
another machine and select the folder under Advanced → Model → Local folder.

**The app seems frozen**
It shouldn't be — transcription runs in a separate process and the log
updates live. "Loading model" on the first run can sit for several minutes
while downloading (the progress bar shows an indeterminate animation). Check
the log panel; if nothing appears for a long time, Stop and check
`%LOCALAPPDATA%\HebrewTranscriber\logs`.

**The transcript repeats the same sentence**
Use **Long-safe** mode (default) and rerun. The quality report lists exactly
where loops happened.

**Antivirus flags the exe**
PyInstaller apps are sometimes false-flagged, especially `--onefile` builds.
Prefer the onedir build, add an exclusion for the app folder, or build from
source yourself (`python build_windows.py`).

**CUDA not detected**
Update the NVIDIA driver (`nvidia-smi` must work). CPU mode always works —
it's just slower.

**Hebrew path issues**
Hebrew usernames/filenames (e.g. `C:\Users\רחלי\...`) are fully supported —
everything is UTF-8. If a file fails to open, check for OneDrive placeholder
files (right-click → "Always keep on this device").

## 7. Recommended workflow

1. Open the app and drag your lecture file in.
2. Click **Run 5-min Sample** and read the sample transcript.
3. If terms look wrong, click **Edit glossary**, add your lecturer's terms
   and tickers, and re-run the sample.
4. Click **Start Transcription** (Long-safe mode is already selected).
5. If you must stop — click **Stop**; later click **Resume**.
6. When done, use **Open Transcript**, or paste the `_partN.txt` files into
   your AI chatbot one by one (each part tells the AI to wait for the rest).

## Developer notes

- `gui_app.py` — the whole GUI. It talks to the engine via one added,
  opt-in mechanism: setting `HEB_TRANSCRIBE_EVENTS=1` makes `transcribe.py`
  print `@@EVENT {json}` lines (stage, progress, done, error) that the GUI
  parses. Plain CLI behavior is unchanged.
- The packaged exe doubles as the engine: `HebrewTranscriber.exe --cli
  <args>` forwards straight to `transcribe.py`'s main — that's how the GUI
  spawns its worker process when frozen.
- `HebrewTranscriber.spec` — PyInstaller spec (onedir/onefile switched via
  the `HEB_BUILD_ONEFILE` env var, which `build_windows.py` sets).
- Build artifacts (`build/`, `dist/`, `bin/`, `*.ico`) are git-ignored;
  don't commit them.
