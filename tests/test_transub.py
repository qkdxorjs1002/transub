import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, wait as wait_for_futures
from pathlib import Path
from unittest.mock import Mock, patch

from transub import main, parser
from transub_core.batch import discover_videos, process_input
from transub_core import runtime
from transub_core.codex import CodexError, CodexRunner, DISABLED_FEATURES, clean_environment
from transub_core.languages import get_language
from transub_core.media import (MediaError, language, probe, run, select_track, mux, verify_streams, av_hash, extract,
                                verify_interleaving, check_packet_delivery)
from transub_core.pipeline import (ExistingKoreanError, ExistingTargetLanguageError, digest, process,
                                   translation_prompt, validate_translations, research_prompt, guide_schema,
                                   job_directory_name, translation_schema, assemble_translations, save_json)
from transub_core.subtitles import AssDocument, SubtitleError, batches, context_for, protect, restore


ASS = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,24,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,COMMENT_001
Dialogue: 0,0:00:00.20,0:00:02.00,Default,SPEAKER_001,0,0,0,,{\i1}SOURCE_001, SOURCE_002.{\i0}\N- SOURCE_003?
Dialogue: 1,0:00:00.50,0:00:01.50,Default,SPEAKER_002,0,0,0,,{\an8\pos(320,20)}SOURCE_004!
Dialogue: 2,0:00:01.00,0:00:02.50,Default,,0,0,0,,{\p1}m 0 0 l 10 10{\p0}
Dialogue: 0,0:00:02.00,0:00:02.80,Default,SPEAKER_001,0,0,0,,SOURCE_005!
"""


def track(index, lang="und", codec="ass", title="", forced=0, default=0):
    return {"index": index, "codec_type": "subtitle", "codec_name": codec,
            "tags": {"language": lang, "title": title}, "disposition": {"forced": forced, "default": default}}


class SubtitleTests(unittest.TestCase):
    def test_language_priority_and_same_language_full_dialogue(self):
        streams = [track(1, "eng", default=1), track(2, "jpn", forced=1), track(3, "ja-JP"), track(4, "fra")]
        self.assertEqual(select_track(streams)["index"], 3)
        self.assertEqual(select_track(streams[:2])["index"], 2)
        self.assertEqual(select_track([streams[0], streams[3]])["index"], 1)

    def test_bitmap_korean_and_explicit_selection(self):
        streams = [track(1, "jpn", "hdmv_pgs_subtitle"), track(2, "eng"), track(3, "kor")]
        self.assertEqual(select_track(streams)["index"], 2)
        self.assertEqual(select_track(streams, 3)["index"], 3)
        with self.assertRaises(MediaError):
            select_track(streams, 1)
        with self.assertRaises(MediaError):
            select_track([streams[0], streams[2]])
        self.assertEqual(language(track(4, title="Japanese full subtitles")), "ja")

    def test_lossless_roundtrip_and_overlaps(self):
        doc = AssDocument(ASS)
        result = doc.render({c.id: c.protected for c in doc.cues if c.translatable})
        self.assertEqual(result, ASS)
        self.assertEqual(doc.cues[0].overlaps, ["c000001", "c000002"])
        self.assertNotIn("c000003", doc.cues[0].overlaps)  # equal boundary is not overlap
        self.assertFalse(doc.cues[2].translatable)
        self.assertEqual(doc.verify_structure(AssDocument(result))["overlap_pairs"], 4)

    def test_tokens_reject_missing_duplicates_reordering_and_new_markup(self):
        text, tokens = protect(r"{\i1}SOURCE_001{\i0}\NSOURCE_002")
        self.assertEqual(restore(text, "⟦P0⟧번역_001⟦P1⟧⟦P2⟧번역_002", tokens), r"{\i1}번역_001{\i0}\N번역_002")
        for value in ("번역_001", "⟦P0⟧⟦P1⟧⟦P2⟧⟦P2⟧번역_001", "⟦P1⟧⟦P0⟧⟦P2⟧번역_001",
                      "⟦P0⟧⟦P1⟧⟦P2⟧{\\an8}번역_001", "⟦P0⟧⟦P1⟧⟦P2⟧번\n역_001",
                      "⟦P0⟧⟦P1⟧⟦P2⟧번\u2028역_001"):
            with self.subTest(value=value), self.assertRaises(SubtitleError):
                restore(text, value, tokens)

    def test_drawing_injection_rejected_by_final_structure(self):
        doc = AssDocument(ASS.replace(r"{\p0}" + "\n", r"{\p0}SOURCE_001" + "\n"))
        values = {c.id: c.protected for c in doc.cues if c.translatable}
        values["c000002"] = "⟦P0⟧⟦P1⟧INJECTED_001⟦P2⟧번역_001"
        with self.assertRaises(SubtitleError):
            doc.verify_structure(AssDocument(doc.render(values)))

    def test_duplicate_missing_and_unknown_ids(self):
        cue = AssDocument(ASS).cues[3]
        good = {"id": cue.id, "text": "번역_005!"}
        self.assertEqual(validate_translations({"translations": [good]}, [cue]), {cue.id: "번역_005!"})
        for entries in ([], [good, good], [{"id": "other", "text": "번역_005!"}]):
            with self.assertRaises(SubtitleError):
                validate_translations({"translations": entries}, [cue])

    def test_cross_batch_context_includes_simultaneous_speaker(self):
        doc = AssDocument(ASS)
        chunk = batches(doc.cues, 1)[1]
        neighbours = context_for(doc.cues, chunk, radius=0)
        self.assertIn("c000000", {c.id for c in neighbours})
        before = translation_prompt({}, chunk, neighbours, {"c000000": "번역_001"})
        after = translation_prompt({}, chunk, neighbours, {"c000000": "번역_002"})
        self.assertNotEqual(before, after)

    def test_font_changes_only_style(self):
        doc = AssDocument(ASS)
        output = doc.render({c.id: c.protected for c in doc.cues if c.translatable}, font="Noto Sans CJK KR")
        self.assertIn("Default,Noto Sans CJK KR,24", output)
        doc.verify_structure(AssDocument(output))

    def test_span_translation_preserves_style_and_line_breaks(self):
        original = r"SOURCE_101 \N{\i1}SOURCE_102{\i0} SOURCE_103"
        doc = AssDocument(ASS.replace(r"{\i1}SOURCE_001, SOURCE_002.{\i0}\N- SOURCE_003?", original))
        cue = doc.cues[0]
        result = assemble_translations({"translations": {cue.id: {
            "s3": " 번역_103", "s2": "번역_102", "s0": "번역_101"}}}, [cue])
        translated = validate_translations(result, [cue])[cue.id]
        self.assertEqual(restore(cue.protected, translated, cue.tokens),
                         r"번역_101\N{\i1}번역_102{\i0} 번역_103")
        layout = cue.translation_payload()["layout"]
        self.assertEqual([p["fixed"] for p in layout if "fixed" in p],
                         ["line_break", "format_change", "format_change"])
        self.assertNotIn("⟦P", json.dumps(cue.translation_payload()))

    def test_span_schema_requires_every_cue_and_every_text_gap(self):
        cues = batches(AssDocument(ASS).cues, 60)[0]
        schema = translation_schema(cues)["properties"]["translations"]
        self.assertEqual(schema["required"], [c.id for c in cues])
        self.assertFalse(schema["additionalProperties"])
        for cue in cues:
            event = schema["properties"][cue.id]
            self.assertEqual(event["required"], list(cue.text_spans()))
            self.assertFalse(event["additionalProperties"])

    def test_span_drawings_and_blank_gaps_cannot_be_written(self):
        original = r"{\p1}m 0 0 l 10 10{\p0} \NSOURCE_001{\i1}{\i0} SOURCE_002\h!"
        doc = AssDocument(ASS.replace(r"{\i1}SOURCE_001, SOURCE_002.{\i0}\N- SOURCE_003?", original))
        cue = doc.cues[0]
        self.assertEqual(cue.text_spans(), {"s4": "SOURCE_001", "s6": " SOURCE_002", "s7": "!"})
        result = cue.assemble_spans({"s4": "번역_001", "s6": " 번역_002", "s7": "!"})
        self.assertEqual(restore(cue.protected, result, cue.tokens),
                         r"{\p1}m 0 0 l 10 10{\p0} \N번역_001{\i1}{\i0} 번역_002\h!")
        translations = {c.id: c.protected for c in doc.cues if c.translatable}
        translations[cue.id] = result
        doc.verify_structure(AssDocument(doc.render(translations)))

    def test_span_output_rejects_missing_extra_blank_and_injected_controls(self):
        cue = AssDocument(ASS).cues[0]
        good = {key: "번역_003" for key in cue.text_spans()}
        bad = [None, {}, {**good, "s99": "EXTRA_001"}]
        key = next(iter(good))
        for value in ("", " ", None, "⟦P0⟧번역_003", r"번역_003\N번역_003", r"{\p1}번역_003", "번역_003\n", "번역_003\u2028"):
            bad.append({**good, key: value})
        for values in bad:
            with self.subTest(values=values), self.assertRaisesRegex(SubtitleError, cue.id):
                cue.assemble_spans(values)
        for values in ([], {}, {cue.id: good, "c999999": good}):
            with self.assertRaises(SubtitleError):
                assemble_translations({"translations": values}, [cue])


class TargetPromptTests(unittest.TestCase):
    def test_korean_prompt_bytes_retain_legacy_checkpoint_keys(self):
        doc = AssDocument(ASS)
        chunk = batches(doc.cues, 1)[1]
        neighbours = context_for(doc.cues, chunk, radius=0)
        # Captured from the Korean-only implementation before target-language support.
        research_hash = "846ea02a68729d4d1a38a210bac284d8b86ec8efc2425570866b5f94a259b1c4"
        translation_hash = "b7b938f42a3294d82d902c4cee4158a16d4ea7c373ba689a578e562f208e1cd5"
        for target_args in ((), ("ko",), ("kor",)):
            with self.subTest(target=target_args):
                research = research_prompt("SOURCE_001.mkv", None, track(0, "eng"), doc, *target_args)
                translation = translation_prompt({}, chunk, neighbours, {"c000000": "번역_001"}, *target_args)
                self.assertEqual(hashlib.sha256(research.encode()).hexdigest(), research_hash)
                self.assertEqual(hashlib.sha256(translation.encode()).hexdigest(), translation_hash)

    def test_research_schema_and_translation_context_follow_target(self):
        doc = AssDocument(ASS)
        chunk = batches(doc.cues, 1)[1]
        neighbours = context_for(doc.cues, chunk, radius=0)
        for code, name in (("fr", "French"), ("ja", "Japanese")):
            with self.subTest(target=code):
                schema = guide_schema(code)["properties"]
                self.assertEqual(schema["glossary"]["items"]["required"], ["source", "target", "note"])
                self.assertIn("target_name", schema["characters"]["items"]["required"])
                self.assertNotIn("korean_name", schema["characters"]["items"]["required"])
                research = research_prompt("SOURCE_001.mkv", None, track(0, "eng"), doc, code)
                self.assertIn(f"guide in {name}", research)
                self.assertNotIn("Korean", research)
                self.assertEqual(json.loads(research.split("INPUT_DATA:\n")[1])["target_language"], code)
                prompt = translation_prompt({}, chunk, neighbours, {"c000000": "TARGET_001"}, code)
                self.assertIn(f"concise {name} subtitles", prompt)
                self.assertNotIn("previous_korean", prompt)
                data = json.loads(prompt.split("INPUT_DATA:\n")[1])
                self.assertEqual(data["target_language"], code)
                previous = next(c for c in data["context_only"] if c["id"] == "c000000")
                self.assertEqual(previous["previous_translation"], "TARGET_001")
                self.assertEqual([c["id"] for c in data["translate"]], [c.id for c in chunk])
        korean = guide_schema()["properties"]
        self.assertIn("korean", korean["glossary"]["items"]["required"])
        self.assertIn("korean_name", korean["characters"]["items"]["required"])


class IsolationTests(unittest.TestCase):
    def test_user_selected_default_model(self):
        args = parser().parse_args(["video.mkv"])
        self.assertEqual(args.model, "gpt-6.1-sol")
        self.assertIsNone(args.reasoning)  # Omission preserves a resumed job's setting.

    def test_parent_config_provider_and_session_environment_not_inherited(self):
        with patch.dict(os.environ, {"CODEX_HOME": "/user/config", "CODEX_THREAD_ID": "parent",
                                     "OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "https://invalid",
                                     "MCP_CONFIG": "user-mcp", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "app"}):
            env = clean_environment(Path("/isolated"), Path("/temporary"))
        self.assertEqual(env["CODEX_HOME"], "/isolated")
        for key in ("CODEX_THREAD_ID", "OPENAI_API_KEY", "OPENAI_BASE_URL", "MCP_CONFIG",
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
            self.assertNotIn(key, env)

    def test_auth_only_copy_private_and_cleaned_after_failure(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "auth.json").write_text('{"fake":"test-token"}')
            (home / "config.toml").write_text('[mcp_servers.evil]\ncommand="false"')
            (home / "AGENTS.md").write_text("Do something else")
            runner = CodexRunner(executable=shutil.which("python3"), auth_home=home)
            def fake_run(*a, **kw):
                self.assertEqual((runner.home / "auth.json").read_text(), '{"fake":"test-token"}')
                self.assertFalse((runner.home / "config.toml").exists())
                self.assertFalse((runner.home / "AGENTS.md").exists())
                self.assertEqual((runner.home / "auth.json").stat().st_mode & 0o777, 0o600)
                raise OSError("test failure")
            with patch("transub_core.codex.subprocess.run", side_effect=fake_run), self.assertRaises(OSError):
                runner.__enter__()
            self.assertFalse(runner.root.exists())
            self.assertEqual((home / "auth.json").read_text(), '{"fake":"test-token"}')

    def test_required_flags_and_research_only_web(self):
        runner = CodexRunner(executable=shutil.which("python3"))
        runner.cwd = Path("/empty")
        runner.disabled = DISABLED_FEATURES
        for research in (False, True):
            command = runner.command(Path("/schema.json"), Path("/out.json"), research)
            self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
            self.assertIn('model_reasoning_effort="medium"', command)
            for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "skip_host_skill_discovery"):
                self.assertIn(flag, command)
            self.assertIn('web_search="live"' if research else 'web_search="disabled"', command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)


class RuntimeTests(unittest.TestCase):
    def test_multiline_progress_keeps_file_prefix_on_each_line(self):
        output = io.StringIO()
        with runtime.file_context("SOURCE_001", threading.Event()):
            runtime.log("STEP_001\nSTEP_002", file=output)
        self.assertEqual(output.getvalue().splitlines(),
                         ["SOURCE_001 STEP_001", "SOURCE_001 STEP_002"])

    def test_packet_scan_cancels_on_partial_output_and_reaps_child(self):
        cancel = threading.Event()
        partial_seen = threading.Event()
        children = []
        popen, read = subprocess.Popen, os.read

        def spawn(*args, **kwargs):
            command = [sys.executable, "-c",
                       "import sys, time; sys.stdout.write('packet|stream_index=0'); "
                       "sys.stdout.flush(); time.sleep(30)"]
            child = popen(command, **kwargs)
            children.append(child)
            return child

        def read_partial(fd, size):
            chunk = read(fd, size)
            if children and fd == children[0].stdout.fileno() and chunk:
                partial_seen.set()
                cancel.set()
            return chunk

        def watchdog():
            cancel.set()
            for child in children:
                if child.poll() is None:
                    child.kill()

        timer = threading.Timer(5, watchdog)
        timer.start()
        try:
            with runtime.file_context("SOURCE_001", cancel), \
                    patch("transub_core.media.subprocess.Popen", side_effect=spawn), \
                    patch("transub_core.runtime.os.read", side_effect=read_partial), \
                    self.assertRaises(KeyboardInterrupt):
                verify_interleaving(Path("SOURCE_001.mkv"),
                                    {"streams": [{"index": 0, "codec_type": "video"}]}, 1, 1)
            self.assertTrue(cancel.is_set())
            self.assertTrue(partial_seen.is_set())
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
        finally:
            timer.cancel()
            timer.join()
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()

    def test_context_restores_log_prefix_and_cancellation_after_error(self):
        output = io.StringIO()
        outer, inner = threading.Event(), threading.Event()
        with runtime.file_context("SOURCE_001", outer):
            runtime.log("STEP_001", file=output)
            with self.assertRaises(KeyboardInterrupt):
                with runtime.file_context("SOURCE_002", inner):
                    runtime.log("STEP_002", file=output)
                    inner.set()
                    runtime.check_cancelled()
            runtime.check_cancelled()
            runtime.log("STEP_003", file=output)
        outer.set()
        runtime.check_cancelled()
        runtime.log("STEP_004", file=output)
        self.assertEqual(output.getvalue().splitlines(),
                         ["SOURCE_001 STEP_001", "SOURCE_002 STEP_002",
                          "SOURCE_001 STEP_003", "STEP_004"])

    def test_communicate_retries_without_resending_stdin(self):
        child = Mock(args=["SOURCE_001"])
        child.communicate.side_effect = [subprocess.TimeoutExpired(child.args, 0.2),
                                         ("OUTPUT_001", "")]
        with runtime.file_context("SOURCE_001", threading.Event()):
            result = runtime.communicate(child, "INPUT_001", timeout=30)
        self.assertEqual(result, ("OUTPUT_001", ""))
        self.assertEqual([call.args for call in child.communicate.call_args_list],
                         [("INPUT_001",), (None,)])
        self.assertTrue(all(0 < call.kwargs["timeout"] <= 0.2
                            for call in child.communicate.call_args_list))

    def test_cancelled_context_prevents_child_start(self):
        cancel = threading.Event()
        with runtime.file_context("SOURCE_001", cancel), \
                patch("transub_core.runtime.subprocess.Popen") as spawn:
            cancel.set()
            with self.assertRaises(KeyboardInterrupt):
                runtime.run_captured([sys.executable, "-c", "pass"], timeout=30)
        spawn.assert_not_called()

    def test_cancellation_reaps_media_and_codex_children(self):
        # Real local Python children exercise pipe polling and process cleanup;
        # no Codex executable, credentials, or network are involved.
        popen = subprocess.Popen
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        for operation in ("media", "codex"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                cancel = threading.Event()
                children = []
                sessions = []

                def spawn(*args, **kwargs):
                    sessions.append(kwargs.get("start_new_session"))
                    child = popen(*args, **kwargs)
                    children.append(child)
                    communicate = child.communicate

                    def cancel_after_poll(*args, **kwargs):
                        try:
                            return communicate(*args, **kwargs)
                        except subprocess.TimeoutExpired:
                            cancel.set()
                            raise

                    child.communicate = cancel_after_poll
                    return child

                runner = CodexRunner(executable=sys.executable, timeout=30)
                runner.root = runner.cwd = Path(directory)
                runner.env = dict(os.environ)
                try:
                    with runtime.file_context("SOURCE_001", cancel), \
                            patch("transub_core.runtime.subprocess.Popen", side_effect=spawn), \
                            patch.object(runner, "command", return_value=command), \
                            self.assertRaises(KeyboardInterrupt):
                        if operation == "media":
                            run(command, timeout=30)
                        else:
                            runner.call("INPUT_001", {}, "REQUEST_001")
                    self.assertTrue(cancel.is_set())
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].poll())
                    self.assertEqual(sessions, [os.name != "nt"])
                    self.assertEqual(list(Path(directory).iterdir()), [])
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                        child.wait()

    def test_timeout_reaps_captured_child(self):
        children = []
        popen = subprocess.Popen

        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        try:
            with runtime.file_context("SOURCE_001", threading.Event()), \
                    patch("transub_core.runtime.subprocess.Popen", side_effect=spawn), \
                    self.assertRaises(subprocess.TimeoutExpired) as error:
                runtime.run_captured([sys.executable, "-c", "import time; time.sleep(30)"],
                                     timeout=0.01, text=True)
            self.assertEqual(error.exception.timeout, 0.01)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()


class InterleaveTests(unittest.TestCase):
    def test_content_and_pts_can_be_valid_while_delivery_is_late(self):
        lines = ["packet|stream_index=0|pts_time=0.709|dts_time=0.709",
                 "packet|stream_index=3|pts_time=0.740|dts_time=0.740",
                 "packet|stream_index=0|pts_time=1410.077|dts_time=1410.077",
                 "packet|stream_index=3|pts_time=3.900|dts_time=3.900"]
        with self.assertRaisesRegex(MediaError, "배치 오류"):
            check_packet_delivery(lines, {0, 1}, 3, 2)

    def test_sparse_cues_and_overlaps_delivered_in_time(self):
        lines = ["packet|stream_index=0|pts_time=0.709|dts_time=0.709",
                 "packet|stream_index=3|pts_time=0.740|dts_time=0.740",
                 "packet|stream_index=0|pts_time=413.400|dts_time=413.400",
                 "packet|stream_index=3|pts_time=413.400|dts_time=413.400",
                 "packet|stream_index=3|pts_time=413.400|dts_time=413.400"]
        self.assertEqual(check_packet_delivery(lines, {0, 1}, 3, 3)["subtitle_packets"], 3)
        with self.assertRaisesRegex(MediaError, "수 불일치"):
            check_packet_delivery(lines, {0, 1}, 3, 4)


class HevcStreamVerificationTests(unittest.TestCase):
    @staticmethod
    def configuration(completeness=(1, 1, 1, 1)):
        # Synthetic header containing the fields used by the structural verifier.
        data = bytearray(23)
        data[0] = 1  # configurationVersion
        data[22] = 4  # numOfArrays: VPS, SPS, PPS, SEI
        offsets = []
        for nal_type, complete, payloads in zip(
                (32, 33, 34, 39), completeness,
                ((b"vps-a", b"vps-b"), (b"sps",), (b"pps-a", b"pps-b"), (b"sei",))):
            offsets.append(len(data))
            data.append((complete << 7) | nal_type)
            data.extend(len(payloads).to_bytes(2, "big"))
            for payload in payloads:
                nal = bytes((nal_type << 1, 1)) + payload
                data.extend(len(nal).to_bytes(2, "big"))
                data.extend(nal)
        return data, offsets

    @staticmethod
    def stream(raw):
        lines = []
        for address in range(0, len(raw), 16):
            chunk = raw[address:address + 16]
            hex_column = " ".join(chunk[i:i + 2].hex() for i in range(0, len(chunk), 2))
            ascii_column = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
            lines.append(f"{address:08x}: {hex_column:<39}  {ascii_column}")
        return {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920,
                "height": 1080, "extradata_size": len(raw),
                "extradata_hash": "SHA256:" + hashlib.sha256(raw).hexdigest(),
                "extradata": "\n" + "\n".join(lines) + "\n"}

    def media_pair(self, before_raw=None, after_raw=None):
        if before_raw is None:
            before_raw, _ = self.configuration()
        if after_raw is None:
            after_raw, _ = self.configuration((0, 0, 0, 0))
        before = {"streams": [self.stream(before_raw)], "format": {"format_name": "matroska,webm"}}
        after = {"streams": [self.stream(after_raw), track(1, "kor")],
                 "format": {"format_name": "matroska,webm"}}
        return before, after

    def test_matroska_accepts_only_completeness_clearing_across_arrays_and_nals(self):
        for completeness in ((1, 1, 1, 1), (1, 0, 1, 0)):
            with self.subTest(completeness=completeness):
                source, _ = self.configuration(completeness)
                before, after = self.media_pair(before_raw=source)
                self.assertNotEqual(before["streams"][0]["extradata_hash"],
                                    after["streams"][0]["extradata_hash"])
                self.assertEqual(verify_streams(before, after), {
                    "original_streams": 1, "output_streams": 2,
                    "korean_stream_index": 1, "stream_copy": True})

    def test_reverse_or_mixed_direction_completeness_changes_are_rejected(self):
        for old_bits, new_bits in (((0, 0, 0, 0), (1, 1, 1, 1)),
                                   ((1, 0, 1, 0), (0, 1, 0, 0)),
                                   ((1, 1, 1, 1), (0, 1, 1, 1))):
            with self.subTest(old_bits=old_bits, new_bits=new_bits):
                before, after = self.media_pair(self.configuration(old_bits)[0],
                                                self.configuration(new_bits)[0])
                with self.assertRaises(MediaError):
                    verify_streams(before, after)

    def test_other_configuration_header_or_nal_payload_changes_are_rejected(self):
        output, offsets = self.configuration((0, 0, 0, 0))
        for position in (1, 12, 21, offsets[0] + 7, len(output) - 1):
            with self.subTest(position=position):
                changed = bytearray(output)
                changed[position] ^= 1
                with self.assertRaises(MediaError):
                    verify_streams(*self.media_pair(after_raw=changed))

    def test_invalid_hvcc_structure_cannot_be_normalized(self):
        source, offsets = self.configuration()
        mutations = {
            "unsupported version": lambda data: data.__setitem__(0, 2),
            "array count too small": lambda data: data.__setitem__(22, 3),
            "array count too large": lambda data: data.__setitem__(22, 5),
            "reserved array bit": lambda data: data.__setitem__(23, data[23] | 0x40),
            "NAL count too small": lambda data: data.__setitem__(25, 1),
            "NAL count too large": lambda data: data.__setitem__(25, 3),
            "zero NAL length": lambda data: data.__setitem__(slice(26, 28), b"\0\0"),
            "short NAL header": lambda data: data.__setitem__(slice(26, 28), b"\0\1"),
            "oversized NAL length": lambda data: data.__setitem__(slice(26, 28), b"\xff\xff"),
            "NAL type mismatch": lambda data: data.__setitem__(28, 33 << 1),
            "truncated payload": lambda data: data.__delitem__(-1),
            "truncated array header": lambda data: data.__delitem__(slice(offsets[-1] + 1, None)),
            "truncated NAL length": lambda data: data.__delitem__(slice(offsets[-1] + 4, None)),
            "trailing bytes": lambda data: data.extend(b"\0"),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                old = bytearray(source)
                new, _ = self.configuration((0, 0, 0, 0))
                mutate(old)
                mutate(new)
                # Both sides carry real hashes and equal sizes: malformed data must
                # fail parsing even when the only differing bytes are completeness.
                before, after = self.media_pair(old, new)
                self.assertEqual(before["streams"][0]["extradata_size"],
                                 after["streams"][0]["extradata_size"])
                with self.assertRaises(MediaError):
                    verify_streams(before, after)

    def test_short_or_missing_configuration_is_rejected(self):
        for size in (0, 1, 22):
            with self.subTest(size=size):
                old, _ = self.configuration()
                new = bytearray(old[:size])
                if new:
                    new[-1] ^= 1
                before, after = self.media_pair(old[:size], new)
                # A distinct advertised hash forces verification even for empty data.
                if not new:
                    after["streams"][0]["extradata_hash"] = "SHA256:" + "0" * 64
                with self.assertRaises(MediaError):
                    verify_streams(before, after)

    def test_missing_malformed_or_untrusted_dump_is_rejected_on_either_side(self):
        mutations = {
            "missing dump": lambda stream: stream.pop("extradata"),
            "null dump": lambda stream: stream.update(extradata=None),
            "non-string dump": lambda stream: stream.update(extradata=[]),
            "empty dump": lambda stream: stream.update(extradata=""),
            "missing delimiter": lambda stream: stream.update(extradata="00000000 0101"),
            "invalid hex": lambda stream: stream.update(extradata="00000000: zz"),
            "invalid address": lambda stream: stream.update(extradata="invalid: 0101"),
            "wrong initial address": lambda stream: stream.update(
                extradata=stream["extradata"].replace("00000000:", "00000001:", 1)),
            "noncontiguous address": lambda stream: stream.update(
                extradata=stream["extradata"].replace("00000010:", "00000011:", 1)),
            "oversized row": lambda stream: stream.update(extradata="00000000: " + "01" * 17),
            "empty row": lambda stream: stream.update(extradata="00000000: "),
            "missing hash": lambda stream: stream.pop("extradata_hash"),
            "wrong hash": lambda stream: stream.update(extradata_hash="SHA256:" + "0" * 64),
        }
        for side in (0, 1):
            for label, mutate in mutations.items():
                with self.subTest(side=side, case=label):
                    pair = self.media_pair()
                    mutate(pair[side]["streams"][0])
                    with self.assertRaises(MediaError):
                        verify_streams(*pair)

    def test_equal_but_incorrect_or_missing_declared_sizes_are_rejected(self):
        for size in (None, 0, 999, "90"):
            with self.subTest(size=size):
                before, after = self.media_pair()
                for info in (before, after):
                    if size is None:
                        info["streams"][0].pop("extradata_size")
                    else:
                        info["streams"][0]["extradata_size"] = size
                with self.assertRaises(MediaError):
                    verify_streams(before, after)

    def test_codec_dimensions_and_other_stream_changes_are_not_exempt(self):
        for field, value in (("codec_name", "h264"), ("width", 1280), ("height", 720),
                             ("sample_rate", "48000"), ("channels", 2), ("extradata_size", 999)):
            with self.subTest(field=field):
                before, after = self.media_pair()
                after["streams"][0][field] = value
                with self.assertRaises(MediaError):
                    verify_streams(before, after)

    def test_normalization_requires_hevc_video_and_matroska_output(self):
        for codec_type, codec_name in (("video", "h264"), ("audio", "hevc"),
                                        ("attachment", "hevc")):
            with self.subTest(codec_type=codec_type, codec_name=codec_name):
                before, after = self.media_pair()
                for info in (before, after):
                    info["streams"][0].update(codec_type=codec_type, codec_name=codec_name)
                with self.assertRaises(MediaError):
                    verify_streams(before, after)
        for format_name in (None, "", "mov,mp4,m4a,3gp,3g2,mj2", "webm", "notmatroska"):
            with self.subTest(format_name=format_name):
                before, after = self.media_pair()
                after["format"] = {} if format_name is None else {"format_name": format_name}
                with self.assertRaises(MediaError):
                    verify_streams(before, after)

    def test_unchanged_codec_hashes_still_work_without_raw_dumps(self):
        for codec in ("h264", "ffv1", "hevc"):
            with self.subTest(codec=codec):
                raw, _ = self.configuration()
                before, after = self.media_pair(raw, raw)
                for info in (before, after):
                    info["streams"][0]["codec_name"] = codec
                    info["streams"][0].pop("extradata")
                self.assertTrue(verify_streams(before, after)["stream_copy"])


class HevcProbeTests(unittest.TestCase):
    def test_video_only_dump_query_merges_hevc_by_index_without_attachment_data(self):
        streams = [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 2, "codec_type": "video", "codec_name": "hevc"},
            {"index": 4, "codec_type": "attachment", "codec_name": "ttf"},
            {"index": 7, "codec_type": "video", "codec_name": "hevc"},
        ]
        dumps = {"streams": [{"index": 7, "codec_name": "hevc", "extradata": "hevc-seven"},
                             {"index": 0, "codec_name": "h264", "extradata": "h264-data"},
                             {"index": 2, "codec_name": "hevc", "extradata": "hevc-two"},
                             {"index": 4, "codec_name": "ttf", "extradata": "font-data"}]}
        with patch("transub_core.media.run", side_effect=[json.dumps({"streams": streams}),
                                                         json.dumps(dumps)]) as execute:
            result = probe(Path("episode.mkv"), "custom-ffprobe")
        self.assertEqual(execute.call_count, 2)
        first, second = [call.args[0] for call in execute.call_args_list]
        self.assertNotIn("-show_data", first)
        self.assertEqual(second, ["custom-ffprobe", "-v", "error", "-select_streams", "v",
                                  "-show_entries", "stream=index,codec_name,extradata", "-show_data",
                                  "-of", "json", "episode.mkv"])
        by_index = {stream["index"]: stream for stream in result["streams"]}
        self.assertEqual(by_index[2]["extradata"], "hevc-two")
        self.assertEqual(by_index[7]["extradata"], "hevc-seven")
        self.assertNotIn("extradata", by_index[0])
        self.assertNotIn("extradata", by_index[4])

    def test_non_hevc_video_does_not_request_any_raw_extradata(self):
        info = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"},
                            {"index": 1, "codec_type": "audio", "codec_name": "hevc"},
                            {"index": 2, "codec_type": "attachment", "codec_name": "ttf"}]}
        with patch("transub_core.media.run", return_value=json.dumps(info)) as execute:
            self.assertEqual(probe(Path("episode.mkv")), info)
        execute.assert_called_once()
        self.assertNotIn("-show_data", execute.call_args.args[0])

    def test_missing_or_wrong_codec_dump_does_not_supply_hevc_evidence(self):
        info = {"streams": [{"index": 2, "codec_type": "video", "codec_name": "hevc"}]}
        for dumps in ({}, {"streams": []},
                      {"streams": [{"index": 2, "codec_name": "h264", "extradata": "wrong-codec"}]},
                      {"streams": [{"index": 3, "codec_name": "hevc", "extradata": "wrong-index"}]},
                      {"streams": [{"index": 2, "codec_name": "hevc"}]}):
            with self.subTest(dumps=dumps), patch(
                    "transub_core.media.run", side_effect=[json.dumps(info), json.dumps(dumps)]):
                self.assertIsNone(probe(Path("episode.mkv"))["streams"][0].get("extradata"))


class FakeCodex:
    calls = []
    fail_at = None

    def __init__(self, *a, **kw):
        self.audit = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def call(self, prompt, schema, label, research=False):
        self.calls.append(label)
        if label == self.fail_at:
            raise CodexError("simulated failure")
        data = json.loads(prompt.split("INPUT_DATA:\n", 1)[1])
        target = data.get("target_language", "ko")
        if research:
            return {"work_title": "FIXTURE_TITLE_001", "episode": "1", "identification_confidence": "high",
                    "source_language": "en", "premise": "FIXTURE_PREMISE_001", "glossary": [], "characters": [],
                    "translation_rules": [get_language(target).label], "sources": [{"url": "https://example.com/",
                    "title": "fixture", "supports": "synthetic test"}], "uncertainties": []}
        result = {}
        for cue in data["translate"]:
            result[cue["id"]] = {part["id"]: "번역_001" if target == "ko" else "TARGET_001"
                                 for part in cue["layout"] if "id" in part}
        return {"translations": result}


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        ass = self.folder / "input.ass"
        ass.write_text(ASS)
        self.video = self.folder / "sample.mkv"
        run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5",
             "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-i", str(ass),
             "-map", "0:v", "-map", "1:a", "-map", "2:s", "-t", "3",
             "-c:v", "ffv1", "-c:a", "flac", "-c:s", "ass", "-metadata:s:s:0", "language=eng", str(self.video)])
        self.args = parser().parse_args([str(self.video), "--work-dir", str(self.folder / "work"),
                                         "--batch-size", "1", "--retries", "0"])
        self.original = self.video.read_bytes()
        FakeCodex.calls, FakeCodex.fail_at = [], None
        self.mock = patch("transub_core.pipeline.CodexRunner", FakeCodex)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_real_remux_replaces_original_and_preserves_av_and_overlaps(self):
        report = process(self.args)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["in_place"])
        self.assertTrue(report["av_sha256_match"])
        self.assertEqual(report["events"], 4)
        self.assertEqual(report["target_language"], "ko")
        self.assertEqual(report["translated_subtitle"], report["korean_subtitle"])
        self.assertEqual(report["translated_stream_index"], report["korean_stream_index"])
        self.assertEqual([s["tags"].get("language") for s in probe(self.video)["streams"]
                          if s["codec_type"] == "subtitle"], ["eng", "kor"])
        self.assertEqual(list(self.folder.glob("*.mkv")), [self.video])
        self.assertEqual(list(self.folder.glob(".transub-mux-*")), [])
        with self.assertRaises(MediaError):
            process(self.args)  # duplicate Korean track guard

    def test_failed_hash_verification_never_replaces_original(self):
        with patch("transub_core.pipeline.av_hash", side_effect=["before", "different"]), self.assertRaises(MediaError):
            process(self.args)
        self.assertEqual(self.video.read_bytes(), self.original)
        self.assertEqual(list(self.folder.glob(".transub-mux-*")), [])

    def test_cancellation_after_verification_never_publishes_output(self):
        for separate_output in (False, True):
            with self.subTest(separate_output=separate_output):
                cancel = threading.Event()
                output = self.folder / "TARGET_001.mkv"
                self.args.output = str(output) if separate_output else None

                def cancel_after_verification(path, data):
                    save_json(path, data)
                    if path.name == "verification.json" and data.get("status") == "verified":
                        cancel.set()

                with runtime.file_context("SOURCE_001", cancel), \
                        patch("transub_core.pipeline.save_json", side_effect=cancel_after_verification), \
                        self.assertRaises(KeyboardInterrupt):
                    process(self.args)
                self.assertTrue(cancel.is_set())
                self.assertEqual(self.video.read_bytes(), self.original)
                self.assertFalse(output.exists())
                self.assertEqual(list(self.folder.glob(".transub-mux-*")), [])

    def test_non_korean_real_remux_metadata_dispositions_ass_and_av(self):
        before = probe(self.video)
        original_hash = av_hash(self.video)
        source_doc = AssDocument((self.folder / "input.ass").read_text())
        for code, default_flag in (("fr", "--default-subtitle"), ("ja", "--default-korean")):
            with self.subTest(target=code):
                output = self.folder / f"TARGET_001.{code}.mkv"
                args = parser().parse_args([str(self.video), "--target-language", code,
                    "--output", str(output), "--work-dir", str(self.folder / f"work-{code}"), default_flag])
                report = process(args)
                self.assertEqual(report["target_language"], code)
                self.assertNotIn("korean_subtitle", report)
                self.assertNotIn("korean_stream_index", report)
                self.assertEqual(Path(report["translated_subtitle"]).name, f"translated.{code}.ass")
                after = probe(output)
                verification = verify_streams(before, after, target_language=code)
                self.assertEqual(report["translated_stream_index"], verification["translated_stream_index"])
                subtitles = [s for s in after["streams"] if s["codec_type"] == "subtitle"]
                target = subtitles[-1]
                self.assertEqual(target["tags"]["language"], get_language(code).iso639_2)
                self.assertEqual(target["tags"]["title"], get_language(code).track_title)
                self.assertEqual([s["disposition"]["default"] for s in subtitles], [0, 1])
                self.assertEqual(av_hash(output), original_hash)
                self.assertTrue(report["av_sha256_match"])
                self.assertTrue(report["subtitle_delivery_verified"])
                extracted = self.folder / f"TARGET_001.{code}.ass"
                extract(output, target, extracted)
                actual = AssDocument(extracted.read_text(encoding="utf-8-sig"))
                self.assertEqual(source_doc.verify_structure(actual)["events"], 4)
                self.assertTrue(all("TARGET_001" in cue.text for cue in actual.cues if cue.translatable))
                with self.assertRaises(MediaError):
                    verify_streams(before, after, target_language="de")
        self.assertEqual(self.video.read_bytes(), self.original)

    def test_target_duplicate_guard_and_both_override_flags(self):
        self.assertIs(ExistingKoreanError, ExistingTargetLanguageError)
        self.args.target_language = "fr"
        process(self.args)
        translated_bytes = self.video.read_bytes()
        FakeCodex.calls = []
        with self.assertRaises(ExistingTargetLanguageError):
            process(self.args)
        self.assertEqual(FakeCodex.calls, [])
        self.assertEqual(self.video.read_bytes(), translated_bytes)
        for index, flag in enumerate(("--allow-existing-target", "--allow-existing-korean")):
            with self.subTest(flag=flag):
                output = self.folder / f"TARGET_00{index + 1}.mkv"
                args = parser().parse_args([str(self.video), "--target-language", "fra", flag,
                    "--output", str(output), "--work-dir", str(self.folder / f"override-{index}")])
                report = process(args)
                self.assertEqual(report["source_language"], "en")
                subtitles = [s for s in probe(output)["streams"] if s["codec_type"] == "subtitle"]
                self.assertEqual([language(s) for s in subtitles], ["en", "fr", "fr"])
                self.assertEqual(subtitles[-1]["disposition"]["default"], 0)

    def test_default_workdirs_are_target_scoped_and_keep_korean_legacy_path(self):
        self.args.work_dir = None
        FakeCodex.fail_at = "research"
        legacy_name = self.video.stem + "-" + digest(str(self.video.resolve()))[:10]
        for code in ("ko", "fr", "ja"):
            with self.subTest(target=code):
                self.args.target_language = code
                with patch("transub_core.pipeline.Path.cwd", return_value=self.folder), self.assertRaises(CodexError):
                    process(self.args)
                expected_name = legacy_name + ("" if code == "ko" else f"-{code}")
                manifest = json.loads((self.folder / ".transub" / expected_name / "manifest.json").read_text())
                self.assertEqual(manifest["target_language"], code)
        self.assertEqual(len(list((self.folder / ".transub").iterdir())), 3)

    def test_explicit_workdir_rejects_target_switch_before_touching_cache(self):
        self.args.target_language = "fr"
        FakeCodex.fail_at = "batch-0002-try-1"
        with self.assertRaises(CodexError):
            process(self.args)
        work = Path(self.args.work_dir)
        saved = {path: path.read_bytes() for path in work.iterdir() if path.is_file()}
        self.args.target_language = "ja"
        with patch("transub_core.pipeline.CodexRunner") as runner:
            with self.assertRaisesRegex(MediaError, "원본/설정이 다릅니다"):
                process(self.args)
        runner.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in work.iterdir() if path.is_file()}, saved)
        self.assertEqual(self.video.read_bytes(), self.original)

    def test_recursive_non_korean_folder_rerun_skips_without_codex(self):
        nested = self.folder / "nested" / "SOURCE_002.mkv"
        nested.parent.mkdir()
        nested.write_bytes(self.original)
        args = parser().parse_args([str(self.folder), "--target-language", "fr",
                                   "--work-dir", str(self.folder / "work")])
        first = process_input(args)
        self.assertEqual((first["completed"], first["skipped"], first["failed"]), (2, 0, 0))
        saved = {path: path.read_bytes() for path in (self.video, nested)}
        FakeCodex.calls = []
        second = process_input(args)
        self.assertEqual((second["completed"], second["skipped"], second["failed"]), (0, 2, 0))
        self.assertEqual(FakeCodex.calls, [])
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)
        for path in saved:
            work = self.folder / "work" / job_directory_name(path.resolve(), "fr")
            self.assertEqual(json.loads((work / "manifest.json").read_text())["target_language"], "fr")

    def test_resume_only_missing_batches(self):
        FakeCodex.fail_at = "batch-0002-try-1"
        with self.assertRaises(CodexError):
            process(self.args)
        self.assertEqual(self.video.read_bytes(), self.original)
        FakeCodex.calls, FakeCodex.fail_at = [], None
        process(self.args)
        self.assertNotIn("research", FakeCodex.calls)
        self.assertNotIn("batch-0001-try-1", FakeCodex.calls)
        self.assertEqual(len(FakeCodex.calls), 2)

    def test_new_job_default_and_explicit_reasoning_reach_runner_and_manifest(self):
        for requested, expected in ((None, "medium"), ("low", "low"), ("high", "high")):
            with self.subTest(reasoning=requested):
                self.args.reasoning = requested
                self.args.work_dir = str(self.folder / f"work-{expected}")
                FakeCodex.fail_at = "batch-0002-try-1"
                with patch("transub_core.pipeline.CodexRunner", wraps=FakeCodex) as runner:
                    with self.assertRaises(CodexError):
                        process(self.args)
                self.assertEqual(runner.call_args.args[1:3], ("gpt-6.1-sol", expected))
                manifest = json.loads((Path(self.args.work_dir) / "manifest.json").read_text())
                self.assertEqual((manifest["model"], manifest["reasoning"]), ("gpt-6.1-sol", expected))
                self.assertEqual(self.video.read_bytes(), self.original)

    def test_legacy_null_reasoning_resume_preserves_cached_work(self):
        FakeCodex.fail_at = "batch-0002-try-1"
        with patch("transub_core.pipeline.DEFAULT_REASONING", None), self.assertRaises(CodexError):
            process(self.args)
        work = self.folder / "work"
        manifest_path = work / "manifest.json"
        legacy_manifest = json.loads(manifest_path.read_text())
        legacy_manifest.pop("target_language")
        manifest_path.write_text(json.dumps(legacy_manifest))
        saved = {name: (work / name).read_bytes()
                 for name in ("research.json", "batch-0001.json")}
        self.assertIsNone(legacy_manifest["reasoning"])
        FakeCodex.calls, FakeCodex.fail_at = [], None
        with patch("transub_core.pipeline.CodexRunner", wraps=FakeCodex) as runner:
            process(self.args)
        self.assertIsNone(runner.call_args.args[2])
        self.assertEqual(FakeCodex.calls, ["batch-0002-try-1", "batch-0003-try-1"])
        self.assertEqual(json.loads(manifest_path.read_text()), {**legacy_manifest, "target_language": "ko"})
        for name, contents in saved.items():
            self.assertEqual((work / name).read_bytes(), contents, name)

    def test_explicit_medium_refuses_legacy_null_reasoning_before_runner(self):
        FakeCodex.fail_at = "batch-0002-try-1"
        with patch("transub_core.pipeline.DEFAULT_REASONING", None), self.assertRaises(CodexError):
            process(self.args)
        work = self.folder / "work"
        saved = {path: path.read_bytes() for path in work.iterdir() if path.is_file()}
        self.args.reasoning = "medium"
        with patch("transub_core.pipeline.CodexRunner", wraps=FakeCodex) as runner:
            with self.assertRaisesRegex(MediaError, "원본/설정이 다릅니다"):
                process(self.args)
        runner.assert_not_called()
        self.assertEqual(self.video.read_bytes(), self.original)
        self.assertEqual({path: path.read_bytes() for path in work.iterdir() if path.is_file()}, saved)

    def test_omitted_reasoning_resumes_stored_high(self):
        self.args.reasoning = "high"
        FakeCodex.fail_at = "batch-0002-try-1"
        with self.assertRaises(CodexError):
            process(self.args)
        self.args.reasoning = None
        FakeCodex.calls, FakeCodex.fail_at = [], None
        with patch("transub_core.pipeline.CodexRunner", wraps=FakeCodex) as runner:
            process(self.args)
        self.assertEqual(runner.call_args.args[2], "high")
        self.assertEqual(FakeCodex.calls, ["batch-0002-try-1", "batch-0003-try-1"])
        self.assertEqual(json.loads((self.folder / "work" / "manifest.json").read_text())["reasoning"], "high")

    def test_invalid_stored_reasoning_refused_before_runner(self):
        FakeCodex.fail_at = "batch-0002-try-1"
        with self.assertRaises(CodexError):
            process(self.args)
        manifest_path = self.folder / "work" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["reasoning"] = "invalid"
        manifest_path.write_text(json.dumps(manifest))
        saved_manifest = manifest_path.read_bytes()
        with patch("transub_core.pipeline.CodexRunner", wraps=FakeCodex) as runner:
            with self.assertRaisesRegex(MediaError, "추론 설정이 올바르지 않습니다"):
                process(self.args)
        runner.assert_not_called()
        self.assertEqual(self.video.read_bytes(), self.original)
        self.assertEqual(manifest_path.read_bytes(), saved_manifest)

    def test_old_protocol_cache_keeps_research_but_retranslates_batch(self):
        FakeCodex.fail_at = "batch-0002-try-1"
        with self.assertRaises(CodexError):
            process(self.args)
        checkpoint = self.folder / "work" / "batch-0001.json"
        data = json.loads(checkpoint.read_text())
        data["key"] = "old-token-protocol"
        checkpoint.write_text(json.dumps(data))
        research_path = checkpoint.with_name("research.json")
        saved_research = research_path.read_bytes()
        FakeCodex.calls, FakeCodex.fail_at = [], None
        process(self.args)
        self.assertEqual(FakeCodex.calls, [f"batch-{i:04d}-try-1" for i in (1, 2, 3)])
        self.assertEqual(research_path.read_bytes(), saved_research)

    def test_retry_identifies_invalid_span_and_only_checkpoints_valid_result(self):
        original_call = FakeCodex.call
        seen = []
        def invalid_then_valid(runner, prompt, schema, label, research=False):
            if label == "batch-0001-try-2":
                seen.append(prompt)
            result = original_call(runner, prompt, schema, label, research)
            if label == "batch-0001-try-1":
                event = next(iter(result["translations"].values()))
                event[next(iter(event))] = "⟦P0⟧잘못된 토큰"
            return result
        self.args.retries = 1
        with patch.object(FakeCodex, "call", invalid_then_valid):
            process(self.args)
        self.assertEqual(len(seen), 1)
        self.assertIn("c000000/s1", seen[0])
        self.assertIn("previous response failed validation", seen[0])
        checkpoint = json.loads((self.folder / "work" / "batch-0001.json").read_text())
        self.assertNotIn("잘못된 토큰", str(checkpoint))

    def test_existing_output_and_changed_source_refused(self):
        output = self.folder / "existing.mkv"
        output.write_text("user data")
        self.args.output = str(output)
        with self.assertRaises(MediaError):
            process(self.args)
        self.assertEqual(output.read_text(), "user data")
        self.assertEqual(self.video.read_bytes(), self.original)

    def test_changed_original_is_not_replaced(self):
        def change_source(path, executable):
            if Path(path) != self.video:
                self.video.write_bytes(self.original + b"external edit")
            return "equal hashes for test"
        with patch("transub_core.pipeline.av_hash", side_effect=change_source), self.assertRaises(MediaError):
            process(self.args)
        self.assertEqual(self.video.read_bytes(), self.original + b"external edit")

    def test_attachments_do_not_delay_new_subtitles(self):
        attachment = self.folder / "sample-font-data.bin"
        attachment.write_bytes(b"original attachment data\x00\xff")
        source = self.folder / "with-attachment.mkv"
        run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5",
             "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-i", str(self.folder / "input.ass"),
             "-map", "0:v", "-map", "1:a", "-map", "2:s", "-t", "25",
             "-c:v", "ffv1", "-c:a", "flac", "-c:s", "ass", "-attach", str(attachment),
             "-metadata:s:t:0", "mimetype=application/octet-stream", str(source)])
        output = self.folder / "with-attachment-translated.mkv"
        before = probe(source)
        mux(source, self.folder / "input.ass", output, before)
        after = probe(output)
        old = next(s for s in before["streams"] if s["codec_type"] == "attachment")
        new = next(s for s in after["streams"] if s["codec_type"] == "attachment")
        self.assertEqual(old["extradata_hash"], new["extradata_hash"])
        self.assertEqual(old["tags"], new["tags"])
        report = verify_interleaving(output, after, 3, 4)
        self.assertTrue(report["subtitle_delivery_verified"])

    def test_sparse_original_subtitles_do_not_delay_regular_new_subtitles(self):
        header = ASS.split("Comment:", 1)[0]

        def event(second, text):
            return (f"Dialogue: 0,0:00:{second:05.2f},0:00:{second + 0.6:05.2f},"
                    f"Default,,0,0,0,,{text}\n")

        sparse = self.folder / "sparse-original.ass"
        sparse.write_text(header + event(0.2, "SOURCE_SPARSE_001") + event(35.0, "SOURCE_SPARSE_002"))
        regular = self.folder / "regular-korean.ass"
        starts = list(range(0, 39, 2))
        regular.write_text(header + "".join(event(second + 0.2, "번역_001") for second in starts))
        attachment = self.folder / "sparse-font-data.bin"
        attachment.write_bytes(b"preserved attachment data\x00\xff")
        source = self.folder / "sparse-with-attachment.mkv"
        run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5",
             "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-i", str(sparse),
             "-map", "0:v", "-map", "1:a", "-map", "2:s", "-t", "40",
             "-c:v", "ffv1", "-c:a", "flac", "-c:s", "ass", "-attach", str(attachment),
             "-metadata:s:t:0", "mimetype=application/octet-stream", str(source)])
        before = probe(source)
        output = self.folder / "sparse-with-korean.mkv"
        mux(source, regular, output, before)
        after = probe(output)
        streams = verify_streams(before, after)
        old_attachment = next(s for s in before["streams"] if s["codec_type"] == "attachment")
        new_attachment = next(s for s in after["streams"] if s["codec_type"] == "attachment")
        self.assertEqual(old_attachment["extradata_hash"], new_attachment["extradata_hash"])
        self.assertEqual(old_attachment["tags"], new_attachment["tags"])
        self.assertEqual(av_hash(source), av_hash(output))
        # With FFmpeg's default interleave timeout this fixture delivers the third
        # Korean packet over 20 seconds late despite preserving its timestamp.
        report = verify_interleaving(output, after, streams["korean_stream_index"], len(starts))
        self.assertEqual(report["subtitle_packets"], 20)
        self.assertTrue(report["subtitle_delivery_verified"])
        self.assertLessEqual(report["max_subtitle_delivery_lag_seconds"], 1.0)


class FolderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()
        self.args = parser().parse_args([str(self.folder)])
        self.stderr = patch("sys.stderr", new_callable=io.StringIO)
        self.stderr.start()
        self.addCleanup(self.stderr.stop)

    def video(self, relative):
        path = self.folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_recursive_discovery_sorted_and_excludes_links_and_mux_directories(self):
        expected = [self.video(name) for name in
                    ("a.MKV", "nested/B.mkv", "nested/deeper/c.MkV", "Z.mkv")]
        self.video("ignored.mp4")
        self.video("nested/subtitle.ass")
        self.video(".transub-mux-stale/output.mkv")
        self.video("nested/.transub-mux-stale/output.mkv")
        (self.folder / "linked-file.mkv").symlink_to(expected[0])
        (self.folder / "linked-folder").symlink_to(self.folder / "nested", target_is_directory=True)
        (self.folder / "broken.mkv").symlink_to(self.folder / "missing.mkv")
        self.assertEqual(discover_videos(self.folder), expected)

    def test_scan_failure_prevents_all_processing(self):
        video = self.video("first.mkv")

        def broken_walk(folder, *, followlinks, onerror):
            self.assertFalse(followlinks)
            yield str(folder), [], [video.name]
            onerror(PermissionError("unreadable nested folder"))

        with patch("transub_core.batch.os.walk", side_effect=broken_walk), \
                patch("transub_core.batch.process") as single:
            with self.assertRaisesRegex(PermissionError, "unreadable nested folder"):
                process_input(self.args)
        single.assert_not_called()

    def test_empty_tree_and_folder_output_rejected_before_processing(self):
        self.video("nested/ignored.mp4")
        with patch("transub_core.batch.process") as single:
            with self.assertRaises(MediaError):
                process_input(self.args)
            self.video("nested/eligible.mkv")
            self.args.output = str(self.folder / "output.mkv")
            with self.assertRaisesRegex(MediaError, "--output"):
                process_input(self.args)
        single.assert_not_called()

    def test_single_file_dispatch_preserves_namespace_and_result(self):
        self.args.video = str(self.video("single.mkv"))
        result = {"status": "completed", "in_place": True}
        with patch("transub_core.batch.process", return_value=result) as single:
            self.assertIs(process_input(self.args), result)
        self.assertIs(single.call_args.args[0], self.args)

    def test_symlink_input_rejected(self):
        file = self.video("single.mkv")
        with patch("transub_core.batch.process") as single:
            for name, target in (("file-link.mkv", file), ("folder-link", self.folder)):
                link = self.folder / name
                link.symlink_to(target, target_is_directory=target.is_dir())
                self.args.video = str(link)
                with self.subTest(name=name), self.assertRaises(MediaError):
                    process_input(self.args)
        single.assert_not_called()

    def test_snapshot_clones_options_and_separates_same_basename_workdirs(self):
        paths = [self.video("sample.mkv"), self.video("nested/deep/sample.mkv")]
        self.args.work_dir = str(self.folder / "work")
        self.args.title = "FIXTURE_TITLE_HINT_001"
        self.args.allow_existing_korean = True
        original = vars(self.args).copy()
        seen = []

        def single(item):
            seen.append(item)
            self.video("new-during-processing.mkv")
            return {"status": "completed"}

        with patch("transub_core.batch.process", side_effect=single):
            result = process_input(self.args)
        self.assertEqual(vars(self.args), original)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["completed"], 2)
        self.assertEqual({item.video for item in seen}, {str(path) for path in paths})
        self.assertIsNot(seen[0], seen[1])
        for item in seen:
            self.assertIsNot(item, self.args)
            self.assertEqual(item.title, original["title"])
            self.assertTrue(item.allow_existing_korean)
            expected = self.folder / "work" / ("sample-" + digest(item.video)[:10])
            self.assertEqual(Path(item.work_dir), expected)
        self.assertNotEqual(seen[0].work_dir, seen[1].work_dir)

    def test_supported_failures_continue_and_existing_korean_is_skipped(self):
        errors = [CodexError("codex"), MediaError("media"), SubtitleError("subtitle"),
                  OSError("filesystem"), ValueError("invalid"),
                  subprocess.TimeoutExpired("ffprobe", 3)]
        outcomes = [ExistingKoreanError("Korean present"), *errors, {"status": "completed"}]
        paths = [self.video(f"{index:02d}.mkv") for index in range(len(outcomes))]
        by_path = dict(zip(map(str, paths), outcomes))

        def outcome(item):
            value = by_path[item.video]
            if isinstance(value, BaseException):
                raise value
            return value

        with patch("transub_core.batch.process", side_effect=outcome) as single:
            result = process_input(self.args)
        self.assertEqual(single.call_count, len(paths))
        self.assertCountEqual([call.args[0].video for call in single.call_args_list], list(map(str, paths)))
        self.assertTrue(all(call.args[0].work_dir is None for call in single.call_args_list))
        self.assertEqual({key: result[key] for key in
                          ("status", "folder", "total", "completed", "skipped", "failed")},
                         {"status": "failed", "folder": str(self.folder), "total": 8,
                          "completed": 1, "skipped": 1, "failed": 6})
        self.assertEqual(result["results"][0],
                         {"video": str(paths[0]), "status": "skipped", "reason": "Korean present"})
        for item, error in zip(result["results"][1:-1], errors):
            self.assertEqual(item["status"], "failed")
            self.assertEqual(item["error"], str(error))
        self.assertEqual(result["results"][-1]["result"], {"status": "completed"})

    def test_keyboard_interrupt_stops_before_next_video(self):
        self.args.jobs = 1
        self.video("a.mkv")
        self.video("b.mkv")
        with patch("transub_core.batch.process", side_effect=KeyboardInterrupt) as single:
            with self.assertRaises(KeyboardInterrupt):
                process_input(self.args)
        single.assert_called_once()

    def test_default_and_explicit_jobs_overlap_without_exceeding_limit(self):
        for index in range(6):
            self.video(f"SOURCE_{index:03d}.mkv")
        for jobs in (2, 3):
            with self.subTest(jobs=jobs):
                if jobs == 2:
                    self.assertEqual(self.args.jobs, 2)
                else:
                    self.args.jobs = jobs
                barrier = threading.Barrier(jobs, timeout=5)
                lock = threading.Lock()
                active = peak = 0

                def single(item):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    try:
                        barrier.wait()
                        return {"status": "completed"}
                    finally:
                        with lock:
                            active -= 1

                def bounded_wait(futures, **kwargs):
                    self.assertLessEqual(len(futures), jobs)
                    return wait_for_futures(futures, **kwargs)

                with patch("transub_core.batch.process", side_effect=single), \
                        patch("transub_core.batch.wait", side_effect=bounded_wait):
                    result = process_input(self.args)
                self.assertEqual(peak, jobs)
                self.assertEqual(active, 0)
                self.assertEqual(result["completed"], 6)

    def test_results_keep_discovery_order_when_workers_finish_in_reverse(self):
        paths = [self.video(f"SOURCE_{index:03d}.mkv") for index in (1, 2)]
        release_first = threading.Event()
        completed = []

        def single(item):
            if item.video == str(paths[0]):
                self.assertTrue(release_first.wait(5))
            completed.append(item.video)
            return {"source": item.video}

        def release_after_completion(futures, **kwargs):
            done, pending = wait_for_futures(futures, **kwargs)
            release_first.set()
            return done, pending

        with patch("transub_core.batch.process", side_effect=single), \
                patch("transub_core.batch.wait", side_effect=release_after_completion):
            result = process_input(self.args)
        self.assertEqual(completed, list(map(str, reversed(paths))))
        self.assertEqual([item["video"] for item in result["results"]], list(map(str, paths)))
        self.assertEqual([item["result"]["source"] for item in result["results"]], list(map(str, paths)))

    def test_jobs_one_runs_in_order_on_calling_thread(self):
        self.args.jobs = 1
        paths = [self.video(f"SOURCE_{index:03d}.mkv") for index in range(3)]
        seen = []
        calling_thread = threading.get_ident()

        def single(item):
            self.assertEqual(threading.get_ident(), calling_thread)
            seen.append(item.video)
            return {}

        with patch("transub_core.batch.process", side_effect=single), \
                patch("transub_core.batch.ThreadPoolExecutor") as executor:
            result = process_input(self.args)
        executor.assert_not_called()
        self.assertEqual(seen, list(map(str, paths)))
        self.assertEqual(result["completed"], 3)

    def test_parallel_logs_keep_file_prefixes_and_do_not_leak_to_caller(self):
        paths = [self.video(f"SOURCE_{index:03d}.mkv") for index in (1, 2)]
        output = io.StringIO()
        barrier = threading.Barrier(2, timeout=5)

        def single(item):
            for index in range(3):
                barrier.wait()
                runtime.log(f"{Path(item.video).stem} STEP_{index:03d}", file=output)
            return {}

        with patch("transub_core.batch.process", side_effect=single):
            process_input(self.args)
        runtime.log("CALLER_001", file=output)
        expected = [f"[{index}/2] {path.name} {path.stem} STEP_{step:03d}"
                    for index, path in enumerate(paths, 1) for step in range(3)]
        self.assertCountEqual(output.getvalue().splitlines(), [*expected, "CALLER_001"])

    def test_interrupt_cancels_running_worker_and_never_submits_more_files(self):
        paths = [self.video(f"SOURCE_{index:03d}.mkv") for index in range(5)]
        second_started = threading.Event()
        second_stopped = threading.Event()
        cancel_events = []
        submitted = []

        def context(prefix, cancel):
            cancel_events.append(cancel)
            return runtime.file_context(prefix, cancel)

        class RecordingExecutor(ThreadPoolExecutor):
            def submit(self, function, index, video):
                submitted.append(video)
                return super().submit(function, index, video)

        def single(item):
            if item.video == str(paths[0]):
                self.assertTrue(second_started.wait(5))
                raise KeyboardInterrupt
            second_started.set()
            try:
                self.assertTrue(cancel_events[-1].wait(5))
                runtime.check_cancelled()
                self.fail("running worker ignored cancellation")
            finally:
                second_stopped.set()

        with patch("transub_core.batch.process", side_effect=single), \
                patch("transub_core.batch.file_context", side_effect=context), \
                patch("transub_core.batch.ThreadPoolExecutor", RecordingExecutor):
            with self.assertRaises(KeyboardInterrupt):
                process_input(self.args)
        self.assertEqual(submitted, paths[:2])
        self.assertTrue(second_stopped.is_set())
        self.assertTrue(all(event is cancel_events[0] for event in cancel_events))
        self.assertTrue(cancel_events[0].is_set())


class FolderCliTests(unittest.TestCase):
    def test_jobs_defaults_to_two_and_accepts_positive_integer(self):
        self.assertEqual(parser().parse_args(["SOURCE_001"]).jobs, 2)
        for jobs in (1, 3, 20):
            with self.subTest(jobs=jobs):
                self.assertEqual(parser().parse_args(["SOURCE_001", "--jobs", str(jobs)]).jobs, jobs)

    def test_invalid_jobs_exits_before_processing(self):
        for jobs in ("0", "-1", "1.5", "invalid"):
            with self.subTest(jobs=jobs), \
                    patch("sys.argv", ["transub.py", "SOURCE_001", "--jobs", jobs]), \
                    patch("sys.stderr", new_callable=io.StringIO), \
                    patch("transub.process_input") as process_batch:
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                process_batch.assert_not_called()

    def test_folder_summary_and_exit_status_for_success_skip_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "a.mkv").touch()
            (folder / "b.mkv").touch()
            for outcomes, code, completed, skipped, failed in (
                    ([{}, {}], 0, 2, 0, 0),
                    ([ExistingKoreanError("already translated")] * 2, 0, 0, 2, 0),
                    ([MediaError("bad file"), {}], 1, 1, 0, 1)):
                by_name = dict(zip(("a.mkv", "b.mkv"), outcomes))

                def outcome(item):
                    value = by_name[Path(item.video).name]
                    if isinstance(value, BaseException):
                        raise value
                    return value

                with self.subTest(code=code, skipped=skipped), \
                        patch("sys.argv", ["transub.py", directory]), \
                        patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                        patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                        patch("transub_core.batch.process", side_effect=outcome):
                    self.assertEqual(main(), code)
                lines = stdout.getvalue().splitlines()
                self.assertEqual(lines[0], f"처리 완료: 총 2개 · 완료 {completed}개 · "
                                 f"건너뜀 {skipped}개 · 실패 {failed}개")
                self.assertNotIn('"results"', stdout.getvalue())
                if failed:
                    self.assertIn("실패한 파일:", lines)
                    self.assertIn("  - a.mkv: bad file", lines)
                    self.assertIn("같은 명령으로 다시 실행하면 저장된 번역을 재사용합니다.", lines)
                else:
                    self.assertEqual(len(lines), 1)
                self.assertIn("[1/2] a.mkv", stderr.getvalue())
                self.assertIn("[2/2] b.mkv", stderr.getvalue())

    def test_interrupt_and_discovery_error_exit_codes(self):
        for error, code in ((KeyboardInterrupt(), 130), (PermissionError("scan failed"), 1)):
            with self.subTest(code=code), patch("sys.argv", ["transub.py", "folder"]), \
                    patch("transub.process_input", side_effect=error), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(main(), code)
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue())


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class FolderPipelineTests(unittest.TestCase):
    setUp = PipelineTests.setUp

    def test_nested_same_basename_remux_and_rerun_without_codex(self):
        nested = self.folder / "season" / "episodes" / self.video.name
        nested.parent.mkdir(parents=True)
        shutil.copyfile(self.video, nested)
        self.args.video = str(self.folder)
        self.args.list_tracks = True
        listing = process_input(self.args)
        self.assertEqual(listing["completed"], 2)
        self.assertEqual(FakeCodex.calls, [])
        for item in listing["results"]:
            self.assertEqual([track["language"] for track in item["result"]["subtitle_tracks"]], ["en"])
        self.args.list_tracks = False
        result = process_input(self.args)
        self.assertEqual((result["status"], result["total"], result["completed"], result["failed"]),
                         ("completed", 2, 2, 0))
        self.assertEqual(FakeCodex.calls.count("research"), 2)
        for item in result["results"]:
            self.assertTrue(item["result"]["av_sha256_match"])
            self.assertTrue(item["result"]["in_place"])
            self.assertEqual([language(stream) for stream in probe(Path(item["video"]))["streams"]
                              if stream["codec_type"] == "subtitle"], ["en", "ko"])
        workdirs = list((self.folder / "work").iterdir())
        self.assertEqual(len(workdirs), 2)
        self.assertTrue(all((work / "verification.json").is_file() for work in workdirs))
        bytes_before_rerun = {path: path.read_bytes() for path in (self.video, nested)}
        calls_before_rerun = list(FakeCodex.calls)
        rerun = process_input(self.args)
        self.assertEqual((rerun["status"], rerun["completed"], rerun["skipped"], rerun["failed"]),
                         ("completed", 0, 2, 0))
        self.assertTrue(all(item["status"] == "skipped" for item in rerun["results"]))
        self.assertEqual(FakeCodex.calls, calls_before_rerun)
        for path, contents in bytes_before_rerun.items():
            self.assertEqual(path.read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
