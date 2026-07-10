#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_agent_export.py — deterministic tests for the Kikar Agent export.

Run:  python -m unittest test_agent_export -v
      (or simply: python test_agent_export.py)

No network or Whisper model is needed. The end-to-end integration test uses
the engine's rebuild-from-segments path (real transcribe.py main(), fake
finalized segments) and requires ffmpeg; it is skipped when ffmpeg is absent.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent_export  # noqa: E402
import transcribe  # noqa: E402

HEBREW_SEGMENTS = [
    {"start": 0.72, "end": 5.6, "text": " אל תעשו כמוני, כי תפסידו את המכנסיים.",
     "avg_logprob": -0.4483, "compression_ratio": 1.8854,
     "no_speech_prob": 0.0, "temperature": 0.6},
    {"start": 5.9, "end": 11.2, "text": "מניית NVDA עלתה 3.5% אחרי הדוחות.",
     "avg_logprob": -0.31, "compression_ratio": 1.6, "no_speech_prob": 0.01,
     "temperature": 0.0},
    {"start": 12.0, "end": 18.0, "text": "טראמפ דיבר על סין, וגם טראמפ הזכיר "
                                          "את סין שוב בהקשר של מכסים ומכס.",
     "avg_logprob": -0.4, "compression_ratio": 1.7, "no_speech_prob": 0.0,
     "temperature": 0.0},
]
HEBREW_TEXT = " ".join(r["text"] for r in HEBREW_SEGMENTS)
TRANSCRIPT_CONTENT = ("Source file: lecture.mp3\nDuration: 00:00:18\n"
                      "Language: Hebrew\nNote: test\n\n" + HEBREW_TEXT + "\n")


def make_export(plan, **overrides):
    """finalize_export with sane defaults for tests."""
    kwargs = dict(
        transcript_content=TRANSCRIPT_CONTENT,
        segment_records=HEBREW_SEGMENTS,
        source_file="lecture 2026-07-09 בעברית.mp3",
        duration_seconds=18.0,
        transcript_body=HEBREW_TEXT,
        model_name="ivrit-ai/whisper-large-v3-turbo-ct2",
        mode="long-safe",
        metadata_mode="basic",
        log=lambda *_: None,
    )
    kwargs.update(overrides)
    return agent_export.finalize_export(plan, **kwargs)


class TempArchive(unittest.TestCase):
    def setUp(self):
        # Hebrew folder name + space: Windows-style paths must work (test 17)
        self.base = tempfile.mkdtemp(prefix="kikar_test_")
        self.root = os.path.join(self.base, "ארכיון קיקר", "data",
                                 "raw_transcripts")
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)


class TestNormalization(unittest.TestCase):
    def test_episode_forms_normalize_to_e01(self):  # required test 8
        for raw in ("1", "01", "e1", "E01", " e01 "):
            self.assertEqual(agent_export.normalize_episode_id(raw), "e01")
        self.assertEqual(agent_export.normalize_episode_id("100"), "e100")

    def test_extra_forms_normalize_to_x004(self):  # required test 9
        for raw in ("4", "004", "x4", "X004"):
            self.assertEqual(agent_export.normalize_extra_id(raw), "x004")
        self.assertEqual(agent_export.normalize_extra_id("999"), "x999")

    def test_out_of_range_and_garbage_rejected(self):
        for bad in ("0", "101", "e101", "abc", "", "e-1"):
            with self.assertRaises(agent_export.AgentExportError):
                agent_export.normalize_episode_id(bad)
        for bad in ("0", "1000", "y1"):
            with self.assertRaises(agent_export.AgentExportError):
                agent_export.normalize_extra_id(bad)


class TestExportStructure(TempArchive):
    def test_episode_export_creates_exactly_three_files(self):  # test 1
        plan = agent_export.resolve_plan(self.root, episode="e01")
        result = make_export(plan)
        folder = os.path.join(self.root, "e01")
        self.assertEqual(result["folder"], folder)
        self.assertEqual(sorted(os.listdir(folder)),
                         ["metadata.json", "segments.jsonl", "transcript.txt"])

    def test_extra_export_structure(self):  # test 2
        plan = agent_export.resolve_plan(self.root, extra="x001")
        result = make_export(plan)
        folder = os.path.join(self.root, "extras", "x001")
        self.assertEqual(result["folder"], folder)
        self.assertTrue(os.path.isfile(os.path.join(folder, "transcript.txt")))
        meta = json.load(open(os.path.join(folder, "metadata.json"),
                              encoding="utf-8"))
        self.assertEqual(meta["extra_id"], "x001")
        self.assertEqual(meta["lecture_id"], "extra-x001")
        self.assertEqual(meta["series"], "Kikar Hashuk Extras")
        self.assertEqual(meta["source_type"], "other")

    def test_transcript_and_segments_preserved_exactly(self):  # tests 18+19
        plan = agent_export.resolve_plan(self.root, episode="e01")
        make_export(plan)
        folder = os.path.join(self.root, "e01")
        content = open(os.path.join(folder, "transcript.txt"),
                       encoding="utf-8").read()
        self.assertEqual(content, TRANSCRIPT_CONTENT)
        self.assertIn("NVDA", content)
        self.assertIn("3.5%", content)
        lines = open(os.path.join(folder, "segments.jsonl"),
                     encoding="utf-8").read().strip().splitlines()
        self.assertEqual([json.loads(l) for l in lines], HEBREW_SEGMENTS)

    def test_single_export_creates_single_record(self):  # test 20
        plan = agent_export.resolve_plan(self.root, episode="e01")
        make_export(plan)
        created = []
        for dirpath, _dirs, files in os.walk(self.root):
            if "transcript.txt" in files:
                created.append(dirpath)
        self.assertEqual(created, [os.path.join(self.root, "e01")])


class TestOverwriteProtection(TempArchive):
    def _populate(self, ident="e07"):
        plan = agent_export.resolve_plan(self.root, episode=ident)
        make_export(plan)
        return os.path.join(self.root, ident)

    def test_populated_folder_refused_by_default(self):  # test 3
        folder = self._populate("e07")
        before = open(os.path.join(folder, "transcript.txt"),
                      encoding="utf-8").read()
        with self.assertRaises(agent_export.AgentExportError) as ctx:
            agent_export.resolve_plan(self.root, episode="e07")
        self.assertIn("E07 already contains a lecture", str(ctx.exception))
        self.assertIn("--overwrite-agent-export", str(ctx.exception))
        after = open(os.path.join(folder, "transcript.txt"),
                     encoding="utf-8").read()
        self.assertEqual(before, after)

    def test_overwrite_creates_backup(self):  # test 4
        folder = self._populate("e07")
        plan = agent_export.resolve_plan(self.root, episode="e07",
                                         overwrite=True)
        result = make_export(plan, transcript_content="NEW\n" + TRANSCRIPT_CONTENT)
        self.assertTrue(result["backup_dir"])
        backed_up = sorted(os.listdir(result["backup_dir"]))
        self.assertEqual(backed_up,
                         ["metadata.json", "segments.jsonl", "transcript.txt"])
        original = open(os.path.join(result["backup_dir"], "transcript.txt"),
                        encoding="utf-8").read()
        self.assertEqual(original, TRANSCRIPT_CONTENT)
        replaced = open(os.path.join(folder, "transcript.txt"),
                        encoding="utf-8").read()
        self.assertTrue(replaced.startswith("NEW\n"))


class TestAutoNext(TempArchive):
    def test_auto_next_picks_first_empty(self):  # test 5
        plan = agent_export.resolve_plan(self.root, auto_next_episode=True)
        self.assertEqual(plan.ident, "e01")

    def test_auto_next_skips_populated(self):  # test 6
        for ident in ("e01", "e02"):
            make_export(agent_export.resolve_plan(self.root, episode=ident))
        # A metadata-only template must NOT count as populated.
        os.makedirs(os.path.join(self.root, "e03"), exist_ok=True)
        with open(os.path.join(self.root, "e03", "metadata.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"lecture_title": "template"}, fh)
        plan = agent_export.resolve_plan(self.root, auto_next_episode=True)
        self.assertEqual(plan.ident, "e03")

    def test_auto_next_extra(self):  # test 7
        make_export(agent_export.resolve_plan(self.root, extra="x001"))
        plan = agent_export.resolve_plan(self.root, auto_next_extra=True)
        self.assertEqual(plan.ident, "x002")

    def test_conflicting_destinations_rejected(self):
        with self.assertRaises(agent_export.AgentExportError):
            agent_export.resolve_plan(self.root, episode="e01", extra="x001")
        with self.assertRaises(agent_export.AgentExportError):
            agent_export.resolve_plan(self.root)


class TestMetadata(TempArchive):
    def test_ids_match_destination_and_schema_valid(self):  # tests 10+11
        plan = agent_export.resolve_plan(self.root, episode="e05")
        result = make_export(plan)
        meta = result["metadata"]
        self.assertEqual(meta["episode"], 5)
        self.assertEqual(meta["episode_id"], "e05")
        self.assertEqual(meta["lecture_id"], "episode-e05")
        self.assertEqual(agent_export.validate_metadata(meta, "episode", "e05"),
                         [])
        # Deterministic extraction: known ticker; date from filename;
        # repeated person/country >= 2 mentions.
        self.assertIn("NVDA", meta["tickers"])
        self.assertEqual(meta["lecture_date"], "2026-07-09")
        self.assertIn("Donald Trump", meta["people"])
        self.assertIn("China", meta["countries"])
        self.assertIn("tariffs", meta["macro_topics"])
        self.assertEqual(meta["status"], "ready")
        self.assertGreater(meta["duration_seconds"], 0)

    def test_explicit_values_override_inferred(self):  # test 12
        plan = agent_export.resolve_plan(self.root, episode="e01")
        result = make_export(plan, cli_overrides={
            "lecture_title": "American Interest, Trump and the MOU",
            "lecture_date": "2026-01-01",
            "speaker": "אורח",
        })
        meta = result["metadata"]
        self.assertEqual(meta["lecture_title"],
                         "American Interest, Trump and the MOU")
        self.assertEqual(meta["lecture_date"], "2026-01-01")  # beats filename
        self.assertEqual(meta["speaker"], "אורח")

    def test_existing_customized_metadata_preserved(self):  # test 13
        folder = os.path.join(self.root, "e02")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "metadata.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"lecture_title": "הכותרת שלי",
                       "notes": "הערות חשובות",
                       "custom_field": "keep me",
                       "episode_id": "WRONG",     # must be corrected
                       "lecture_id": "also-wrong"}, fh, ensure_ascii=False)
        plan = agent_export.resolve_plan(self.root, episode="e02")
        meta = make_export(plan)["metadata"]
        self.assertEqual(meta["lecture_title"], "הכותרת שלי")  # preserved
        self.assertEqual(meta["notes"], "הערות חשובות")
        self.assertEqual(meta["custom_field"], "keep me")      # unknown kept
        self.assertEqual(meta["episode_id"], "e02")            # identity fixed
        self.assertEqual(meta["lecture_id"], "episode-e02")

    def test_enrichment_failure_falls_back(self):  # test 14
        # claude mode without a key: transcript/segments still export,
        # metadata is deterministic with status metadata_incomplete.
        old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            plan = agent_export.resolve_plan(self.root, episode="e03")
            result = make_export(plan, metadata_mode="claude")
            self.assertEqual(result["status"], "metadata_incomplete")
            self.assertFalse(result["ai_enriched"])
            folder = os.path.join(self.root, "e03")
            self.assertTrue(os.path.getsize(
                os.path.join(folder, "transcript.txt")) > 0)
            self.assertTrue(os.path.getsize(
                os.path.join(folder, "segments.jsonl")) > 0)
        finally:
            if old_key:
                os.environ["ANTHROPIC_API_KEY"] = old_key

    def test_metadata_lists_normalized(self):
        meta = agent_export.build_deterministic_metadata(
            "episode", "e01", "l.mp3", 10, "", "m", "accurate")
        meta["topics"] = ["Trump", "trump", " ", "Trump", "China"]
        agent_export.validate_metadata(meta, "episode", "e01")
        self.assertEqual(meta["topics"], ["Trump", "China"])

    def test_validation_catches_problems(self):
        meta = agent_export.build_deterministic_metadata(
            "episode", "e01", "l.mp3", 10, "", "m", "accurate")
        meta["lecture_date"] = "not-a-date"
        meta["status"] = "bogus"
        meta["source_type"] = "movie"
        problems = agent_export.validate_metadata(meta, "episode", "e01")
        self.assertEqual(len(problems), 3)

    def test_sample_export_marked(self):  # test 16
        plan = agent_export.resolve_plan(self.root, episode="e04")
        result = make_export(plan, sample_info={"start_minute": 30,
                                                "duration_minutes": 5})
        meta = result["metadata"]
        self.assertEqual(meta["status"], "sample")
        self.assertEqual(meta["sample"], {"start_minute": 30,
                                          "duration_minutes": 5})


class TestTickerExtraction(unittest.TestCase):
    def test_conservative_tickers(self):
        text = "קניתי NVDA וגם SPY אבל BLAH ו-USA הם לא טיקרים וגם OK לא"
        self.assertEqual(agent_export.extract_tickers(text), ["NVDA", "SPY"])

    def test_date_from_filename(self):
        cases = [("הרצאה 2026-07-09.mp3", "2026-07-09"),
                 ("lecture_09.07.2026.mp4", "2026-07-09"),
                 ("שוק ההון 20260709 חלק ב.mp3", "2026-07-09"),
                 ("sicha.mp3", None)]
        for name, expected in cases:
            self.assertEqual(agent_export.extract_date_from_filename(name),
                             expected, name)


class TestCLIIntegration(TempArchive):
    """Real transcribe.py main() runs (subprocess), using the engine's
    rebuild-from-segments path so no Whisper model is needed."""

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

    def _make_media(self, folder):
        video = os.path.join(folder, "הרצאה מבחן 2026-07-09.mp4")
        subprocess.run([shutil.which("ffmpeg"), "-y", "-v", "error",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                        "-f", "lavfi", "-i", "color=c=black:s=64x64:d=6",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest",
                        video], check=True)
        segments = os.path.splitext(video)[0] + "_segments.jsonl"
        records = [{"start": i * 0.1, "end": i * 0.1 + 0.1,
                    "text": f"משפט {i} על NVDA וטראמפ טראמפ בסין סין",
                    "avg_logprob": -0.3, "no_speech_prob": 0.0}
                   for i in range(60)]
        with open(segments, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return video

    def _run_cli(self, *extra_args):
        video = getattr(self, "_video", None) or self._make_media(self.base)
        self._video = video
        cmd = [sys.executable, os.path.join(HERE, "transcribe.py"), video,
               "--resume", "--metadata-mode", "basic",
               "--agent-export-root", self.root] + list(extra_args)
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8"), video

    def test_end_to_end_episode_export(self):  # required integration test
        if not self.ffmpeg:
            self.skipTest("ffmpeg not available")
        result, video = self._run_cli("--episode", "e01",
                                      "--lecture-title", "בדיקת אינטגרציה")
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        folder = os.path.join(self.root, "e01")
        transcript = open(os.path.join(folder, "transcript.txt"),
                          encoding="utf-8").read()
        self.assertTrue(transcript.strip())
        self.assertIn("NVDA", transcript)
        lines = open(os.path.join(folder, "segments.jsonl"),
                     encoding="utf-8").read().strip().splitlines()
        self.assertEqual(len(lines), 60)
        for line in lines:
            rec = json.loads(line)
            self.assertIn("start", rec)
            self.assertIn("end", rec)
            self.assertIn("text", rec)
        meta = json.load(open(os.path.join(folder, "metadata.json"),
                              encoding="utf-8"))
        self.assertEqual(meta["episode_id"], "e01")
        self.assertEqual(meta["lecture_id"], "episode-e01")
        self.assertEqual(meta["status"], "ready")
        self.assertEqual(meta["lecture_title"], "בדיקת אינטגרציה")
        self.assertEqual(meta["lecture_date"], "2026-07-09")  # from filename
        self.assertGreater(meta["duration_seconds"], 0)

    def test_sample_export_blocked_by_default(self):  # test 15
        if not self.ffmpeg:
            self.skipTest("ffmpeg not available")
        video = self._make_media(self.base)
        cmd = [sys.executable, os.path.join(HERE, "transcribe.py"), video,
               "--sample-minutes", "1", "--agent-export-root", self.root,
               "--episode", "e09"]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not exported as a complete Kikar episode",
                      result.stderr)
        self.assertIn("--allow-sample-agent-export", result.stderr)
        self.assertFalse(agent_export.folder_is_populated(
            os.path.join(self.root, "e09")))

    def test_resume_keeps_destination_and_refuses_repeat(self):  # test 21
        if not self.ffmpeg:
            self.skipTest("ffmpeg not available")
        result, _ = self._run_cli("--episode", "e02")
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        meta1 = json.load(open(os.path.join(self.root, "e02",
                                            "metadata.json"), encoding="utf-8"))
        # Re-running against the now-populated episode must refuse (no
        # silent duplicate/overwrite), keeping the same destination intact.
        result2, _ = self._run_cli("--episode", "e02")
        self.assertNotEqual(result2.returncode, 0)
        self.assertIn("E02 already contains a lecture", result2.stderr)
        meta2 = json.load(open(os.path.join(self.root, "e02",
                                            "metadata.json"), encoding="utf-8"))
        self.assertEqual(meta1["lecture_id"], meta2["lecture_id"])

    def test_normal_transcription_untouched_by_agent_flags(self):  # test 23
        if not self.ffmpeg:
            self.skipTest("ffmpeg not available")
        video = self._make_media(self.base)
        cmd = [sys.executable, os.path.join(HERE, "transcribe.py"), video,
               "--resume"]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        # No agent archive folders were created anywhere near the video.
        self.assertFalse(os.path.exists(os.path.join(self.root, "e01")))
        self.assertTrue(os.path.exists(
            os.path.splitext(video)[0] + "_transcript.txt"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
