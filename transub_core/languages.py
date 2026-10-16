"""Shared target languages and ISO 639 aliases for CLI, prompts, and tracks."""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TARGET_LANGUAGE = "ko"


@dataclass(frozen=True)
class Language:
    code: str
    iso639_2: str
    name: str

    @property
    def label(self) -> str:
        return "한국어" if self.code == "ko" else self.name

    @property
    def track_title(self) -> str:
        return "한국어 (Codex 번역)" if self.code == "ko" else f"{self.name} (Codex translation)"


# Matroska's legacy Language field uses ISO 639-2 bibliographic codes.
_LANGUAGES = (
    ("ko", "kor", "Korean"), ("en", "eng", "English"), ("ja", "jpn", "Japanese"),
    ("zh", "chi", "Chinese"), ("es", "spa", "Spanish"), ("fr", "fre", "French"),
    ("de", "ger", "German"), ("pt", "por", "Portuguese"), ("it", "ita", "Italian"),
    ("ru", "rus", "Russian"), ("uk", "ukr", "Ukrainian"), ("ar", "ara", "Arabic"),
    ("hi", "hin", "Hindi"), ("bn", "ben", "Bengali"), ("vi", "vie", "Vietnamese"),
    ("th", "tha", "Thai"), ("id", "ind", "Indonesian"), ("ms", "may", "Malay"),
    ("tr", "tur", "Turkish"), ("nl", "dut", "Dutch"), ("pl", "pol", "Polish"),
    ("sv", "swe", "Swedish"), ("no", "nor", "Norwegian"), ("da", "dan", "Danish"),
    ("fi", "fin", "Finnish"), ("cs", "cze", "Czech"), ("el", "gre", "Greek"),
    ("he", "heb", "Hebrew"), ("hu", "hun", "Hungarian"), ("ro", "rum", "Romanian"),
    ("fa", "per", "Persian"), ("ta", "tam", "Tamil"), ("te", "tel", "Telugu"),
    ("ur", "urd", "Urdu"), ("tl", "tgl", "Tagalog"), ("sw", "swa", "Swahili"),
)
LANGUAGES = {code: Language(code, alpha3, name) for code, alpha3, name in _LANGUAGES}
_ALIASES = {alias: item.code for item in LANGUAGES.values() for alias in (item.code, item.iso639_2)}
_ALIASES.update({"zho": "zh", "fra": "fr", "deu": "de", "msa": "ms", "nld": "nl",
                 "ces": "cs", "ell": "el", "ron": "ro", "fas": "fa"})


def get_language(value: str = DEFAULT_TARGET_LANGUAGE) -> Language:
    """Resolve a supported base-language code; reject regional tags and typos."""
    code = _ALIASES.get(value.strip().lower()) if isinstance(value, str) else None
    if code is None:
        raise ValueError("지원하지 않는 목표 언어입니다. 사용 가능한 코드: " + ", ".join(LANGUAGES))
    return LANGUAGES[code]


def normalize_code(value: str) -> str:
    """Normalize track tags, including region suffixes and legacy name aliases."""
    base = value.strip().lower().replace("_", "-").split("-")[0]
    names = {item.name.lower(): item.code for item in LANGUAGES.values()}
    return _ALIASES.get(base, names.get(base, "ja" if base == "jp" else base or "und"))
