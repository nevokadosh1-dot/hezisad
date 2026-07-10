#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_export.py — Kikar Framework Agent export for the Hebrew transcriber.

Turns a finished transcription into an agent-ready episode folder:

    <agent-export-root>/            (= kikar-agent/data/raw_transcripts)
      e01/                          episodes e01..e100
        transcript.txt
        segments.jsonl
        metadata.json
      extras/
        x001/                       extras x001..x999
          transcript.txt
          segments.jsonl
          metadata.json

Shared by the CLI (transcribe.py) and the GUI (gui_app.py). Responsibilities:
  * episode/extra ID normalization + validation
  * empty-folder detection and auto-next destination selection
  * overwrite protection with timestamped backups
  * deterministic (offline) metadata generation
  * optional cost-conscious Claude enrichment (lazy anthropic import)
  * metadata validation and merge with existing customized metadata
  * atomic export of transcript.txt / segments.jsonl / metadata.json

The transcription engine itself is untouched: this module only consumes the
finished transcript text and segment records the engine already produces.
"""

import datetime
import json
import os
import re
import shutil
import sys
import tempfile

EPISODE_MIN, EPISODE_MAX = 1, 100
EXTRA_MIN, EXTRA_MAX = 1, 999

EXTRA_SOURCE_TYPES = [
    "special_lecture", "interview", "guest_appearance", "live_session",
    "short_update", "course_lesson", "podcast", "other",
]
VALID_SOURCE_TYPES = ["episode"] + EXTRA_SOURCE_TYPES

VALID_STATUSES = ["processing", "ready", "sample", "metadata_incomplete",
                  "failed"]

DEFAULT_SPEAKER = "Hezi"
DEFAULT_EPISODE_SERIES = "Kikar Hashuk"
DEFAULT_EXTRA_SERIES = "Kikar Hashuk Extras"

# Low-cost model suited to classification/structured JSON. Overridable via
# --metadata-model and the KIKAR_METADATA_MODEL env var so model-name churn
# never breaks the app.
DEFAULT_METADATA_MODEL = "claude-haiku-4-5-20251001"
METADATA_MODEL_ENV = "KIKAR_METADATA_MODEL"

# Identity/technical fields the exporter always controls; existing customized
# metadata can never override these (they must match the destination folder).
ALWAYS_REFRESH_FIELDS = {
    "episode", "episode_id", "extra_number", "extra_id", "lecture_id",
    "source_audio_file", "duration_seconds", "duration_hms", "language",
    "transcription_model", "transcription_mode", "generated_at", "status",
    "metadata_generation", "sample",
}

# Fields Claude enrichment is allowed to fill.
ENRICHABLE_FIELDS = ["lecture_title", "topics", "tickers", "countries",
                     "people", "companies", "macro_topics", "market_regime",
                     "summary"]

INGESTION_COMMANDS = (
    "python scripts/ingest_transcripts.py --reingest\n"
    "python scripts/build_framework_map.py\n"
    "python scripts/check_setup.py"
)


class AgentExportError(Exception):
    """Raised for user-facing agent-export problems (bad destination,
    unsafe overwrite, invalid metadata, ...)."""


class EnrichmentUnavailable(Exception):
    """Claude enrichment cannot run (no key / no package)."""


class EnrichmentFailed(Exception):
    """Claude enrichment ran but did not produce usable metadata."""


# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------
def normalize_episode_id(value) -> str:
    """'1' / '01' / 'e1' / 'E01' -> 'e01'. Raises AgentExportError."""
    text = str(value).strip().lower()
    match = re.fullmatch(r"e?0*(\d{1,3})", text)
    if not match:
        raise AgentExportError(
            f"invalid episode id '{value}' — use forms like 1, 01, e1, E01")
    number = int(match.group(1))
    if not EPISODE_MIN <= number <= EPISODE_MAX:
        raise AgentExportError(
            f"episode {number} is out of range "
            f"(e{EPISODE_MIN:02d}-e{EPISODE_MAX})")
    return f"e{number:02d}"


def normalize_extra_id(value) -> str:
    """'4' / '004' / 'x4' / 'X004' -> 'x004'. Raises AgentExportError."""
    text = str(value).strip().lower()
    match = re.fullmatch(r"x?0*(\d{1,3})", text)
    if not match:
        raise AgentExportError(
            f"invalid extra id '{value}' — use forms like 4, 004, x4, X004")
    number = int(match.group(1))
    if not EXTRA_MIN <= number <= EXTRA_MAX:
        raise AgentExportError(
            f"extra {number} is out of range (x{EXTRA_MIN:03d}-x{EXTRA_MAX})")
    return f"x{number:03d}"


def id_number(ident: str) -> int:
    return int(ident[1:])


def target_dir(root: str, kind: str, ident: str) -> str:
    """kind: 'episode' -> <root>/e01 ; 'extra' -> <root>/extras/x001."""
    if kind == "episode":
        return os.path.join(root, ident)
    return os.path.join(root, "extras", ident)


# ---------------------------------------------------------------------------
# Empty-folder detection + auto-next selection
# ---------------------------------------------------------------------------
def _has_valid_segments(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    float(rec["start"]); float(rec["end"]); str(rec["text"])
                    return True
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return False


def folder_is_populated(path: str) -> bool:
    """A destination counts as populated when transcript.txt has real text
    or segments.jsonl has at least one valid segment. A metadata.json
    template alone does NOT make a folder populated."""
    if not os.path.isdir(path):
        return False
    transcript = os.path.join(path, "transcript.txt")
    if os.path.isfile(transcript):
        try:
            with open(transcript, encoding="utf-8") as fh:
                if fh.read().strip():
                    return True
        except OSError:
            return True  # unreadable existing file: play safe, treat as full
    segments = os.path.join(path, "segments.jsonl")
    if os.path.isfile(segments) and _has_valid_segments(segments):
        return True
    return False


def find_next_empty(root: str, kind: str):
    """Return the first genuinely empty episode/extra id, or None."""
    if kind == "episode":
        ids = (f"e{n:02d}" for n in range(EPISODE_MIN, EPISODE_MAX + 1))
    else:
        ids = (f"x{n:03d}" for n in range(EXTRA_MIN, EXTRA_MAX + 1))
    for ident in ids:
        if not folder_is_populated(target_dir(root, kind, ident)):
            return ident
    return None


# ---------------------------------------------------------------------------
# Deterministic metadata (Layer A — always works offline)
# ---------------------------------------------------------------------------
# Conservative extraction tables. Only well-known, unambiguous references are
# extracted — nothing is invented. Entries require >= 2 mentions (tickers 1).
KNOWN_TICKERS = {
    "NVDA", "AAPL", "TSLA", "MSFT", "META", "AMZN", "GOOGL", "GOOG", "INTC",
    "AMD", "PLTR", "QQQ", "SPY", "VOO", "IWM", "TLT", "SMH", "NFLX", "AVGO",
    "MU", "TSM", "COIN", "MSTR", "BRK.B", "XLE", "XLF", "GLD", "SLV", "USO",
}

COUNTRY_PATTERNS = {
    "United States": ["ארצות הברית", 'ארה"ב', "ארהב"],
    "China": ["סין"],
    "Israel": ["ישראל"],
    "Russia": ["רוסיה"],
    "Iran": ["איראן", "אירן"],
    "Japan": ["יפן"],
    "Germany": ["גרמניה"],
    "India": ["הודו"],
    "Ukraine": ["אוקראינה"],
    "United Kingdom": ["בריטניה", "אנגליה"],
    "Saudi Arabia": ["סעודיה", "ערב הסעודית"],
    "Taiwan": ["טייוואן", "טאיוואן"],
    "Europe": ["אירופה"],
}

PEOPLE_PATTERNS = {
    "Donald Trump": ["טראמפ", "טרמפ", "Trump"],
    "Joe Biden": ["ביידן", "Biden"],
    "Benjamin Netanyahu": ["נתניהו"],
    "Jerome Powell": ["פאוול", "פאואל", "Powell"],
    "Warren Buffett": ["באפט", "בפט", "Buffett"],
    "Elon Musk": ["מאסק", "Musk"],
    "Jensen Huang": ["ג'נסן", "Jensen Huang"],
}

COMPANY_PATTERNS = {
    "Nvidia": ["אנבידיה", "Nvidia", "NVIDIA", "נבידיה"],
    "Apple": ["אפל", "Apple"],
    "Tesla": ["טסלה", "Tesla"],
    "Microsoft": ["מיקרוסופט", "Microsoft"],
    "Google": ["גוגל", "Google", "אלפאבית", "Alphabet"],
    "Amazon": ["אמזון", "Amazon"],
    "Meta": ["מטא", "פייסבוק", "Meta"],
    "Intel": ["אינטל", "Intel"],
    "Palantir": ["פלנטיר", "Palantir"],
    "TSMC": ["טי אס אם סי", "TSMC"],
}

MACRO_TOPIC_PATTERNS = {
    "inflation": ["אינפלציה"],
    "interest rates": ["ריבית"],
    "Federal Reserve": ["הפד", "הפדרל ריזרב"],
    "recession": ["מיתון"],
    "bonds": ['אג"ח', "אגח"],
    "real estate": ['נדל"ן', "נדלן"],
    "crypto": ["קריפטו", "ביטקוין", "Bitcoin"],
    "oil and energy": ["נפט", "אנרגיה"],
    "employment": ["אבטלה", "שוק העבודה"],
    "tariffs": ["מכסים", "מכס"],
    "geopolitics": ["מלחמה", "גיאופוליטי"],
    "AI": ["בינה מלאכותית", "AI"],
}

_TICKER_RE = re.compile(r"\b[A-Z]{2,5}(?:\.[A-Z])?\b")


def _count_mentions(text: str, patterns: list) -> int:
    return sum(text.count(p) for p in patterns)


def extract_tickers(text: str) -> list:
    """Conservative: only symbols from the known-tickers list, preserving
    casing. Ordinary uppercase words are never treated as tickers."""
    found, seen = [], set()
    for match in _TICKER_RE.finditer(text):
        symbol = match.group(0)
        if symbol in KNOWN_TICKERS and symbol not in seen:
            seen.add(symbol)
            found.append(symbol)
    return found


def _extract_by_patterns(text: str, table: dict, min_mentions: int = 2) -> list:
    results = []
    for name, patterns in table.items():
        if _count_mentions(text, patterns) >= min_mentions:
            results.append(name)
    return results


def extract_date_from_filename(filename: str):
    """Recognize a date in the filename (never the file mtime).
    Supports 2026-07-09 / 2026_07_09 / 2026.07.09 / 09-07-2026 / 20260709."""
    name = os.path.basename(filename)
    patterns = [
        (r"(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})", "ymd"),
        (r"(\d{1,2})[-_.](\d{1,2})[-_.](20\d{2})", "dmy"),
        (r"(20\d{2})(\d{2})(\d{2})", "ymd_compact"),
    ]
    for pattern, order in patterns:
        for match in re.finditer(pattern, name):
            a, b, c = (int(g) for g in match.groups())
            year, month, day = (a, b, c) if order.startswith("ymd") else (c, b, a)
            try:
                return datetime.date(year, month, day).isoformat()
            except ValueError:
                continue
    return None


def title_from_filename(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"[-_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem


def format_hms(seconds: float) -> str:
    seconds = max(0, int(round(seconds or 0)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_deterministic_metadata(kind, ident, source_file, duration_seconds,
                                 transcript_text, model_name, mode,
                                 status="ready", source_type=None,
                                 sample_info=None) -> dict:
    """Layer A: metadata that never needs a network or an API key."""
    number = id_number(ident)
    text = transcript_text or ""
    meta = {}
    if kind == "episode":
        meta["episode"] = number
        meta["episode_id"] = ident
        meta["lecture_id"] = f"episode-{ident}"
        default_title = f"Episode {number}"
        series = DEFAULT_EPISODE_SERIES
        default_source_type = "episode"
    else:
        meta["extra_number"] = number
        meta["extra_id"] = ident
        meta["lecture_id"] = f"extra-{ident}"
        default_title = f"Extra {number}"
        series = DEFAULT_EXTRA_SERIES
        default_source_type = "other"

    filename_title = title_from_filename(source_file)
    meta.update({
        "lecture_date": extract_date_from_filename(source_file),
        "lecture_title": filename_title or default_title,
        "speaker": DEFAULT_SPEAKER,
        "series": series,
        "source_type": source_type or default_source_type,
        "source_audio_file": os.path.basename(source_file),
        "duration_seconds": round(float(duration_seconds or 0), 1),
        "duration_hms": format_hms(duration_seconds or 0),
        "language": "Hebrew",
        "topics": [],
        "tickers": extract_tickers(text),
        "countries": _extract_by_patterns(text, COUNTRY_PATTERNS),
        "people": _extract_by_patterns(text, PEOPLE_PATTERNS),
        "companies": _extract_by_patterns(text, COMPANY_PATTERNS),
        "macro_topics": _extract_by_patterns(text, MACRO_TOPIC_PATTERNS),
        "market_regime": None,
        "summary": "",
        "notes": "",
        "transcription_model": model_name or "",
        "transcription_mode": mode or "",
        "generated_at": datetime.datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "status": status,
        "metadata_generation": {
            "method": "automatic",
            "ai_enriched": False,
            "model": None,
        },
    })
    if sample_info:
        meta["sample"] = sample_info
    return meta


# ---------------------------------------------------------------------------
# Optional Claude enrichment (Layer B — lazy, cost-conscious, never required)
# ---------------------------------------------------------------------------
def resolve_metadata_model(cli_value=None) -> str:
    return cli_value or os.environ.get(METADATA_MODEL_ENV) \
        or DEFAULT_METADATA_MODEL


def build_transcript_excerpt(text: str, budget: int = 8000) -> str:
    """Representative excerpt instead of the full 1-3h transcript:
    opening, a few middle windows, and the ending."""
    text = text.strip()
    if len(text) <= budget:
        return text
    head = text[:int(budget * 0.35)]
    tail = text[-int(budget * 0.2):]
    middle_budget = budget - len(head) - len(tail)
    windows = []
    for frac in (0.35, 0.55, 0.75):
        pos = int(len(text) * frac)
        windows.append(text[pos:pos + middle_budget // 3])
    marker = "\n[...]\n"
    return head + marker + marker.join(windows) + marker + tail


_ENRICH_PROMPT = """\
You extract metadata from a Hebrew stock-market lecture transcript.

Rules:
- Use ONLY information that appears in the transcript excerpt below.
- Do not invent dates, tickers, people, companies, or topics.
- Tickers must be real stock/ETF symbols explicitly referenced.
- Distinguish geopolitical topics (put in "topics") from market/economic
  topics (put in "macro_topics").
- "summary" must be a factual 2-5 sentence summary of what was discussed.
- "lecture_title" is a short descriptive title (English or Hebrew) for the
  lecture content.
- "market_regime" is a short phrase like "bull market" / "correction" /
  "high-rate environment" only if the speaker clearly characterizes it,
  otherwise null.
- Return ONLY a JSON object, no markdown fences, with exactly these keys:
  lecture_title (string), topics (array of strings),
  tickers (array of strings), countries (array of strings, English names),
  people (array of strings, full English names when clear),
  companies (array of strings), macro_topics (array of strings),
  market_regime (string or null), summary (string).

Transcript excerpt:
---
{excerpt}
---
"""


def _parse_strict_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text.strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in response")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return data


def _sanitize_enrichment(data: dict) -> dict:
    """Keep only allowed fields with correct types; drop everything else."""
    clean = {}
    title = data.get("lecture_title")
    if isinstance(title, str) and title.strip():
        clean["lecture_title"] = title.strip()
    for key in ("topics", "tickers", "countries", "people", "companies",
                "macro_topics"):
        value = data.get(key)
        if isinstance(value, list):
            items = [str(v).strip() for v in value
                     if isinstance(v, (str, int, float)) and str(v).strip()]
            if items:
                clean[key] = items
    regime = data.get("market_regime")
    if isinstance(regime, str) and regime.strip():
        clean["market_regime"] = regime.strip()
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        clean["summary"] = summary.strip()
    return clean


def enrich_with_claude(transcript_text: str, model: str, log=print) -> dict:
    """Ask Claude for enrichment fields. Raises EnrichmentUnavailable when
    prerequisites are missing, EnrichmentFailed when the request/JSON fails
    after one retry. Never called unless the metadata mode wants it."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise EnrichmentUnavailable("ANTHROPIC_API_KEY is not set")
    try:
        import anthropic  # lazy: not a requirement for normal transcription
    except ImportError:
        raise EnrichmentUnavailable(
            "the 'anthropic' package is not installed (pip install anthropic)")

    excerpt = build_transcript_excerpt(transcript_text)
    prompt = _ENRICH_PROMPT.format(excerpt=excerpt)
    client = anthropic.Anthropic()
    last_error = None
    for attempt in (1, 2):
        try:
            message = client.messages.create(
                model=model,
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt if attempt == 1
                           else prompt + "\n\nYour previous reply was not "
                                "valid JSON. Return ONLY the JSON object."}],
            )
            raw = "".join(block.text for block in message.content
                          if getattr(block, "type", "") == "text")
            data = _parse_strict_json(raw)
            clean = _sanitize_enrichment(data)
            if not clean:
                raise ValueError("enrichment returned no usable fields")
            log(f"Claude metadata enrichment OK (model {model}, "
                f"attempt {attempt})")
            return clean
        except Exception as exc:  # API errors, JSON errors, validation
            last_error = exc
            log(f"Claude enrichment attempt {attempt} failed: {exc}")
    raise EnrichmentFailed(str(last_error))


# ---------------------------------------------------------------------------
# Metadata merge + validation
# ---------------------------------------------------------------------------
def _is_empty_value(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def normalize_list(values) -> list:
    """Strings only, stripped, de-duplicated case-insensitively while
    preserving the first-seen casing, no empties."""
    out, seen = [], set()
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        key = text.lower()
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out


def merge_metadata(generated: dict, existing: dict, cli_overrides: dict) -> dict:
    """Priority: CLI > existing customized > generated (AI/deterministic).
    Unknown custom keys in existing metadata are preserved. Identity and
    technical fields always come from `generated`."""
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in generated.items():
        if key in ALWAYS_REFRESH_FIELDS or _is_empty_value(merged.get(key)):
            merged[key] = value
    for key, value in (cli_overrides or {}).items():
        if value not in (None, ""):
            merged[key] = value
    return merged


def validate_metadata(meta: dict, kind: str, ident: str) -> list:
    """Return a list of problems (empty list = valid). Also normalizes the
    list fields in place (dedupe / strip / drop empties)."""
    problems = []
    number = id_number(ident)
    if kind == "episode":
        if meta.get("episode") != number:
            problems.append(f"episode must be {number}")
        if meta.get("episode_id") != ident:
            problems.append(f"episode_id must be '{ident}'")
        if meta.get("lecture_id") != f"episode-{ident}":
            problems.append(f"lecture_id must be 'episode-{ident}'")
    else:
        if meta.get("extra_number") != number:
            problems.append(f"extra_number must be {number}")
        if meta.get("extra_id") != ident:
            problems.append(f"extra_id must be '{ident}'")
        if meta.get("lecture_id") != f"extra-{ident}":
            problems.append(f"lecture_id must be 'extra-{ident}'")

    date = meta.get("lecture_date")
    if date is not None:
        try:
            datetime.date.fromisoformat(str(date))
        except ValueError:
            problems.append(f"lecture_date '{date}' is not YYYY-MM-DD or null")

    for key in ("topics", "tickers", "countries", "people", "companies",
                "macro_topics"):
        if not isinstance(meta.get(key), list):
            problems.append(f"{key} must be a list")
        else:
            meta[key] = normalize_list(meta[key])

    if meta.get("source_type") not in VALID_SOURCE_TYPES:
        problems.append(f"source_type '{meta.get('source_type')}' is not one "
                        f"of {VALID_SOURCE_TYPES}")
    if meta.get("status") not in VALID_STATUSES:
        problems.append(f"status '{meta.get('status')}' is not one of "
                        f"{VALID_STATUSES}")
    try:
        if float(meta.get("duration_seconds", 0)) < 0:
            problems.append("duration_seconds must be non-negative")
    except (TypeError, ValueError):
        problems.append("duration_seconds must be a number")

    try:  # must serialize as valid UTF-8 JSON
        json.dumps(meta, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        problems.append(f"metadata is not JSON-serializable: {exc}")
    return problems


# ---------------------------------------------------------------------------
# Atomic writing, backups, export
# ---------------------------------------------------------------------------
def _atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agent_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_metadata(path: str, meta: dict) -> None:
    _atomic_write(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def load_existing_metadata(folder: str):
    path = os.path.join(folder, "metadata.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def backup_existing_files(folder: str) -> str:
    """Copy the three canonical files into <folder>/backups/<timestamp>/.
    Returns the backup path ('' when there was nothing to back up)."""
    existing = [f for f in ("transcript.txt", "segments.jsonl", "metadata.json")
                if os.path.isfile(os.path.join(folder, f))]
    if not existing:
        return ""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_dir = os.path.join(folder, "backups", stamp)
    os.makedirs(backup_dir, exist_ok=True)
    for name in existing:
        shutil.copy2(os.path.join(folder, name), os.path.join(backup_dir, name))
    return backup_dir


def segments_to_jsonl(records: list) -> str:
    """Serialize segment records in the engine's exact JSONL structure."""
    return "".join(json.dumps(rec, ensure_ascii=False) + "\n"
                   for rec in records)


# ---------------------------------------------------------------------------
# Plan + lifecycle used by CLI and GUI
# ---------------------------------------------------------------------------
class ExportPlan:
    """Resolved, validated destination for one agent export."""

    def __init__(self, root, kind, ident, overwrite=False):
        self.root = os.path.abspath(root)
        self.kind = kind            # "episode" | "extra"
        self.ident = ident          # "e01" | "x001"
        self.overwrite = overwrite
        self.folder = target_dir(self.root, kind, ident)
        self.was_populated = folder_is_populated(self.folder)
        self.existing_metadata = load_existing_metadata(self.folder)
        self.backup_dir = ""
        self.processing_written = False

    @property
    def display_id(self) -> str:
        return self.ident.upper()


def resolve_plan(root, episode=None, extra=None, auto_next_episode=False,
                 auto_next_extra=False, overwrite=False) -> ExportPlan:
    """Resolve destination flags into an ExportPlan. Raises AgentExportError
    for conflicts, invalid ids, exhausted archives, or unsafe overwrites."""
    chosen = [name for name, value in [
        ("--episode", episode), ("--extra", extra),
        ("--auto-next-episode", auto_next_episode),
        ("--auto-next-extra", auto_next_extra)] if value]
    if len(chosen) > 1:
        raise AgentExportError(
            f"choose only one destination ({', '.join(chosen)} given)")
    if not chosen:
        raise AgentExportError(
            "agent export needs a destination: --episode, --extra, "
            "--auto-next-episode or --auto-next-extra")

    if episode:
        kind, ident = "episode", normalize_episode_id(episode)
    elif extra:
        kind, ident = "extra", normalize_extra_id(extra)
    elif auto_next_episode:
        kind = "episode"
        ident = find_next_empty(root, kind)
        if not ident:
            raise AgentExportError(
                f"no empty episode folder found in e{EPISODE_MIN:02d}-"
                f"e{EPISODE_MAX} under {root}")
    else:
        kind = "extra"
        ident = find_next_empty(root, kind)
        if not ident:
            raise AgentExportError(
                f"no empty extra folder found in x{EXTRA_MIN:03d}-"
                f"x{EXTRA_MAX} under {root}")

    plan = ExportPlan(root, kind, ident, overwrite=overwrite)
    if plan.was_populated and not overwrite:
        raise AgentExportError(
            f"{plan.display_id} already contains a lecture.\n"
            f"Use --overwrite-agent-export only if you intentionally want "
            f"to replace it.")
    return plan


def write_processing_metadata(plan: ExportPlan, source_file, mode,
                              source_type=None, cli_overrides=None) -> None:
    """Lifecycle step: mark the destination as 'processing' before the long
    transcription starts. Merges over existing customized metadata so a
    pre-filled template is never lost."""
    meta = build_deterministic_metadata(
        plan.kind, plan.ident, source_file, 0.0, "", "", mode,
        status="processing", source_type=source_type)
    merged = merge_metadata(meta, plan.existing_metadata, cli_overrides or {})
    merged["status"] = "processing"
    os.makedirs(plan.folder, exist_ok=True)
    write_metadata(os.path.join(plan.folder, "metadata.json"), merged)
    plan.processing_written = True


def mark_failed(plan: ExportPlan) -> None:
    """Best-effort: flip a 'processing' metadata to 'failed'. Never touches
    a populated lecture and never raises."""
    try:
        if plan.was_populated or not plan.processing_written:
            return
        meta = load_existing_metadata(plan.folder)
        if meta and meta.get("status") == "processing":
            meta["status"] = "failed"
            write_metadata(os.path.join(plan.folder, "metadata.json"), meta)
    except Exception:
        pass


def finalize_export(plan: ExportPlan, transcript_content, segment_records,
                    source_file, duration_seconds, transcript_body,
                    model_name, mode, metadata_mode="auto",
                    metadata_model=None, cli_overrides=None,
                    sample_info=None, log=print) -> dict:
    """Write transcript.txt / segments.jsonl / metadata.json atomically into
    the destination folder. Returns a summary dict for logging/GUI.

    transcript_content: the full transcript file content (header included).
    transcript_body:    transcript text only (used for metadata extraction).
    """
    status = "sample" if sample_info else "ready"

    # ---- metadata: deterministic layer -----------------------------------
    meta = build_deterministic_metadata(
        plan.kind, plan.ident, source_file, duration_seconds,
        transcript_body, model_name, mode, status=status,
        source_type=(cli_overrides or {}).get("source_type"),
        sample_info=sample_info)

    # ---- metadata: optional Claude enrichment ------------------------------
    ai_ran = False
    model = resolve_metadata_model(metadata_model)
    if metadata_mode in ("auto", "claude"):
        try:
            enrichment = enrich_with_claude(transcript_body, model, log=log)
            for key, value in enrichment.items():
                if key in ENRICHABLE_FIELDS and not _is_empty_value(value):
                    meta[key] = value
            ai_ran = True
        except EnrichmentUnavailable as exc:
            if metadata_mode == "claude":
                log(f"WARNING: --metadata-mode claude requested but "
                    f"enrichment is unavailable: {exc}")
                meta["status"] = "metadata_incomplete"
            else:
                log(f"Metadata enrichment skipped: {exc} "
                    f"(deterministic metadata used)")
        except EnrichmentFailed as exc:
            log(f"WARNING: Claude metadata enrichment failed: {exc}")
            if metadata_mode == "claude":
                meta["status"] = "metadata_incomplete"
    elif metadata_mode == "none":
        # Minimal metadata: keep identity/technical fields, clear extraction.
        for key in ("topics", "tickers", "countries", "people", "companies",
                    "macro_topics"):
            meta[key] = []
    meta["metadata_generation"] = {
        "method": "automatic",
        "ai_enriched": ai_ran,
        "model": model if ai_ran else None,
    }
    if sample_info:
        meta["status"] = "sample"

    # ---- merge with existing customized metadata + CLI values -------------
    merged = merge_metadata(meta, plan.existing_metadata, cli_overrides or {})
    merged["status"] = meta["status"]  # lifecycle status is never inherited

    problems = validate_metadata(merged, plan.kind, plan.ident)
    if problems:
        raise AgentExportError("metadata validation failed: "
                               + "; ".join(problems))

    # ---- backups + atomic writes -------------------------------------------
    if plan.was_populated:
        if not plan.overwrite:
            raise AgentExportError(  # defense in depth; resolve_plan checks too
                f"{plan.display_id} already contains a lecture.\n"
                f"Use --overwrite-agent-export only if you intentionally "
                f"want to replace it.")
        plan.backup_dir = backup_existing_files(plan.folder)
        log(f"Existing lecture backed up to: {plan.backup_dir}")

    os.makedirs(plan.folder, exist_ok=True)
    transcript_path = os.path.join(plan.folder, "transcript.txt")
    segments_path = os.path.join(plan.folder, "segments.jsonl")
    metadata_path = os.path.join(plan.folder, "metadata.json")
    _atomic_write(transcript_path, transcript_content)
    _atomic_write(segments_path, segments_to_jsonl(segment_records))
    write_metadata(metadata_path, merged)

    return {
        "folder": plan.folder,
        "id": plan.ident,
        "display_id": plan.display_id,
        "kind": plan.kind,
        "files": {
            "transcript": transcript_path,
            "segments": segments_path,
            "metadata": metadata_path,
        },
        "sizes": {os.path.basename(p): os.path.getsize(p)
                  for p in (transcript_path, segments_path, metadata_path)},
        "status": merged["status"],
        "ai_enriched": ai_ran,
        "metadata_model": model if ai_ran else None,
        "backup_dir": plan.backup_dir,
        "metadata": merged,
    }


# ---------------------------------------------------------------------------
# Optional direct ingestion (disabled by default; may cost money)
# ---------------------------------------------------------------------------
def agent_project_root(export_root: str):
    """<project>/data/raw_transcripts -> <project>, when it looks right."""
    candidate = os.path.dirname(os.path.dirname(os.path.abspath(export_root)))
    if os.path.isfile(os.path.join(candidate, "scripts",
                                   "ingest_transcripts.py")):
        return candidate
    return None


def run_ingestion(export_root: str, rebuild_framework_map=False, log=print) -> bool:
    """Run the Kikar Agent ingestion scripts. Returns True on success.
    Failures are reported but must not undo a successful export."""
    import subprocess

    project = agent_project_root(export_root)
    if not project:
        log("WARNING: could not locate the Kikar Agent project root "
            "(scripts/ingest_transcripts.py) above the export root — "
            "skipping ingestion. Run it manually:\n" + INGESTION_COMMANDS)
        return False
    commands = [[sys.executable, os.path.join("scripts",
                                              "ingest_transcripts.py"),
                 "--reingest"]]
    if rebuild_framework_map:
        commands.append([sys.executable,
                         os.path.join("scripts", "build_framework_map.py")])
    ok = True
    for cmd in commands:
        log(f"Running: {' '.join(cmd)}  (cwd={project})")
        result = subprocess.run(cmd, cwd=project, capture_output=True,
                                text=True, encoding="utf-8")
        tail = (result.stdout or "").strip().splitlines()[-5:]
        for line in tail:
            log(f"  {line}")
        if result.returncode != 0:
            log(f"WARNING: {os.path.basename(cmd[1])} exited with code "
                f"{result.returncode}: {(result.stderr or '').strip()[-500:]}")
            ok = False
            break
    return ok
