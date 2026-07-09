# Hebrew Lecture Transcriber

> **Windows desktop app available:** prefer a GUI over the command line?
> See [README_WINDOWS_APP.md](README_WINDOWS_APP.md) for the PySide6 desktop
> app and how to build `HebrewTranscriber.exe`. The CLI below keeps working
> unchanged.

A local command-line tool that transcribes long Hebrew lecture videos
(1–3 hours, e.g. stock-market lectures) into a clean, AI-readable `.txt`
transcript.

The output is **not** a subtitle file. It is a paragraph-based text file
designed to be pasted into an AI chatbot (ChatGPT, Claude, Gemini, ...) for
summarization:

- No timestamps, no SRT/VTT formatting.
- Whisper segments are merged into coherent paragraphs (new paragraph on a
  long pause or when a paragraph gets too long).
- Only *light* cleanup is applied (whitespace, stutter like "אה אה",
  punctuation spacing). Wording, numbers, percentages, tickers and company
  names are never altered — financial accuracy over prettiness.
- Very long transcripts are also split into numbered part files
  (`..._part1.txt`, `..._part2.txt`, ...) sized for pasting into a chatbot.
  Each part starts with an AI-friendly Hebrew header (`חלק X מתוך Y`, plus an
  instruction not to summarize until all parts arrive; the last part says
  it is safe to summarize).
- A finance glossary is passed to the model as `initial_prompt` to bias it
  toward Hebrew stock-market vocabulary and common tickers (decoder bias
  only — the transcript is never post-corrected).
- Crash-safe and resumable: raw segments are streamed to a
  `..._segments.jsonl` file during transcription; `--resume` continues an
  interrupted run from the last completed timestamp instead of starting over.
- A `..._quality_report.txt` flags suspicious sections (repetition loops,
  low-confidence spans) for manual review — the transcript itself is never
  modified based on them.

It uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with the
Hebrew-tuned [ivrit-ai](https://huggingface.co/ivrit-ai) models by default:

1. `ivrit-ai/whisper-large-v3-turbo-ct2` (default)
2. `ivrit-ai/faster-whisper-v2-d4` (automatic fallback)
3. `large-v3` (automatic fallback)

## Installation

Requires Python 3.9+.

```bash
# 1. (Recommended) create a virtual environment
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# 2. Install Python dependencies
pip install -r requirements.txt
```

### Install ffmpeg (required)

The tool needs `ffmpeg` and `ffprobe` on your PATH.

**Windows:**

```powershell
winget install Gyan.FFmpeg
```

(or download from <https://www.gyan.dev/ffmpeg/builds/>, extract, and add the
`bin` folder to your PATH — then reopen the terminal)

**macOS:**

```bash
brew install ffmpeg
```

**Ubuntu / Debian:**

```bash
sudo apt update && sudo apt install -y ffmpeg
```

## Quick start

```bash
# Normal transcription (Hebrew filenames and spaces are fine — just quote)
python transcribe.py "הרצאה על שוק ההון 2026.mp4"

# Test quality/speed on the first 5 minutes before committing to hours
python transcribe.py lecture.mp4 --sample-minutes 5

# Sample 5 minutes starting at minute 30 (test a later section)
python transcribe.py lecture.mp4 --sample-minutes 5 --sample-start-minute 30

# Long-safe mode: resists repetition loops on 1-3 hour recordings
python transcribe.py lecture.mp4 --mode long-safe

# Use your own finance glossary as the initial prompt
python transcribe.py lecture.mp4 --glossary finance_glossary.txt

# Force CPU / force CUDA (NVIDIA GPU)
python transcribe.py lecture.mp4 --device cpu --compute-type int8
python transcribe.py lecture.mp4 --device cuda --compute-type float16

# Resume an interrupted run (continues from the last completed timestamp)
python transcribe.py lecture.mp4 --resume

# Normalize quiet / uneven audio during extraction
python transcribe.py lecture.mp4 --normalize-audio
```

Output files land next to the video (or next to `--output`):

| File | What it is |
|---|---|
| `lecture_transcript.txt` | the final paragraph transcript |
| `lecture_transcript_part1.txt`, ... | AI-chunk parts (only if the transcript is long) |
| `lecture_segments.jsonl` | raw segments with timestamps + confidence (for `--resume` and auditing) |
| `lecture_quality_report.txt` | run stats + suspicious sections to review |
| `lecture_transcript.txt.partial.txt` | live progress view; deleted on success |

## Recommended workflow

1. Run `--sample-minutes 5` first.
2. Check the sample output quality.
3. If good, run the full transcription.
4. If the output repeats itself, rerun with `--mode long-safe`.
5. If financial terms come out wrong, use `--glossary finance_glossary.txt`
   (edit it to match the terms/tickers your lecturer actually uses).
6. If the recording is quiet, try `--normalize-audio`.
7. If the run is interrupted, rerun the same command with `--resume`.

## Glossary / initial prompt

By default the tool feeds a built-in Hebrew finance prompt (market terms +
common tickers like NVDA/AAPL/S&P 500) to Whisper as `initial_prompt`, which
biases the decoder toward that vocabulary. This repo ships an example
[`finance_glossary.txt`](finance_glossary.txt) you can copy and edit:

```text
זוהי הרצאה בעברית על שוק ההון והשקעות.
מונחים: מניות, מדדים, אג"ח, תשואה, דיבידנד, מכפיל רווח, אינפלציה, ריבית, הפד.
חברות: NVDA, Nvidia, אנבידיה, AAPL, Apple, אפל, TSLA, Tesla, טסלה.
```

Priority: `--initial-prompt "text"` beats `--glossary file.txt`, which beats
the built-in prompt. `--no-default-prompt` disables the built-in fallback.
Keep glossaries short (Whisper only uses roughly the last ~224 tokens) and
put the most important terms near the end. The glossary is **never** used to
post-correct the transcript — words, numbers and tickers stay exactly as
transcribed.

## Modes

| Mode | beam size | condition_on_previous_text | Use when |
|---|---|---|---|
| `accurate` (default) | 5 | on | best quality, normal recordings |
| `long-safe` | 5 | off | 1–3 h lectures, or the transcript loops/repeats |
| `fast` | 3 | off | quick drafts, slow machines |

Explicit flags always beat the preset: `--beam-size N`,
`--condition-on-previous-text`, `--no-condition-on-previous-text`.
The resolved settings are printed before transcription starts.

## Resume

Raw segments are continuously saved to `lecture_segments.jsonl`. If a run is
interrupted (Ctrl-C, crash, power loss), rerun the same command with
`--resume`: the tool reads the last completed timestamp, re-extracts audio
from `--overlap-seconds` (default 5) before it, trims overlapping duplicate
segments, and rebuilds the final transcript from **all** segments — no
duplicated paragraphs, and no retranscribing hours of finished audio. If the
resume data is missing or corrupt it warns and falls back to a full run;
pass `--resume-strict` to make that an error instead.

## Audio normalization

`--normalize-audio` applies ffmpeg's `loudnorm` filter during extraction. It
can help quiet or unevenly-recorded lectures, but on decent recordings it can
occasionally *hurt* accuracy — that's why it is off by default. Advanced
users can pass an exact filter chain with `--audio-filter "..."` (which takes
priority over `--normalize-audio`).

## All options

Run `python transcribe.py --help` for the full list. Highlights beyond the
flags shown above:

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `ivrit-ai/whisper-large-v3-turbo-ct2` | model name or local path |
| `--output` | `<video>_transcript.txt` | output text file |
| `--no-vad` | off | disable voice-activity-detection filtering |
| `--vad-min-silence-duration-ms` | 2000 | silence before VAD splits speech |
| `--vad-speech-pad-ms` | 400 | padding around detected speech |
| `--vad-threshold` | model default | VAD speech probability threshold |
| `--no-save-segments` | (segments on) | skip the raw segments JSONL |
| `--no-quality-report` | (report on) | skip the quality report |
| `--max-paragraph-chars` | 750 | max paragraph length |
| `--pause-threshold` | 2.0 | pause (s) that starts a new paragraph |
| `--split-threshold` | 50000 | transcripts longer than this get part files |
| `--part-size` | 40000 | target characters per part file |
| `--keep-audio` | off | keep the extracted temporary WAV |

## Self-test

```bash
python transcribe.py --self-test              # full test, downloads the small "tiny" model
python transcribe.py --self-test --skip-model # everything except the model download
```

The self-test exercises ffmpeg detection, audio + sample-window extraction,
glossary/prompt priority, mode presets, paragraph merging, cleanup
invariants (numbers/tickers untouched), part splitting with AI chunk
headers, resume planning from a partly-corrupt JSONL, quality-report
generation, and VAD flag resolution.

## Speed expectations (honest)

- **CPU:** transcription with a large model typically runs around
  0.5×–1.5× real time on a modern CPU. A 2-hour lecture can take **1.5–4
  hours** (sometimes more on older machines). This is normal.
- **NVIDIA GPU (CUDA):** usually **5–15× faster** — a 2-hour lecture often
  finishes in 10–25 minutes.
- The first run also downloads the model (~1–3 GB), which takes a few
  minutes depending on your connection. Models are cached afterwards.
- Google Colab's free GPU is a good alternative if your machine has no
  NVIDIA GPU (see below).

Progress (elapsed time, position in the audio, percent, ETA) is printed while
transcribing. Segments stream to the `.jsonl` and paragraphs to the
`.partial.txt` continuously, so an interruption never loses hours of work —
just rerun with `--resume`.

## Troubleshooting

**"ffmpeg / ffprobe not found"**
ffmpeg is not installed or not on your PATH. Install it (see above) and
**reopen the terminal**. Verify with `ffmpeg -version`.

**CUDA not detected even though I have an NVIDIA GPU**
Make sure recent NVIDIA drivers are installed (`nvidia-smi` should work).
faster-whisper on GPU also needs CUDA 12 + cuDNN libraries; the simplest fix
is `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` (Linux) or installing
the CUDA toolkit. If it still fails, run with `--device cpu` — it always
works, just slower.

**Model download fails**
The models come from Hugging Face. Check your internet connection / proxy,
then retry — downloads resume. You can also try the fallback model directly
(`--model ivrit-ai/faster-whisper-v2-d4` or `--model large-v3`), or download
the model on another machine and pass the local folder to `--model`.

**Out-of-memory errors**
- On CPU: use `--compute-type int8` (the default) and close other programs.
  Large models want ~4–6 GB free RAM.
- On GPU: try `--compute-type int8_float16`, or fall back to `--device cpu`.

**The transcript repeats the same sentence over and over**
This is a known Whisper failure mode on long audio. Rerun with:

```bash
python transcribe.py lecture.mp4 --mode long-safe
```

(equivalent to `--no-condition-on-previous-text`). The quality report also
flags where repetition loops happened, so you can check just those minutes.

**`--resume` cannot start / falls back to a full run**
Resume needs the `..._segments.jsonl` from the interrupted run, next to the
transcript output. Make sure you rerun with the *same* video and `--output`
so the tool finds it, and that the previous run didn't use
`--no-save-segments`. Corrupt trailing lines (from a hard crash) are skipped
automatically. Use `--resume-strict` if you'd rather get an error than a
silent full rerun.

**Financial terms come out wrong (tickers, company names)**
Edit `finance_glossary.txt` to contain the exact terms your lecturer uses
(Hebrew + English spellings) and pass `--glossary finance_glossary.txt`.
This biases the model toward those words during decoding.

**Audio is quiet / muffled**
Try `--normalize-audio`. If you know ffmpeg, a custom chain like
`--audio-filter "highpass=f=80,loudnorm"` can help noisy rooms. Compare a
`--sample-minutes 5` run with and without it before doing the full lecture.

## Too slow on CPU? Use a free Google Colab GPU

If your machine has no NVIDIA GPU, the easiest big speedup is running the
same pipeline on a free Google Colab GPU (T4):

1. Open <https://colab.research.google.com>, create a notebook, and set
   **Runtime → Change runtime type → T4 GPU**.
2. In a cell: `!pip install faster-whisper` (ffmpeg is preinstalled).
3. Upload `transcribe.py` and your video (or mount Google Drive), then run
   `!python transcribe.py lecture.mp4`.
4. Download the resulting `.txt` files.

A ready-made Colab notebook version of this exact pipeline can be created on
request — it uses the same code, so results are identical, just much faster.
