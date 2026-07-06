#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcribe.py — Hebrew lecture video -> AI-readable text transcript.

Transcribes long (1-3 hour) Hebrew lecture videos (stock-market talks) into a
clean, paragraph-based UTF-8 .txt file intended to be pasted into an AI
chatbot for summarization — NOT a subtitle file.

Pipeline:
  1. Detect hardware (CPU/GPU/RAM) and pick device + compute type.
  2. Extract 16 kHz mono PCM WAV audio with ffmpeg (optionally only a sample
     window, optionally with loudness normalization).
  3. Transcribe with faster-whisper (language forced to Hebrew), biased by a
     finance glossary passed as initial_prompt.
  4. Stream raw segments to a JSONL sidecar file (the durable source of
     truth, used for --resume and the quality report).
  5. Merge segments into paragraphs, apply *light* cleanup only
     (no paraphrasing, no changing numbers/tickers/financial wording).
  6. Atomically write the final transcript; split long transcripts into
     AI-chunk part files; write a quality report of suspicious sections.

Usage:
  python transcribe.py lecture.mp4
  python transcribe.py lecture.mp4 --sample-minutes 5
  python transcribe.py lecture.mp4 --resume
  python transcribe.py --self-test
"""

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Console setup: force UTF-8 so Hebrew prints correctly (esp. on Windows).
# ---------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

FALLBACK_MODELS = [
    "ivrit-ai/whisper-large-v3-turbo-ct2",
    "ivrit-ai/faster-whisper-v2-d4",
    "large-v3",
]
DEFAULT_MODEL = FALLBACK_MODELS[0]

# Built-in initial_prompt biasing Whisper toward Hebrew finance vocabulary.
# Used ONLY as decoder context — the transcript is never post-corrected.
DEFAULT_FINANCE_PROMPT = (
    'זוהי הרצאה בעברית על שוק ההון, מניות, מדדים, אג"ח, תשואה, דיבידנד, '
    'מכפיל רווח, אינפלציה, ריבית, הפד, נאסד"ק, S&P 500, QQQ, SPY, VOO, '
    'NVDA, Nvidia, אנבידיה, AAPL, Apple, אפל, TSLA, Tesla, טסלה, MSFT, '
    'Microsoft, מיקרוסופט, META, AMZN, Google, Alphabet, Intel, אינטל.'
)

# Transcription mode presets. Explicit --beam-size /
# --[no-]condition-on-previous-text flags always override these.
MODE_PRESETS = {
    "accurate": {"beam_size": 5, "condition": True},   # best quality
    "long-safe": {"beam_size": 5, "condition": False},  # resists repeat loops
    "fast": {"beam_size": 3, "condition": False},       # speed over quality
}

# AI-chunk part file header lines (feature: better chunk headers).
PART_INTRO = ("זהו חלק מתוך תמלול אוטומטי בעברית של הרצאה על שוק ההון. "
              "ייתכנו שגיאות תמלול קלות.")
PART_NOT_LAST = "אל תסכם סופית עדיין אם לא קיבלת את כל החלקים."
PART_LAST = "זהו החלק האחרון. לאחר חלק זה אפשר לסכם את ההרצאה המלאה."

FFMPEG_INSTALL_HELP = """\
ffmpeg / ffprobe not found. Please install ffmpeg first:

  Windows:
    winget install Gyan.FFmpeg
    (or download from https://www.gyan.dev/ffmpeg/builds/ and add the
     'bin' folder to your PATH, then reopen the terminal)

  macOS:
    brew install ffmpeg

  Ubuntu / Debian Linux:
    sudo apt update && sudo apt install -y ffmpeg

After installing, reopen your terminal and run this script again.
"""

# Hebrew hesitation fillers whose *repetition* is pure stutter noise.
# Deliberately small and conservative — real words (e.g. "אם") are never touched.
FILLER_WORDS = {"אה", "אהה", "אההה", "אמ", "אממ", "אמממ", "אהמ", "המ", "הממ"}

# Quality-report thresholds (report only — the transcript is never modified).
SUSPICIOUS_AVG_LOGPROB = -1.0
SUSPICIOUS_NO_SPEECH_PROB = 0.85
SUSPICIOUS_COMPRESSION_RATIO = 2.4
SUSPICIOUS_SEGMENT_SECONDS = 60.0
SUSPICIOUS_FILLER_COUNT = 5
MAX_REPORT_WARNINGS = 150


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def format_hms(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def die(message: str, code: int = 1) -> "None":
    print(f"\nERROR: {message}", file=sys.stderr)
    sys.exit(code)


def derive_sidecar_path(output_path: str, suffix: str) -> str:
    """lecture_transcript.txt + 'segments.jsonl' -> lecture_segments.jsonl"""
    base, _ = os.path.splitext(output_path)
    if base.endswith("_transcript"):
        base = base[: -len("_transcript")]
    return f"{base}_{suffix}"


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------
def detect_cuda() -> bool:
    """Detect an NVIDIA GPU usable by CTranslate2 (torch is optional)."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return True
    except Exception:
        pass
    try:  # torch is NOT required; only used as a secondary probe if present
        import torch

        if torch.cuda.is_available():
            return True
    except Exception:
        pass
    return False


def get_ram_gb() -> float:
    try:
        import psutil

        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        return 0.0


def resolve_hardware(args) -> dict:
    """Decide device / compute type / CPU threads, honoring CLI overrides."""
    cuda_available = detect_cuda()

    device = args.device
    if device == "auto":
        device = "cuda" if cuda_available else "cpu"
    if device == "cuda" and not cuda_available:
        print("WARNING: --device cuda requested but no CUDA GPU was detected; "
              "trying anyway (this may fail).", file=sys.stderr)

    compute_type = args.compute_type
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    cpu_threads = args.cpu_threads if args.cpu_threads else (os.cpu_count() or 4)

    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu_cores": os.cpu_count() or 0,
        "ram_gb": get_ram_gb(),
        "cuda_available": cuda_available,
        "device": device,
        "compute_type": compute_type,
        "cpu_threads": cpu_threads,
    }


def print_hardware(info: dict) -> None:
    print("=== Hardware ===")
    print(f"  Operating system : {info['os']}")
    print(f"  CPU cores        : {info['cpu_cores']}")
    ram = f"{info['ram_gb']:.1f} GB" if info["ram_gb"] else "unknown"
    print(f"  RAM              : {ram}")
    print(f"  CUDA GPU         : {'available' if info['cuda_available'] else 'not available'}")
    print(f"  Selected device  : {info['device']}")
    print(f"  Compute type     : {info['compute_type']}")
    print(f"  CPU threads      : {info['cpu_threads']}")
    print()


# ---------------------------------------------------------------------------
# Settings resolution (mode presets, prompt, VAD, audio filter)
# ---------------------------------------------------------------------------
def resolve_mode_settings(args) -> dict:
    """Apply --mode preset, letting explicit flags win."""
    preset = MODE_PRESETS[args.mode]
    beam_size = args.beam_size if args.beam_size else preset["beam_size"]
    if args.condition_on_previous_text:
        condition = True
    elif args.no_condition_on_previous_text:
        condition = False
    else:
        condition = preset["condition"]
    return {"beam_size": beam_size, "condition": condition}


def resolve_initial_prompt(args):
    """Return (prompt_text_or_None, human_readable_source).

    Priority: --initial-prompt > --glossary > built-in finance prompt,
    unless --no-default-prompt disables the built-in fallback.
    The prompt only biases the decoder; the transcript is never post-edited.
    """
    if args.initial_prompt:
        return args.initial_prompt.strip(), "command line (--initial-prompt)"
    if args.glossary:
        if not os.path.isfile(args.glossary):
            die(f"glossary file not found: {args.glossary}")
        with open(args.glossary, encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        if not text:
            die(f"glossary file is empty: {args.glossary}")
        if len(text) > 800:
            print("WARNING: glossary is long; Whisper only uses roughly the "
                  "last ~224 tokens of the initial prompt. Keep the most "
                  "important terms near the end.", file=sys.stderr)
        return text, f"glossary file ({os.path.basename(args.glossary)})"
    if args.no_default_prompt:
        return None, "none (--no-default-prompt)"
    return DEFAULT_FINANCE_PROMPT, "built-in Hebrew finance prompt"


def resolve_vad_settings(args):
    """Return (vad_filter: bool, vad_parameters: dict|None, description).

    Parameters unsupported by the installed faster-whisper version are
    dropped with a message instead of crashing.
    """
    if args.no_vad:
        return False, None, "disabled (--no-vad)"

    params = {
        "min_silence_duration_ms": args.vad_min_silence_duration_ms,
        "speech_pad_ms": args.vad_speech_pad_ms,
    }
    if args.vad_threshold is not None:
        params["threshold"] = args.vad_threshold

    # Drop parameters the installed faster-whisper's VadOptions doesn't know.
    try:
        import dataclasses
        from faster_whisper.vad import VadOptions

        supported = {f.name for f in dataclasses.fields(VadOptions)}
        dropped = sorted(k for k in params if k not in supported)
        if dropped:
            print(f"NOTE: this faster-whisper version does not support VAD "
                  f"parameter(s) {', '.join(dropped)} — ignoring them.",
                  file=sys.stderr)
            params = {k: v for k, v in params.items() if k in supported}
    except Exception:
        pass  # can't introspect; pass as-is and rely on the retry in transcribe

    desc = "enabled (" + ", ".join(f"{k}={v}" for k, v in params.items()) + ")"
    return True, params or None, desc


def resolve_audio_filter(args):
    """Return (ffmpeg_audio_filter_or_None, description)."""
    if args.audio_filter:
        return args.audio_filter, f"custom ({args.audio_filter})"
    if args.normalize_audio:
        # EBU R128 loudness normalization — safe single-pass settings.
        return "loudnorm=I=-16:TP=-1.5:LRA=11", "loudnorm (--normalize-audio)"
    return None, "none"


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
def check_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print(FFMPEG_INSTALL_HELP, file=sys.stderr)
        sys.exit(1)


def probe_duration(media_path: str) -> float:
    """Return media duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        media_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        die(f"ffprobe failed on '{media_path}':\n{result.stderr.strip()}")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        die(f"Could not read duration from ffprobe output for '{media_path}'.")


def build_ffmpeg_extract_cmd(video_path: str, wav_path: str,
                             start: float = 0.0, duration: float = None,
                             audio_filter: str = None) -> list:
    """ffmpeg command: video -> 16 kHz mono PCM WAV, optional window/filter."""
    cmd = ["ffmpeg", "-y"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.3f}"]  # before -i: fast seek
    cmd += ["-i", video_path, "-vn", "-ac", "1", "-ar", "16000"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    if audio_filter:
        cmd += ["-af", audio_filter]
    cmd += ["-acodec", "pcm_s16le", wav_path]
    return cmd


def extract_audio(video_path: str, wav_path: str, start: float = 0.0,
                  duration: float = None, audio_filter: str = None) -> None:
    """Extract 16 kHz mono PCM WAV audio (what Whisper expects)."""
    window = ""
    if start and start > 0:
        window += f" from {format_hms(start)}"
    if duration is not None:
        window += f" for {format_hms(duration)}"
    filt = f" [audio filter: {audio_filter}]" if audio_filter else ""
    print(f"Extracting audio{window}{filt} -> {wav_path}")
    cmd = build_ffmpeg_extract_cmd(video_path, wav_path, start, duration, audio_filter)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        die(f"ffmpeg audio extraction failed:\n{result.stderr.strip()[-2000:]}")
    if not os.path.exists(wav_path) or os.path.getsize(wav_path) == 0:
        die("ffmpeg produced an empty audio file.")


# ---------------------------------------------------------------------------
# Light text cleanup (whitespace, stutters, punctuation spacing ONLY)
# ---------------------------------------------------------------------------
_PUNCT_STRIP = ".,!?;:\"'״׳"


def _collapse_stutters(text: str) -> str:
    """Drop stutter repeats: fillers repeated 2+ times, any word 3+ times.

    'מאוד מאוד' (a real emphasis) is kept; 'אה אה אה' and 'של של של'
    are collapsed. Word content is never altered — only exact repeats drop.
    """
    tokens = text.split()
    out = []
    prev_norm = None
    run = 0  # number of consecutive repeats of prev_norm already seen
    for tok in tokens:
        norm = tok.strip(_PUNCT_STRIP)
        if norm and norm == prev_norm:
            run += 1
            # 2nd copy of a filler, or 3rd+ copy of any word -> stutter
            if norm in FILLER_WORDS or run >= 2:
                continue
        else:
            run = 0
            prev_norm = norm
        out.append(tok)
    return " ".join(out)


def clean_segment_text(text: str) -> str:
    """Light cleanup only. Never paraphrases or touches numbers/terms."""
    text = " ".join(text.split())            # normalize whitespace
    if not text:
        return ""
    text = _collapse_stutters(text)
    # No space *before* punctuation (cannot affect numbers like 3.5 / 1,000
    # because those have no space before the separator).
    text = re.sub(r" +([,.!?;:])", r"\1", text)
    # Single space *after* punctuation, only when a Hebrew letter follows —
    # this can never split decimals, percentages or tickers.
    text = re.sub(r"([,.!?;:])(?=[֐-׿])", r"\1 ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Paragraph building + crash-safe writing
# ---------------------------------------------------------------------------
class ParagraphBuilder:
    """Merges cleaned segments into paragraphs.

    A new paragraph starts when the pause between segments exceeds
    `pause_threshold` seconds, or the current paragraph exceeds
    `max_chars` characters.
    """

    def __init__(self, max_chars: int, pause_threshold: float):
        self.max_chars = max_chars
        self.pause_threshold = pause_threshold
        self._parts = []
        self._chars = 0
        self._last_end = None

    def add(self, text: str, start: float, end: float):
        """Feed one segment; returns a completed paragraph or None."""
        finished = None
        if (
            self._parts
            and self._last_end is not None
            and start - self._last_end > self.pause_threshold
        ):
            finished = self._flush()
        self._parts.append(text)
        self._chars += len(text) + 1
        self._last_end = end
        if self._chars >= self.max_chars:
            # Paragraph got long enough — close it at this segment boundary.
            long_par = self._flush()
            finished = f"{finished}\n\n{long_par}" if finished else long_par
        return finished

    def _flush(self):
        paragraph = " ".join(self._parts).strip()
        self._parts = []
        self._chars = 0
        return paragraph or None

    def finalize(self):
        """Return the last, still-open paragraph (or None)."""
        return self._flush() if self._parts else None


def build_paragraphs(records: list, max_chars: int, pause_threshold: float) -> list:
    """Build the full paragraph list from segment records (source of truth)."""
    builder = ParagraphBuilder(max_chars, pause_threshold)
    paragraphs = []
    for rec in records:
        text = clean_segment_text(rec["text"])
        if not text:
            continue
        finished = builder.add(text, rec["start"], rec["end"])
        if finished:
            paragraphs.extend(finished.split("\n\n"))
    tail = builder.finalize()
    if tail:
        paragraphs.append(tail)
    return paragraphs


class StreamingFile:
    """Append-and-flush text file that fsyncs at most every ~10 seconds.
    Used for the human-readable .partial.txt and the segments .jsonl so a
    crash mid-transcription loses almost nothing."""

    def __init__(self, path: str, mode: str = "w", header: str = ""):
        self.path = path
        self._fh = open(path, mode, encoding="utf-8", newline="\n")
        if header:
            self._fh.write(header)
        self._flush(force=True)
        self._last_sync = time.monotonic()

    def write(self, text: str) -> None:
        self._fh.write(text)
        self._flush()

    def _flush(self, force: bool = False) -> None:
        self._fh.flush()
        now = time.monotonic()
        if force or now - self._last_sync > 10:
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            self._last_sync = now

    def close(self) -> None:
        if not self._fh.closed:
            self._flush(force=True)
            self._fh.close()

    def remove(self) -> None:
        self.close()
        try:
            os.remove(self.path)
        except OSError:
            pass


def build_header(source_name: str, duration_s: float, sample_note: str = "") -> str:
    header = (
        f"Source file: {source_name}\n"
        f"Duration: {format_hms(duration_s)}\n"
        f"Language: Hebrew\n"
    )
    if sample_note:
        header += f"Sample: {sample_note}\n"
    header += (
        "Note: This is an auto-generated Hebrew transcript of a stock-market "
        "lecture; minor transcription errors may exist.\n\n"
    )
    return header


def atomic_write(path: str, content: str) -> None:
    """Write via a temp file + os.replace so the target is never half-written."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".transcript_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_part_files(output_path: str, paragraphs: list,
                     split_threshold: int, part_size: int) -> list:
    """If the transcript body exceeds split_threshold chars, write numbered
    part files of ~part_size chars each, split only at paragraph boundaries.
    Each part carries an AI-friendly Hebrew chunk header.
    Returns the list of part file paths created."""
    body_len = sum(len(p) + 2 for p in paragraphs)
    if body_len <= split_threshold:
        return []

    # Group paragraphs into chunks of roughly part_size characters.
    chunks, current, current_len = [], [], 0
    for p in paragraphs:
        if current and current_len + len(p) > part_size:
            chunks.append(current)
            current, current_len = [], 0
        current.append(p)
        current_len += len(p) + 2
    if current:
        chunks.append(current)

    base, _ = os.path.splitext(output_path)
    total = len(chunks)
    paths = []
    for i, chunk in enumerate(chunks, start=1):
        part_path = f"{base}_part{i}.txt"
        closing = PART_LAST if i == total else PART_NOT_LAST
        header = f"חלק {i} מתוך {total}\n{PART_INTRO}\n{closing}\n\n"
        atomic_write(part_path, header + "\n\n".join(chunk) + "\n")
        paths.append(part_path)
    return paths


# ---------------------------------------------------------------------------
# Segment JSONL (source of truth for resume + quality report)
# ---------------------------------------------------------------------------
def segment_to_record(segment, time_offset: float = 0.0) -> dict:
    """Convert a faster-whisper segment to a plain JSON-safe dict, with
    timestamps shifted into full-video time (sample/resume offset)."""
    rec = {
        "start": round(float(segment.start) + time_offset, 3),
        "end": round(float(segment.end) + time_offset, 3),
        "text": segment.text,
    }
    for attr in ("avg_logprob", "compression_ratio", "no_speech_prob", "temperature"):
        value = getattr(segment, attr, None)
        if value is not None:
            try:
                rec[attr] = round(float(value), 4)
            except (TypeError, ValueError):
                pass
    words = getattr(segment, "words", None)
    if words:  # only present when word timestamps were computed
        rec["words"] = [
            {"start": round(float(w.start) + time_offset, 3),
             "end": round(float(w.end) + time_offset, 3),
             "word": w.word}
            for w in words
        ]
    return rec


def load_segment_records(path: str):
    """Read a segments JSONL file, skipping corrupt lines.
    Returns (valid_records, corrupt_line_count)."""
    records, corrupt = [], 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                rec["start"] = float(rec["start"])
                rec["end"] = float(rec["end"])
                rec["text"] = str(rec["text"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                corrupt += 1
                continue
            records.append(rec)
    return records, corrupt


def plan_resume(records: list, overlap_seconds: float):
    """Given previously completed segment records, return
    (resume_point, resume_offset):
      resume_point  — last completed timestamp; new segments ending at or
                      before it are duplicates and get skipped.
      resume_offset — where audio extraction restarts (with overlap)."""
    resume_point = max(r["end"] for r in records)
    resume_offset = max(0.0, resume_point - overlap_seconds)
    return resume_point, resume_offset


# ---------------------------------------------------------------------------
# Quality report
# ---------------------------------------------------------------------------
def detect_suspicious_sections(records: list) -> list:
    """Return human-readable warnings about likely-bad sections.
    Report only — the transcript is NEVER modified based on these."""
    warnings = []

    def span(a, b):
        return f"[{format_hms(a)} - {format_hms(b)}]"

    def shorten(text, limit=60):
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit] + "…"

    # Per-segment checks.
    for rec in records:
        where = span(rec["start"], rec["end"])
        text = " ".join(rec["text"].split())
        tokens = text.split()

        avg_logprob = rec.get("avg_logprob")
        if avg_logprob is not None and avg_logprob < SUSPICIOUS_AVG_LOGPROB:
            warnings.append(f"{where} Low confidence (avg_logprob="
                            f"{avg_logprob:.2f}): \"{shorten(text)}\"")
        no_speech = rec.get("no_speech_prob")
        if no_speech is not None and no_speech > SUSPICIOUS_NO_SPEECH_PROB and text:
            warnings.append(f"{where} Possibly not speech (no_speech_prob="
                            f"{no_speech:.2f}): \"{shorten(text)}\"")
        compression = rec.get("compression_ratio")
        if compression is not None and compression > SUSPICIOUS_COMPRESSION_RATIO:
            warnings.append(f"{where} Possible repetition (compression_ratio="
                            f"{compression:.2f}): \"{shorten(text)}\"")
        if rec["end"] - rec["start"] > SUSPICIOUS_SEGMENT_SECONDS:
            warnings.append(f"{where} Unusually long segment "
                            f"({rec['end'] - rec['start']:.0f}s).")
        # Internal repetition loop: long segment made of very few unique words.
        if len(tokens) >= 12:
            unique_ratio = len(set(tokens)) / len(tokens)
            if unique_ratio < 0.35:
                warnings.append(f"{where} Possible repetition loop inside "
                                f"segment: \"{shorten(text)}\"")
        filler_count = sum(1 for t in tokens if t.strip(_PUNCT_STRIP) in FILLER_WORDS)
        if filler_count >= SUSPICIOUS_FILLER_COUNT:
            warnings.append(f"{where} Many filler repetitions "
                            f"({filler_count} fillers).")

    # Consecutive identical segments (classic Whisper hallucination loop).
    i = 0
    while i < len(records):
        norm = " ".join(records[i]["text"].split())
        j = i + 1
        while j < len(records) and " ".join(records[j]["text"].split()) == norm:
            j += 1
        if norm and j - i >= 3:
            warnings.append(
                f"{span(records[i]['start'], records[j - 1]['end'])} "
                f"Possible repetition loop: phrase repeated {j - i} times: "
                f"\"{shorten(norm)}\"")
        i = j

    warnings.sort()  # timestamps sort lexicographically as [HH:MM:SS ...]
    if len(warnings) > MAX_REPORT_WARNINGS:
        extra = len(warnings) - MAX_REPORT_WARNINGS
        warnings = warnings[:MAX_REPORT_WARNINGS]
        warnings.append(f"... and {extra} more warnings (truncated).")
    return warnings


def build_quality_report(meta: dict, records: list) -> str:
    """Assemble the quality report text. `meta` carries run facts."""
    warnings = detect_suspicious_sections(records)
    lines = [
        f"Quality report — {meta['source']}",
        f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"Source file      : {meta['source']}",
        f"Duration         : {format_hms(meta['duration'])}",
        f"Model            : {meta['model']}",
        f"Mode             : {meta['mode']}",
        f"Device / compute : {meta['device']} / {meta['compute_type']}",
        f"Segments         : {meta['segments']}",
        f"Transcript       : {meta['chars']:,} characters, "
        f"{meta['paragraphs']} paragraphs",
        f"Part files       : {meta['parts']}",
    ]
    if meta.get("rtf"):
        lines.append(f"Processing speed : {meta['rtf']:.2f}x real time "
                     f"(audio seconds per wall-clock second; higher is faster)")
    lines += ["", f"Suspicious sections ({len(warnings)}):"]
    if warnings:
        lines.extend(warnings)
    else:
        lines.append("None detected.")
    lines += ["",
              "Note: these are heuristics for manual review only. "
              "The transcript was NOT modified based on them.",
              ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model loading with fallback chain
# ---------------------------------------------------------------------------
def load_model(requested: str, device: str, compute_type: str, cpu_threads: int):
    """Try the requested model, then the fallback chain. Returns (model, name)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("faster-whisper is not installed. Run:  pip install -r requirements.txt")

    candidates = [requested] + [m for m in FALLBACK_MODELS if m != requested]
    last_error = None
    for name in candidates:
        print(f"Loading model '{name}' (device={device}, compute_type={compute_type})...")
        print("  (first run downloads the model — this can take a while)")
        try:
            model = WhisperModel(
                name,
                device=device,
                compute_type=compute_type,
                cpu_threads=cpu_threads,
            )
            print(f"Model loaded: {name}\n")
            return model, name
        except Exception as exc:  # network errors, bad compute type, OOM, ...
            last_error = exc
            print(f"WARNING: failed to load '{name}': {exc}", file=sys.stderr)
    die(f"Could not load any model. Last error: {last_error}")


# ---------------------------------------------------------------------------
# Transcription loop with progress + streaming writes
# ---------------------------------------------------------------------------
def run_transcription(model, wav_path: str, settings: dict, window: dict,
                      jsonl_writer, partial_writer, args) -> list:
    """Iterate faster-whisper's segment generator, streaming records to the
    JSONL file and readable paragraphs to the partial file. Returns the list
    of new segment records (timestamps in full-video time).

    window: {"offset": extraction start (added to timestamps),
             "resume_point": skip segments ending at/before this,
             "start": progress window start, "end": progress window end}
    """
    transcribe_kwargs = dict(
        language="he",
        beam_size=settings["beam_size"],
        condition_on_previous_text=settings["condition"],
        vad_filter=settings["vad_filter"],
    )
    if settings["vad_filter"] and settings["vad_parameters"]:
        transcribe_kwargs["vad_parameters"] = settings["vad_parameters"]
    if settings["initial_prompt"]:
        transcribe_kwargs["initial_prompt"] = settings["initial_prompt"]

    try:
        segments, _info = model.transcribe(wav_path, **transcribe_kwargs)
    except (TypeError, ValueError) as exc:
        # Most likely a VAD parameter this faster-whisper version rejects.
        if "vad" in str(exc).lower() and "vad_parameters" in transcribe_kwargs:
            print(f"NOTE: VAD parameters rejected by faster-whisper ({exc}); "
                  f"retrying with default VAD settings.", file=sys.stderr)
            transcribe_kwargs.pop("vad_parameters")
            segments, _info = model.transcribe(wav_path, **transcribe_kwargs)
        else:
            raise

    offset = window["offset"]
    resume_point = window["resume_point"]
    win_start, win_end = window["start"], window["end"]
    win_len = max(win_end - win_start, 0.001)

    builder = ParagraphBuilder(args.max_paragraph_chars, args.pause_threshold)
    records = []
    started = time.monotonic()
    last_report = 0.0

    def report(current_ts: float, final: bool = False) -> None:
        nonlocal last_report
        now = time.monotonic()
        if not final and now - last_report < 3:  # throttle to every ~3 s
            return
        last_report = now
        elapsed = now - started
        pct = min(100.0, max(0.0, (current_ts - win_start) / win_len * 100))
        if pct > 0.5:
            eta_str = format_hms(elapsed * (100 - pct) / pct)
        else:
            eta_str = "--:--:--"
        line = (f"  elapsed {format_hms(elapsed)} | "
                f"processed {format_hms(current_ts)} / {format_hms(win_end)} | "
                f"{pct:5.1f}% | ETA {eta_str}")
        print("\r" + line, end="\n" if final else "", file=sys.stderr, flush=True)

    # segments is a GENERATOR — transcription happens as we iterate.
    for segment in segments:
        rec = segment_to_record(segment, offset)
        if rec["end"] <= resume_point:
            continue  # overlap region already covered by a previous run
        records.append(rec)
        if jsonl_writer:
            jsonl_writer.write(json.dumps(rec, ensure_ascii=False) + "\n")
        text = clean_segment_text(rec["text"])
        if text:
            finished = builder.add(text, rec["start"], rec["end"])
            if finished:
                for paragraph in finished.split("\n\n"):
                    partial_writer.write(paragraph + "\n\n")
        report(rec["end"])

    tail = builder.finalize()
    if tail:
        partial_writer.write(tail + "\n\n")
    report(win_end, final=True)
    return records


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def run_self_test(args) -> int:
    """End-to-end pipeline check using a tiny synthetic video."""
    print("=== Self-test ===\n")
    results = []

    def step(name: str, fn):
        try:
            fn()
            results.append((name, True, ""))
            print(f"  [PASS] {name}")
        except SystemExit:
            raise
        except Exception as exc:
            results.append((name, False, str(exc)))
            print(f"  [FAIL] {name}: {exc}")

    # 1. ffmpeg / ffprobe present
    def _check_ffmpeg():
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("ffmpeg/ffprobe not on PATH")
    step("ffmpeg and ffprobe installed", _check_ffmpeg)
    if not results[-1][1]:
        print(FFMPEG_INSTALL_HELP, file=sys.stderr)
        return 1

    tmp_dir = tempfile.mkdtemp(prefix="transcribe_selftest_")
    video_path = os.path.join(tmp_dir, "בדיקה test video.mp4")  # Hebrew + space
    wav_path = os.path.join(tmp_dir, "test_audio.wav")
    out_path = os.path.join(tmp_dir, "test_transcript.txt")

    # 2. Generate a small synthetic video (sine tone + black frame)
    def _gen_video():
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest",
            video_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0 or not os.path.exists(video_path):
            raise RuntimeError(f"could not generate test video: {r.stderr.strip()[-400:]}")
    step("generate synthetic test video (Hebrew filename, spaces)", _gen_video)

    # 3. Audio extraction + duration probe
    def _extract():
        extract_audio(video_path, wav_path)
        dur = probe_duration(wav_path)
        if not 4 <= dur <= 8:
            raise RuntimeError(f"unexpected extracted duration: {dur:.1f}s")
    step("ffmpeg audio extraction to 16 kHz mono WAV + ffprobe duration", _extract)

    # 4. Sample-window extraction + audio filter command generation
    def _sample():
        cmd = build_ffmpeg_extract_cmd("in.mp4", "out.wav", start=1800.0,
                                       duration=300.0, audio_filter="loudnorm")
        assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "1800.000"
        assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "300.000"
        assert "-af" in cmd and cmd[cmd.index("-af") + 1] == "loudnorm"
        assert cmd.index("-ss") < cmd.index("-i"), "-ss must precede -i (fast seek)"
        # Real sample extraction: 2 s starting at 1.5 s of the 6 s test video.
        sample_wav = os.path.join(tmp_dir, "sample.wav")
        extract_audio(video_path, sample_wav, start=1.5, duration=2.0)
        dur = probe_duration(sample_wav)
        assert 1.5 <= dur <= 2.5, f"sample duration {dur:.2f}s, expected ~2s"
    step("sample-window extraction (--sample-minutes) + audio filter flags", _sample)

    # 5. Glossary / initial prompt resolution priority
    def _prompt():
        ns = argparse.Namespace(initial_prompt=None, glossary=None,
                                no_default_prompt=False)
        text, source = resolve_initial_prompt(ns)
        assert text == DEFAULT_FINANCE_PROMPT and "built-in" in source
        gpath = os.path.join(tmp_dir, "מילון פיננסי.txt")
        with open(gpath, "w", encoding="utf-8") as fh:
            fh.write('מניות\nאג"ח   תשואה\n')
        ns.glossary = gpath
        text, source = resolve_initial_prompt(ns)
        assert text == 'מניות אג"ח תשואה' and "glossary" in source
        ns.initial_prompt = "טקסט מותאם אישית"
        text, source = resolve_initial_prompt(ns)
        assert text == "טקסט מותאם אישית" and "--initial-prompt" in source
        ns = argparse.Namespace(initial_prompt=None, glossary=None,
                                no_default_prompt=True)
        text, _ = resolve_initial_prompt(ns)
        assert text is None
    step("glossary / initial prompt resolution priority", _prompt)

    # 6. Mode preset resolution + explicit-flag override
    def _modes():
        ns = argparse.Namespace(mode="accurate", beam_size=None,
                                condition_on_previous_text=False,
                                no_condition_on_previous_text=False)
        s = resolve_mode_settings(ns)
        assert s == {"beam_size": 5, "condition": True}
        ns.mode = "long-safe"
        assert resolve_mode_settings(ns) == {"beam_size": 5, "condition": False}
        ns.mode = "fast"
        assert resolve_mode_settings(ns) == {"beam_size": 3, "condition": False}
        ns.beam_size = 8  # explicit flags beat the preset
        ns.condition_on_previous_text = True
        assert resolve_mode_settings(ns) == {"beam_size": 8, "condition": True}
    step("mode presets (accurate/fast/long-safe) + flag overrides", _modes)

    # 7. Paragraph merge, cleanup invariants, crash-safe & atomic writing
    def _writing():
        header = build_header("בדיקה.mp4", 6.0)
        fake_records = [
            {"start": 0.0, "end": 3.0,
             "text": "שלום אה אה וברוכים הבאים להרצאה על שוק ההון ."},
            {"start": 3.5, "end": 6.0,
             "text": "מניית NVDA עלתה 3.5% היום ,וזה נתון חשוב"},
            {"start": 10.0, "end": 13.0,
             "text": "המדד S&P 500 ירד 1,000 נקודות אצל Apple אפל"},
        ]
        writer = StreamingFile(out_path + ".partial.txt", header=header)
        paragraphs = build_paragraphs(fake_records, max_chars=80,
                                      pause_threshold=2.0)
        for p in paragraphs:
            writer.write(p + "\n\n")
        atomic_write(out_path, header + "\n\n".join(paragraphs) + "\n")
        writer.remove()
        content = open(out_path, encoding="utf-8").read()
        assert "אה אה" not in content, "stutter cleanup failed"
        for token in ("NVDA", "3.5%", "S&P 500", "1,000", "Apple", "אפל"):
            assert token in content, f"cleanup altered '{token}'"
        assert len(paragraphs) >= 2, "pause-based paragraph split failed"
        assert content.startswith("Source file:"), "header missing"
    step("cleanup keeps numbers/tickers + paragraph merge + atomic writing", _writing)

    # 8. Part splitting with AI chunk headers
    def _parts():
        paras = [(f"פסקה {i} " + "תוכן " * 30).strip() for i in range(10)]
        parts_out = os.path.join(tmp_dir, "chunks_transcript.txt")
        paths = write_part_files(parts_out, paras, split_threshold=300,
                                 part_size=400)
        assert len(paths) >= 2, "expected multiple part files"
        for i, path in enumerate(paths, start=1):
            content = open(path, encoding="utf-8").read()
            assert content.startswith(f"חלק {i} מתוך {len(paths)}\n")
            assert PART_INTRO in content
            if i == len(paths):
                assert PART_LAST in content and PART_NOT_LAST not in content
            else:
                assert PART_NOT_LAST in content and PART_LAST not in content
        # All paragraphs preserved, in order, across parts.
        joined = []
        for path in paths:
            body = open(path, encoding="utf-8").read().split("\n\n", 1)[1]
            joined.extend(body.strip().split("\n\n"))
        assert joined == paras, "paragraph content mismatch across parts"
        # Below threshold -> no part files.
        assert write_part_files(parts_out, paras[:1], 300, 400) == []
    step("long transcript splitting + AI chunk headers (חלק X מתוך Y)", _parts)

    # 9. Resume logic from a (partly corrupt) segments JSONL
    def _resume():
        seg_path = os.path.join(tmp_dir, "fake_segments.jsonl")
        with open(seg_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"start": 0.0, "end": 5.0, "text": "א"},
                                ensure_ascii=False) + "\n")
            fh.write(json.dumps({"start": 5.0, "end": 10.0, "text": "ב"},
                                ensure_ascii=False) + "\n")
            fh.write('{"start": 10.0, "end": 15.5, "te')  # crash-truncated line
        records, corrupt = load_segment_records(seg_path)
        assert len(records) == 2 and corrupt == 1
        resume_point, resume_offset = plan_resume(records, overlap_seconds=5.0)
        assert resume_point == 10.0 and resume_offset == 5.0
        # Duplicate-trimming rule: re-decoded segments carry extraction-
        # relative times; after adding resume_offset, anything ending at or
        # before resume_point was already covered by the previous run.
        raw_segments = [(0.0, 4.8, "כפול"), (4.8, 7.0, "חדש")]
        kept = [t for (s, e, t) in raw_segments
                if e + resume_offset > resume_point]
        assert kept == ["חדש"], f"overlap trimming wrong: {kept}"
        # Overlap larger than the recording never yields a negative offset.
        assert plan_resume([{"start": 0, "end": 3.0, "text": "x"}], 5.0) == (3.0, 0.0)
    step("resume: JSONL load, corrupt-line skip, resume point + overlap trim", _resume)

    # 10. Quality report generation with suspicious-section detection
    def _report():
        loop_text = "השוק עולה השוק עולה"
        records = [
            {"start": 0.0, "end": 4.0, "text": "פתיחה רגילה של ההרצאה",
             "avg_logprob": -0.2, "no_speech_prob": 0.01},
            {"start": 4.0, "end": 8.0, "text": "קטע חלש מאוד",
             "avg_logprob": -1.8},
            {"start": 8.0, "end": 12.0, "text": loop_text},
            {"start": 12.0, "end": 16.0, "text": loop_text},
            {"start": 16.0, "end": 20.0, "text": loop_text},
            {"start": 20.0, "end": 30.0,
             "text": "כן " * 20},  # internal repetition loop
        ]
        meta = {"source": "lecture.mp4", "duration": 30.0, "model": "test",
                "mode": "accurate", "device": "cpu", "compute_type": "int8",
                "segments": len(records), "chars": 1234, "paragraphs": 3,
                "parts": 0, "rtf": 1.5}
        report = build_quality_report(meta, records)
        report_path = os.path.join(tmp_dir, "test_quality_report.txt")
        atomic_write(report_path, report)
        content = open(report_path, encoding="utf-8").read()
        assert "Low confidence" in content
        assert "repeated 3 times" in content
        assert "repetition loop inside segment" in content
        assert "[00:00:04 - 00:00:08]" in content
        assert "1.50x real time" in content
        # Clean records -> no warnings.
        clean = build_quality_report(meta, [records[0]])
        assert "None detected." in clean
    step("quality report: metadata + suspicious-section detection", _report)

    # 11. VAD settings resolution
    def _vad():
        ns = argparse.Namespace(no_vad=False, vad_min_silence_duration_ms=2000,
                                vad_speech_pad_ms=400, vad_threshold=None)
        enabled, params, desc = resolve_vad_settings(ns)
        assert enabled and params.get("min_silence_duration_ms") == 2000
        ns.vad_threshold = 0.6
        _, params, _ = resolve_vad_settings(ns)
        assert params is None or "threshold" not in params or params["threshold"] == 0.6
        ns.no_vad = True
        enabled, params, desc = resolve_vad_settings(ns)
        assert not enabled and params is None and "disabled" in desc
    step("VAD flag resolution (--no-vad, thresholds, version-safe filtering)", _vad)

    # 12. Model loading + real transcription (tiny model to keep it fast)
    if args.skip_model:
        print("  [SKIP] model loading (--skip-model)")
    else:
        def _model():
            # Use WhisperModel directly (tiny model, fast download) so a
            # failure here is reported as FAIL instead of aborting the test.
            from faster_whisper import WhisperModel
            model = WhisperModel("tiny", device=args.hw["device"],
                                 compute_type=args.hw["compute_type"],
                                 cpu_threads=args.hw["cpu_threads"])
            segments, _ = model.transcribe(
                wav_path, language="he", beam_size=5, vad_filter=True,
                initial_prompt=DEFAULT_FINANCE_PROMPT)
            list(segments)  # drain the generator; a pure tone yields ~nothing
        step("faster-whisper model load + transcription (tiny model)", _model)

    shutil.rmtree(tmp_dir, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\nSelf-test: {len(results) - len(failed)}/{len(results)} steps passed.")
    if failed:
        print("Failed steps:", ", ".join(name for name, _, _ in failed))
        return 1
    print("Self-test PASSED. The pipeline is working.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Transcribe a Hebrew lecture video into an AI-readable "
                    ".txt transcript (paragraphs, no timestamps).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("video", nargs="?", help="input video file (.mp4/.mkv/.mov/...)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Whisper model name or local path")
    parser.add_argument("--output", default=None,
                        help="output .txt path (default: <video>_transcript.txt "
                             "next to the input)")
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="auto",
                        help="compute device")
    parser.add_argument("--compute-type",
                        choices=["int8", "float16", "int8_float16", "float32", "auto"],
                        default="auto", help="model precision")
    parser.add_argument("--cpu-threads", type=int, default=0,
                        help="CPU threads for inference (0 = all cores)")
    parser.add_argument("--keep-audio", action="store_true",
                        help="keep the extracted temporary WAV file")

    # Mode / decoding settings
    parser.add_argument("--mode", choices=["accurate", "fast", "long-safe"],
                        default="accurate",
                        help="preset: accurate (best), long-safe (resists "
                             "repetition loops on long audio), fast")
    parser.add_argument("--beam-size", type=int, default=None,
                        help="beam size (overrides the --mode preset)")
    cond_group = parser.add_mutually_exclusive_group()
    cond_group.add_argument("--condition-on-previous-text", action="store_true",
                            help="force conditioning on previous text on "
                                 "(overrides the --mode preset)")
    cond_group.add_argument("--no-condition-on-previous-text", action="store_true",
                            help="force conditioning off (use if the transcript "
                                 "repeats itself / hallucinates in loops)")

    # Glossary / initial prompt
    parser.add_argument("--glossary", default=None, metavar="PATH",
                        help="UTF-8 text file of finance terms passed to the "
                             "model as initial_prompt (decoder bias only; the "
                             "transcript is never post-corrected)")
    parser.add_argument("--initial-prompt", default=None, metavar="TEXT",
                        help="custom initial prompt text (beats --glossary)")
    parser.add_argument("--no-default-prompt", action="store_true",
                        help="disable the built-in finance prompt when no "
                             "glossary/prompt is supplied")

    # Sample-first mode
    parser.add_argument("--sample-minutes", type=float, default=None,
                        help="transcribe only N minutes (quality/speed test "
                             "before a full 1-3h run)")
    parser.add_argument("--sample-start-minute", type=float, default=0.0,
                        help="with --sample-minutes: where the sample starts")

    # VAD tuning
    parser.add_argument("--no-vad", action="store_true",
                        help="disable voice-activity-detection filtering")
    parser.add_argument("--vad-min-silence-duration-ms", type=int, default=2000,
                        help="silence (ms) before VAD splits speech")
    parser.add_argument("--vad-speech-pad-ms", type=int, default=400,
                        help="padding (ms) added around detected speech")
    parser.add_argument("--vad-threshold", type=float, default=None,
                        help="VAD speech probability threshold (model default "
                             "if omitted)")

    # Audio filtering
    parser.add_argument("--normalize-audio", action="store_true",
                        help="apply ffmpeg loudness normalization (loudnorm) "
                             "during extraction — may help quiet recordings")
    parser.add_argument("--audio-filter", default=None, metavar="FILTER",
                        help="advanced: exact ffmpeg audio filter string to "
                             "apply during extraction (beats --normalize-audio)")

    # Sidecar outputs
    parser.add_argument("--save-segments", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="save raw segments to <name>_segments.jsonl "
                             "(needed for --resume and the quality report)")
    parser.add_argument("--quality-report", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="write <name>_quality_report.txt flagging "
                             "suspicious sections")

    # Resume
    parser.add_argument("--resume", action="store_true",
                        help="resume an interrupted run from the segments "
                             ".jsonl instead of retranscribing everything")
    parser.add_argument("--resume-strict", action="store_true",
                        help="with --resume: fail instead of falling back to a "
                             "full run when resume data is missing/corrupt")
    parser.add_argument("--overlap-seconds", type=float, default=5.0,
                        help="audio overlap re-transcribed before the resume "
                             "point (duplicates are trimmed)")

    # Paragraphing / splitting
    parser.add_argument("--max-paragraph-chars", type=int, default=750,
                        help="start a new paragraph after this many characters")
    parser.add_argument("--pause-threshold", type=float, default=2.0,
                        help="pause (seconds) between segments that starts a new paragraph")
    parser.add_argument("--split-threshold", type=int, default=50000,
                        help="if the transcript exceeds this many characters, "
                             "also write numbered part files")
    parser.add_argument("--part-size", type=int, default=40000,
                        help="target size (characters) of each part file")

    parser.add_argument("--self-test", action="store_true",
                        help="run an end-to-end pipeline self-test and exit")
    parser.add_argument("--skip-model", action="store_true",
                        help="with --self-test: skip the model download/load step")
    return parser.parse_args(argv)


def print_settings(args, hw, mode_settings, prompt_source, vad_desc,
                   filter_desc, sample_desc, resume_desc) -> None:
    print("=== Transcription settings ===")
    print(f"  Mode                       : {args.mode}")
    print(f"  Model (requested)          : {args.model}")
    print(f"  Beam size                  : {mode_settings['beam_size']}")
    print(f"  condition_on_previous_text : {mode_settings['condition']}")
    print(f"  Initial prompt             : {prompt_source}")
    print(f"  VAD                        : {vad_desc}")
    print(f"  Audio filter               : {filter_desc}")
    print(f"  Device / compute           : {hw['device']} / {hw['compute_type']}")
    print(f"  Sample window              : {sample_desc}")
    print(f"  Resume                     : {resume_desc}")
    print()


def main(argv=None) -> int:
    args = parse_args(argv)

    hw = resolve_hardware(args)
    print_hardware(hw)
    args.hw = hw

    if args.self_test:
        return run_self_test(args)

    if not args.video:
        print("ERROR: missing input video file.\n"
              "Usage: python transcribe.py <video-file>   "
              "(or: python transcribe.py --self-test)", file=sys.stderr)
        return 2

    video_path = os.path.abspath(args.video)
    if not os.path.isfile(video_path):
        die(f"input file not found: {video_path}")

    check_ffmpeg()

    # --- Resolve decoding settings -----------------------------------------
    mode_settings = resolve_mode_settings(args)
    prompt_text, prompt_source = resolve_initial_prompt(args)
    vad_enabled, vad_parameters, vad_desc = resolve_vad_settings(args)
    audio_filter, filter_desc = resolve_audio_filter(args)
    if args.normalize_audio or args.audio_filter:
        print(f"Audio filtering enabled: {filter_desc}")

    # --- Sample window ------------------------------------------------------
    duration_s = probe_duration(video_path)
    if args.sample_start_minute and args.sample_minutes is None:
        die("--sample-start-minute requires --sample-minutes")
    sample_start = 0.0
    sample_duration = None
    sample_desc = "full video"
    sample_note = ""
    if args.sample_minutes is not None:
        if args.sample_minutes <= 0:
            die("--sample-minutes must be positive")
        sample_start = max(0.0, args.sample_start_minute * 60.0)
        if sample_start >= duration_s:
            die(f"--sample-start-minute {args.sample_start_minute:g} is beyond "
                f"the video duration ({format_hms(duration_s)})")
        sample_duration = min(args.sample_minutes * 60.0, duration_s - sample_start)
        sample_desc = (f"minutes {args.sample_start_minute:g}"
                       f"-{args.sample_start_minute + sample_duration / 60.0:g}")
        sample_note = (f"minutes {args.sample_start_minute:g}"
                       f"-{args.sample_start_minute + sample_duration / 60.0:g} "
                       f"of the full lecture")
        if args.resume:
            die("--resume cannot be combined with --sample-minutes "
                "(samples are one-shot quality tests)")

    # --- Resolve output paths ----------------------------------------------
    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        stem, _ = os.path.splitext(video_path)
        if args.sample_minutes is not None:
            output_path = f"{stem}_sample_{args.sample_minutes:g}min_transcript.txt"
        else:
            output_path = stem + "_transcript.txt"
    partial_path = output_path + ".partial.txt"
    segments_path = derive_sidecar_path(output_path, "segments.jsonl")
    report_path = derive_sidecar_path(output_path, "quality_report.txt")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # --- Resume planning ----------------------------------------------------
    # Design decision: the segments JSONL is the source of truth. On resume we
    # reload it, restart audio extraction `--overlap-seconds` before the last
    # completed timestamp, skip re-decoded segments that end inside the
    # already-covered region, and rebuild the final transcript from ALL
    # segments — so paragraphs are never duplicated.
    existing_records = []
    resume_point = 0.0     # segments ending at/before this are already done
    resume_offset = 0.0    # where audio extraction starts (timestamp offset)
    rebuild_only = False
    resume_desc = "no"
    if args.resume:
        if not args.save_segments:
            print("NOTE: --resume requires the segments JSONL; "
                  "enabling --save-segments.", file=sys.stderr)
            args.save_segments = True

        def resume_unavailable(reason):
            if args.resume_strict:
                die(f"cannot resume ({reason}) and --resume-strict was passed")
            print(f"WARNING: cannot resume ({reason}); "
                  f"falling back to a full transcription.", file=sys.stderr)

        if not os.path.exists(segments_path):
            resume_unavailable(f"no segments file at {segments_path}")
        else:
            existing_records, corrupt = load_segment_records(segments_path)
            if corrupt:
                print(f"WARNING: skipped {corrupt} corrupt line(s) in "
                      f"{segments_path} (likely a crash mid-write).",
                      file=sys.stderr)
            if not existing_records:
                existing_records = []
                resume_unavailable("segments file contains no valid segments")
            else:
                resume_point, resume_offset = plan_resume(existing_records,
                                                          args.overlap_seconds)
                if resume_point >= duration_s - 2.0:
                    rebuild_only = True
                    resume_desc = ("previous run already complete — "
                                   "rebuilding transcript from segments")
                else:
                    resume_desc = (f"from {format_hms(resume_offset)} "
                                   f"(last completed {format_hms(resume_point)}, "
                                   f"overlap {args.overlap_seconds:g}s)")

    resuming = bool(existing_records)

    print(f"Input   : {video_path}")
    print(f"Duration: {format_hms(duration_s)}")
    print(f"Output  : {output_path}")
    if args.save_segments:
        print(f"Segments: {segments_path}")
    print()
    print_settings(args, hw, mode_settings, prompt_source, vad_desc,
                   filter_desc, sample_desc, resume_desc)

    new_records = []
    wall_seconds = 0.0
    model_name = "(not run — transcript rebuilt from existing segments)"
    tmp_dir = None

    if not rebuild_only:
        # --- Extract audio (full, sample window, or resume tail) -----------
        extract_start = sample_start if args.sample_minutes is not None else resume_offset
        tmp_dir = tempfile.mkdtemp(prefix="transcribe_audio_")
        wav_path = os.path.join(tmp_dir, "audio_16k_mono.wav")
        extract_audio(video_path, wav_path, start=extract_start,
                      duration=sample_duration, audio_filter=audio_filter)

        # --- Load model (with fallback chain) -------------------------------
        model, model_name = load_model(args.model, hw["device"],
                                       hw["compute_type"], hw["cpu_threads"])

        # --- Progress window + sidecar writers ------------------------------
        if args.sample_minutes is not None:
            window = {"offset": sample_start, "resume_point": 0.0,
                      "start": sample_start,
                      "end": sample_start + sample_duration}
            header_duration = sample_duration
        else:
            window = {"offset": resume_offset, "resume_point": resume_point,
                      "start": resume_offset, "end": duration_s}
            header_duration = duration_s

        jsonl_writer = None
        if args.save_segments:
            if resuming:
                # Rewrite only the valid lines, then append — this heals any
                # crash-truncated trailing line before we continue.
                atomic_write(segments_path, "".join(
                    json.dumps(r, ensure_ascii=False) + "\n"
                    for r in existing_records))
                jsonl_writer = StreamingFile(segments_path, mode="a")
            else:
                jsonl_writer = StreamingFile(segments_path, mode="w")

        # The .partial.txt is a live, human-readable progress view. On a
        # resumed run it only contains the newly transcribed portion — the
        # JSONL (not the partial file) is what resume reads.
        header = build_header(os.path.basename(video_path), header_duration,
                              sample_note)
        partial_writer = StreamingFile(partial_path, header=header)

        settings = {
            "beam_size": mode_settings["beam_size"],
            "condition": mode_settings["condition"],
            "vad_filter": vad_enabled,
            "vad_parameters": vad_parameters,
            "initial_prompt": prompt_text,
        }

        started_wall = time.monotonic()
        try:
            new_records = run_transcription(model, wav_path, settings, window,
                                            jsonl_writer, partial_writer, args)
        except KeyboardInterrupt:
            partial_writer.close()
            if jsonl_writer:
                jsonl_writer.close()
            print(f"\n\nInterrupted. Progress saved:\n"
                  f"  segments : {segments_path if args.save_segments else '(disabled)'}\n"
                  f"  partial  : {partial_path}\n"
                  f"Rerun with --resume to continue from where it stopped.",
                  file=sys.stderr)
            return 130
        except Exception as exc:
            partial_writer.close()
            if jsonl_writer:
                jsonl_writer.close()
            print(f"\n\nTranscription failed: {exc}\n"
                  f"Progress saved:\n"
                  f"  segments : {segments_path if args.save_segments else '(disabled)'}\n"
                  f"  partial  : {partial_path}\n"
                  f"Rerun with --resume to continue from where it stopped.",
                  file=sys.stderr)
            return 1
        wall_seconds = time.monotonic() - started_wall
        if jsonl_writer:
            jsonl_writer.close()
        partial_writer.close()
    else:
        header = build_header(os.path.basename(video_path), duration_s)

    # --- Rebuild final transcript from ALL segments (old + new) -------------
    all_records = sorted(existing_records + new_records,
                         key=lambda r: (r["start"], r["end"]))
    paragraphs = build_paragraphs(all_records, args.max_paragraph_chars,
                                  args.pause_threshold)
    if not paragraphs:
        die("no speech was transcribed (empty result). "
            f"Partial output kept at {partial_path} for inspection.")

    body = "\n\n".join(paragraphs) + "\n"
    atomic_write(output_path, header + body)
    try:  # partial file is redundant once the final transcript exists
        os.remove(partial_path)
    except OSError:
        pass

    part_paths = write_part_files(output_path, paragraphs,
                                  args.split_threshold, args.part_size)

    # --- Quality report ------------------------------------------------------
    if args.quality_report:
        rtf = None
        if new_records and wall_seconds > 1:
            audio_done = max(r["end"] for r in new_records) - (
                sample_start if args.sample_minutes is not None else resume_offset)
            if audio_done > 0:
                rtf = audio_done / wall_seconds
        meta = {
            "source": os.path.basename(video_path),
            "duration": sample_duration if args.sample_minutes is not None else duration_s,
            "model": model_name,
            "mode": args.mode,
            "device": hw["device"],
            "compute_type": hw["compute_type"],
            "segments": len(all_records),
            "chars": len(body),
            "paragraphs": len(paragraphs),
            "parts": len(part_paths),
            "rtf": rtf,
        }
        atomic_write(report_path, build_quality_report(meta, all_records))

    # --- Cleanup temp audio ---------------------------------------------------
    if tmp_dir:
        if args.keep_audio:
            print(f"\nExtracted audio kept at: {os.path.join(tmp_dir, 'audio_16k_mono.wav')}")
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\nDone. Model used: {model_name}")
    print(f"Transcript ({len(body):,} characters, {len(paragraphs)} paragraphs):")
    print(f"  {output_path}")
    if part_paths:
        print(f"Long transcript — also split into {len(part_paths)} parts for "
              f"pasting into an AI chatbot:")
        for p in part_paths:
            print(f"  {p}")
    if args.save_segments:
        print(f"Raw segments (for --resume / auditing): {segments_path}")
    if args.quality_report:
        print(f"Quality report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
