"""Defaults, environment loading and the language table.

Deliberately dependency-free: `python-dotenv` is used when available, but a
tiny built-in parser keeps `.env` working even before `pip install`.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "sub-translator"
# Shown only by `--version`; the banner deliberately stays clean.
VERSION = "0.2"

# --- defaults ---------------------------------------------------------------

DEFAULT_TARGET = "uk"
DEFAULT_INPUT = "input"
DEFAULT_OUTPUT = "output"
DEFAULT_MODEL = "gemini-3.5-flash-lite"

# Batching. `--auto-batch` overrides the fixed size using AUTO_BATCH_TARGET_CHARS.
DEFAULT_BATCH_GEMINI = 60
DEFAULT_BATCH_GOOGLE = 50
AUTO_BATCH_TARGET_CHARS = 6000  # source characters sent per request
AUTO_BATCH_MIN = 20
AUTO_BATCH_MAX = 220

# Pacing. The Gemini free tier allows ~15 requests/minute, hence 4s between calls.
DEFAULT_DELAY_GEMINI = 4.0
DEFAULT_DELAY_GOOGLE = 1.0
RATE_LIMIT_BACKOFF = 35.0

MIN_FREE_GB = 1.0
SEPARATOR = "|||"

# Subtitle codecs that ffmpeg can turn into SRT. Anything else (PGS, VobSub) is
# a bitmap format and needs OCR, which this tool does not do.
TEXT_SUB_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "text",
    "webvtt", "microdvd", "subviewer", "subviewer1", "stl",
}
IMAGE_SUB_CODECS = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvbsub",
}

# --- languages --------------------------------------------------------------

# code -> (ISO 639-2/B code for Matroska metadata, English name, native name)
LANGUAGES: dict[str, tuple[str, str, str]] = {
    "uk": ("ukr", "Ukrainian", "Українська"),
    "en": ("eng", "English", "English"),
    "pl": ("pol", "Polish", "Polski"),
    "de": ("ger", "German", "Deutsch"),
    "fr": ("fre", "French", "Français"),
    "es": ("spa", "Spanish", "Español"),
    "it": ("ita", "Italian", "Italiano"),
    "pt": ("por", "Portuguese", "Português"),
    "ru": ("rus", "Russian", "Русский"),
    "cs": ("cze", "Czech", "Čeština"),
    "sk": ("slo", "Slovak", "Slovenčina"),
    "ro": ("rum", "Romanian", "Română"),
    "hu": ("hun", "Hungarian", "Magyar"),
    "tr": ("tur", "Turkish", "Türkçe"),
    "nl": ("dut", "Dutch", "Nederlands"),
    "sv": ("swe", "Swedish", "Svenska"),
    "no": ("nor", "Norwegian", "Norsk"),
    "da": ("dan", "Danish", "Dansk"),
    "fi": ("fin", "Finnish", "Suomi"),
    "el": ("gre", "Greek", "Ελληνικά"),
    "bg": ("bul", "Bulgarian", "Български"),
    "sr": ("srp", "Serbian", "Српски"),
    "hr": ("hrv", "Croatian", "Hrvatski"),
    "lt": ("lit", "Lithuanian", "Lietuvių"),
    "lv": ("lav", "Latvian", "Latviešu"),
    "et": ("est", "Estonian", "Eesti"),
    "he": ("heb", "Hebrew", "עברית"),
    "ar": ("ara", "Arabic", "العربية"),
    "fa": ("per", "Persian", "فارسی"),
    "hi": ("hin", "Hindi", "हिन्दी"),
    "th": ("tha", "Thai", "ไทย"),
    "vi": ("vie", "Vietnamese", "Tiếng Việt"),
    "id": ("ind", "Indonesian", "Bahasa Indonesia"),
    "ja": ("jpn", "Japanese", "日本語"),
    "ko": ("kor", "Korean", "한국어"),
    "zh": ("chi", "Chinese", "中文"),
}

# Extra ISO 639-2 codes that may appear in a track but map to the same language.
_ALIASES = {
    "ukr": "uk", "eng": "en", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr",
    "spa": "es", "ita": "it", "por": "pt", "rus": "ru", "ces": "cs", "cze": "cs",
    "slk": "sk", "slo": "sk", "ron": "ro", "rum": "ro", "hun": "hu", "tur": "tr",
    "nld": "nl", "dut": "nl", "swe": "sv", "nor": "no", "dan": "da", "fin": "fi",
    "ell": "el", "gre": "el", "bul": "bg", "srp": "sr", "hrv": "hr", "lit": "lt",
    "lav": "lv", "est": "et", "heb": "he", "ara": "ar", "fas": "fa", "per": "fa",
    "hin": "hi", "tha": "th", "vie": "vi", "ind": "id", "jpn": "ja", "kor": "ko",
    "zho": "zh", "chi": "zh", "pol": "pl",
}


def normalize_lang(code: str) -> str:
    """Fold any spelling of a language tag down to our two-letter key."""
    if not code:
        return ""
    c = code.strip().lower().replace("_", "-")
    if c in LANGUAGES:
        return c
    if c in _ALIASES:
        return _ALIASES[c]
    base = c.split("-", 1)[0]
    if base in LANGUAGES:
        return base
    return _ALIASES.get(base, base)


def lang_info(code: str) -> tuple[str, str, str]:
    """(iso639-2, English name, native name) for a language code."""
    key = normalize_lang(code)
    if key in LANGUAGES:
        return LANGUAGES[key]
    return (key[:3] or "und", code.upper(), code.upper())


# Short tag used in output file names and track titles. Ukrainian is "UA" by
# convention (and keeps folders created by earlier versions resumable).
_TAGS = {"uk": "UA", "en": "EN", "cs": "CZ", "da": "DK", "el": "GR",
         "et": "EE", "ja": "JP", "ko": "KR", "sv": "SE", "zh": "CN"}


def lang_tag(code: str) -> str:
    key = normalize_lang(code)
    return _TAGS.get(key, key.upper() or "XX")


def lang_matches(track_lang: str, wanted: str) -> bool:
    return bool(track_lang) and normalize_lang(track_lang) == normalize_lang(wanted)


# --- environment ------------------------------------------------------------

def load_env(env_path: Path | None = None) -> Path | None:
    """Load `.env` into os.environ. Returns the file used, or None.

    Falls back to a minimal parser so `doctor` still works before dependencies
    are installed. Existing environment variables always win.
    """
    path = env_path or Path(".env")
    try:
        from dotenv import load_dotenv  # type: ignore
        if path.is_file():
            load_dotenv(path, override=False)
            return path
        load_dotenv(override=False)
        return None
    except ImportError:
        pass

    if not path.is_file():
        return None
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return path
    except OSError:
        return None
