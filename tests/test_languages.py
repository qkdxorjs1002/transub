import io
import unittest
from unittest.mock import patch

from transub import parser
from transub_core.languages import LANGUAGES, get_language, normalize_code
from transub_core.media import MediaError, language, select_track


class LanguageTests(unittest.TestCase):
    def test_cli_defaults_and_iso_aliases(self):
        self.assertEqual(parser().parse_args(["SOURCE_001.mkv"]).target_language, "ko")
        aliases = {"ENG": "en", " kor ": "ko", "jpn": "ja", "zho": "zh", "chi": "zh",
                   "fra": "fr", "fre": "fr", "deu": "de", "ger": "de", "msa": "ms",
                   "nld": "nl", "ces": "cs", "ell": "el", "ron": "ro", "fas": "fa"}
        for alias, code in aliases.items():
            with self.subTest(alias=alias):
                self.assertEqual(parser().parse_args(
                    ["SOURCE_001.mkv", "--target-language", alias]).target_language, code)
        for code, target in LANGUAGES.items():
            with self.subTest(code=code):
                self.assertEqual(get_language(target.iso639_2), get_language(code))
                self.assertEqual(normalize_code(target.iso639_2), code)

    def test_invalid_target_rejected_by_cli_and_resolver(self):
        for value in ("", "xx", "en-US", "ko_KR", "English", "en,ja"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    get_language(value)
                with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                    parser().parse_args(["SOURCE_001.mkv", "--target-language", value])
                self.assertEqual(error.exception.code, 2)

    def test_generic_and_legacy_flags_share_selected_target(self):
        for default in ("--default-subtitle", "--default-korean"):
            for allow in ("--allow-existing-target", "--allow-existing-korean"):
                with self.subTest(default=default, allow=allow):
                    args = parser().parse_args(["SOURCE_001.mkv", "--target-language", "fra", default, allow])
                    self.assertEqual(args.target_language, "fr")
                    self.assertTrue(args.default_korean)
                    self.assertTrue(args.allow_existing_korean)

    def test_track_language_aliases_regions_titles_and_tag_precedence(self):
        for tag, title, expected in (("fra", "", "fr"), ("fre", "", "fr"),
                                     ("FR-ca", "", "fr"), ("deu_AT", "", "de"),
                                     ("Chinese", "", "zh"), ("jp", "", "ja"),
                                     ("und", "French", "fr"), ("unknown", "한국어", "ko"),
                                     ("eng", "French", "en"), ("und", "SOURCE_001", "und")):
            with self.subTest(tag=tag, title=title):
                stream = {"tags": {"LANGUAGE": tag, "TITLE": title}}
                self.assertEqual(language(stream), expected)

    def test_auto_source_excludes_only_selected_target_and_explicit_can_select_it(self):
        streams = [{"index": i, "codec_type": "subtitle", "codec_name": "ass",
                    "tags": {"language": code}} for i, code in enumerate(("jpn", "eng", "kor", "fra"))]
        for target, expected in (("ja", 1), ("en", 0), ("ko", 0), ("fr", 0)):
            with self.subTest(target=target):
                self.assertEqual(select_track(streams, target_language=target)["index"], expected)
        self.assertEqual(select_track(streams[1:3], target_language="en")["index"], 2)
        self.assertEqual(select_track(streams, 0, "ja")["index"], 0)
        with self.assertRaises(MediaError):
            select_track([streams[0]], target_language="ja")


if __name__ == "__main__":
    unittest.main()
