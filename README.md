# Hebrew Lecture Transcriber

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
  (`..._part1.txt`, `..._part2.txt`, ...) sized for pasting into a chatbot,
  each starting with `חלק X מתוך Y`.
- Crash-safe: paragraphs are streamed to a `.partial.txt` file during
  transcription, so hours of work are never lost if something crashes.

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

## Usage

### Basic

```bash
python transcribe.py lecture.mp4
```

This creates `lecture_transcript.txt` next to the video. Paths with spaces or
Hebrew characters are fine — just quote them:

```bash
python transcribe.py "הרצאה על שוק ההון 2026.mp4"
```

### Custom output path

```bash
python transcribe.py lecture.mp4 --output /path/to/my_transcript.txt
```

### Custom model

```bash
python transcribe.py lecture.mp4 --model ivrit-ai/faster-whisper-v2-d4
# or any faster-whisper model name / local CTranslate2 model directory
```

### Force CPU mode

```bash
python transcribe.py lecture.mp4 --device cpu --compute-type int8
```

### Force CUDA (NVIDIA GPU) mode

```bash
python transcribe.py lecture.mp4 --device cuda --compute-type float16
```

### Self-test

```bash
python transcribe.py --self-test
```

Verifies ffmpeg, audio extraction, text cleanup/paragraph writing, and model
loading end-to-end (it downloads the small `tiny` model for speed). To skip
the model download:

```bash
python transcribe.py --self-test --skip-model
```

### All options

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `ivrit-ai/whisper-large-v3-turbo-ct2` | model name or local path |
| `--output` | `<video>_transcript.txt` | output text file |
| `--device` | `auto` | `cpu`, `cuda`, or `auto` (auto-detects GPU) |
| `--compute-type` | `auto` | `int8`, `float16`, `int8_float16`, `float32` (`auto` = float16 on GPU, int8 on CPU) |
| `--cpu-threads` | all cores | CPU threads for inference |
| `--keep-audio` | off | keep the extracted temporary WAV |
| `--no-condition-on-previous-text` | off | use if the transcript starts repeating itself |
| `--max-paragraph-chars` | 750 | max paragraph length before starting a new one |
| `--pause-threshold` | 2.0 | pause (seconds) that starts a new paragraph |
| `--split-threshold` | 50000 | transcripts longer than this also get part files |
| `--part-size` | 40000 | target characters per part file |

## Speed expectations (honest)

- **CPU:** transcription with a large model typically runs around
  0.5×–1.5× real time on a modern CPU. A 2-hour lecture can take **1.5–4
  hours** (sometimes more on older machines). This is normal.
- **NVIDIA GPU (CUDA):** usually **5–15× faster** — a 2-hour lecture often
  finishes in 10–25 minutes.
- The first run also downloads the model (~1–3 GB), which takes a few
  minutes depending on your connection. Models are cached afterwards.

Progress (elapsed time, position in the audio, percent, ETA) is printed while
transcribing, and completed paragraphs are continuously saved to
`<output>.txt.partial.txt`, so you can watch partial results and nothing is
lost on a crash.

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
This is a known Whisper failure mode on long audio. Re-run with:

```bash
python transcribe.py lecture.mp4 --no-condition-on-previous-text
```

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
