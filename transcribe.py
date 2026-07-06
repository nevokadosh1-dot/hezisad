#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcribe.py — Hebrew lecture video -> AI-readable text transcript.

Transcribes long (1-3 hour) Hebrew lecture videos (stock-market talks) into a
clean, paragraph-based UTF-8 .txt file intended to be pasted into an AI
chatbot for summarization — NOT a subtitle file.

Pipeline:
  1. Detect hardware (CPU/GPU/RAM) and pick device + compute type.
  2. Extract 16 kHz mono PCM WAV audio with ffmpeg.
  3. Transcribe with faster-whisper (language forced to Hebrew).
  4. Merge segments into paragraphs, apply *light* cleanup only
     (no paraphrasing, no changing numbers/tickers/financial wording).
  5. Stream completed paragraphs to a crash-safe .partial.txt file,
     then atomically write the final transcript.
  6. If the transcript is very long, also emit numbered part files
     sized for pasting into a chatbot.

Usage:
  python transcribe.py lecture.mp4
  python transcribe.py --self-test
"""

import argparse
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

    info = {
        "os": f"{platform.system()} {platform.release()}",
        "cpu_cores": os.cpu_count() or 0,
        "ram_gb": get_ram_gb(),
        "cuda_available": cuda_available,
        "device": device,
        "compute_type": compute_type,
        "cpu_threads": cpu_threads,
    }
    return info


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


def extract_audio(video_path: str, wav_path: str) -> None:
    """Extract 16 kHz mono PCM WAV audio (what Whisper expects)."""
    print(f"Extracting audio -> {wav_path}")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",              # drop video
        "-ac", "1",         # mono
        "-ar", "16000",     # 16 kHz
        "-acodec", "pcm_s16le",
        wav_path,
    ]
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


class PartialWriter:
    """Streams completed paragraphs to <output>.partial.txt as they arrive,
    flushing to disk so a crash mid-transcription loses almost nothing."""

    def __init__(self, partial_path: str, header: str):
        self.path = partial_path
        self._fh = open(partial_path, "w", encoding="utf-8", newline="\n")
        self._fh.write(header)
        self._flush(force=True)
        self._last_sync = time.monotonic()

    def write_paragraph(self, paragraph: str) -> None:
        self._fh.write(paragraph + "\n\n")
        self._flush()

    def _flush(self, force: bool = False) -> None:
        self._fh.flush()
        now = time.monotonic()
        if force or now - self._last_sync > 10:  # fsync at most every ~10 s
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


def build_header(source_name: str, duration_s: float) -> str:
    return (
        f"Source file: {source_name}\n"
        f"Duration: {format_hms(duration_s)}\n"
        f"Language: Hebrew\n"
        f"Note: This is an auto-generated Hebrew transcript of a stock-market "
        f"lecture; minor transcription errors may exist.\n"
        f"\n"
    )


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


def write_part_files(output_path: str, header: str, paragraphs: list,
                     split_threshold: int, part_size: int) -> list:
    """If the transcript body exceeds split_threshold chars, write numbered
    part files of ~part_size chars each, split only at paragraph boundaries.
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
        content = f"חלק {i} מתוך {total}\n\n" + header + "\n\n".join(chunk) + "\n"
        atomic_write(part_path, content)
        paths.append(part_path)
    return paths


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
def transcribe_file(model, wav_path: str, duration_s: float, args,
                    partial_writer: PartialWriter) -> list:
    """Run faster-whisper over the WAV, streaming paragraphs to the partial
    file as they complete. Returns the full list of paragraphs."""
    condition = not args.no_condition_on_previous_text
    print(f"Transcribing (language=he, beam_size=5, vad_filter=True, "
          f"condition_on_previous_text={condition})...")

    segments, _info = model.transcribe(
        wav_path,
        language="he",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=condition,
    )

    builder = ParagraphBuilder(args.max_paragraph_chars, args.pause_threshold)
    paragraphs = []
    started = time.monotonic()
    last_report = 0.0

    def report(current_ts: float, final: bool = False) -> None:
        nonlocal last_report
        now = time.monotonic()
        if not final and now - last_report < 3:  # throttle to every ~3 s
            return
        last_report = now
        elapsed = now - started
        pct = min(100.0, current_ts / duration_s * 100) if duration_s else 0.0
        if pct > 0.5:
            eta = elapsed * (100 - pct) / pct
            eta_str = format_hms(eta)
        else:
            eta_str = "--:--:--"
        line = (f"  elapsed {format_hms(elapsed)} | "
                f"processed {format_hms(current_ts)} / {format_hms(duration_s)} | "
                f"{pct:5.1f}% | ETA {eta_str}")
        end = "\n" if final else ""
        print("\r" + line, end=end, file=sys.stderr, flush=True)

    # segments is a GENERATOR — transcription happens as we iterate.
    for segment in segments:
        text = clean_segment_text(segment.text)
        if not text:
            continue
        finished = builder.add(text, segment.start, segment.end)
        if finished:
            for paragraph in finished.split("\n\n"):
                paragraphs.append(paragraph)
                partial_writer.write_paragraph(paragraph)
        report(segment.end)

    tail = builder.finalize()
    if tail:
        paragraphs.append(tail)
        partial_writer.write_paragraph(tail)
    report(duration_s, final=True)
    return paragraphs


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

    # 4. Paragraph building / cleanup / output writing (no model needed)
    def _writing():
        header = build_header("בדיקה.mp4", 6.0)
        builder = ParagraphBuilder(max_chars=80, pause_threshold=2.0)
        writer = PartialWriter(out_path + ".partial.txt", header)
        fake_segments = [
            ("שלום אה אה וברוכים הבאים להרצאה על שוק ההון .", 0.0, 3.0),
            ("המניה עלתה 3.5 אחוזים היום ,וזה נתון חשוב", 3.5, 6.0),
            ("אחרי הפסקה ארוכה מתחילים נושא חדש לגמרי", 10.0, 13.0),
        ]
        paragraphs = []
        for raw, start, end in fake_segments:
            text = clean_segment_text(raw)
            done = builder.add(text, start, end)
            if done:
                for p in done.split("\n\n"):
                    paragraphs.append(p)
                    writer.write_paragraph(p)
        tail = builder.finalize()
        if tail:
            paragraphs.append(tail)
            writer.write_paragraph(tail)
        atomic_write(out_path, header + "\n\n".join(paragraphs) + "\n")
        writer.remove()
        content = open(out_path, encoding="utf-8").read()
        assert "אה אה" not in content, "stutter cleanup failed"
        assert "3.5" in content, "number was altered by cleanup"
        assert len(paragraphs) >= 2, "pause-based paragraph split failed"
        assert content.startswith("Source file:"), "header missing"
    step("cleanup + paragraph merge + crash-safe & atomic writing", _writing)

    # 5. Model loading + real transcription (tiny model to keep it fast)
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
            segments, _ = model.transcribe(wav_path, language="he", beam_size=5,
                                           vad_filter=True)
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
    parser.add_argument("--no-condition-on-previous-text", action="store_true",
                        help="disable conditioning on previous text (use if the "
                             "transcript repeats itself / hallucinates in loops)")
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

    # --- Resolve output paths -------------------------------------------
    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        stem, _ = os.path.splitext(video_path)
        output_path = stem + "_transcript.txt"
    partial_path = output_path + ".partial.txt"
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    duration_s = probe_duration(video_path)
    print(f"Input   : {video_path}")
    print(f"Duration: {format_hms(duration_s)}")
    print(f"Output  : {output_path}\n")

    # --- Extract audio ----------------------------------------------------
    tmp_dir = tempfile.mkdtemp(prefix="transcribe_audio_")
    wav_path = os.path.join(tmp_dir, "audio_16k_mono.wav")
    extract_audio(video_path, wav_path)

    # --- Load model (with fallback chain) ---------------------------------
    model, model_name = load_model(args.model, hw["device"],
                                   hw["compute_type"], hw["cpu_threads"])

    header = build_header(os.path.basename(video_path), duration_s)
    partial_writer = PartialWriter(partial_path, header)

    try:
        paragraphs = transcribe_file(model, wav_path, duration_s, args, partial_writer)
    except KeyboardInterrupt:
        partial_writer.close()
        print(f"\n\nInterrupted. Partial transcript saved at:\n  {partial_path}",
              file=sys.stderr)
        return 130
    except Exception as exc:
        partial_writer.close()
        print(f"\n\nTranscription failed: {exc}\n"
              f"Partial transcript (work so far) saved at:\n  {partial_path}",
              file=sys.stderr)
        return 1

    if not paragraphs:
        partial_writer.close()
        die("no speech was transcribed (empty result). "
            f"Partial file kept at {partial_path} for inspection.")

    # --- Final atomic write + part files -----------------------------------
    body = "\n\n".join(paragraphs) + "\n"
    atomic_write(output_path, header + body)
    partial_writer.remove()  # success: the partial file is now redundant

    part_paths = write_part_files(output_path, header, paragraphs,
                                  args.split_threshold, args.part_size)

    # --- Cleanup temp audio -------------------------------------------------
    if args.keep_audio:
        print(f"\nExtracted audio kept at: {wav_path}")
    else:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\nDone. Model used: {model_name}")
    print(f"Transcript ({len(body):,} characters, {len(paragraphs)} paragraphs):")
    print(f"  {output_path}")
    if part_paths:
        print(f"Long transcript — also split into {len(part_paths)} parts for pasting "
              f"into an AI chatbot:")
        for p in part_paths:
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
