"""Translation back-ends.

Both engines expose the same surface so the pipeline never branches on which
one is in use:

    engine.preflight()          -> list[Problem]   (import + credential checks)
    engine.connect()            -> None            (raises EngineError)
    engine.suggest_batch_size() -> int
    engine.translate(texts)     -> list[str]       (same length, always)

`translate` never raises for a partially bad batch. When the model returns the
wrong number of blocks it splits the batch and retries the halves; anything
still unrecoverable comes back as the original text and is counted in
`engine.untranslated`, which the pipeline reports at the end. Losing a line is
recoverable, shifting every subsequent subtitle's timing is not.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from . import config
from .config import SEPARATOR, lang_info, normalize_lang


class EngineError(RuntimeError):
    """The engine cannot run at all (missing package, bad key, no network)."""


@dataclass
class Problem:
    level: str          # "ok" | "warn" | "error"
    message: str
    hint: str = ""


@dataclass
class Engine:
    target: str = config.DEFAULT_TARGET
    source: str = "auto"
    delay: float = 0.0
    verbose: bool = False
    notes: list[str] = field(default_factory=list)
    untranslated: int = 0
    requests: int = 0
    _last_call: float = 0.0

    name = "engine"
    label = "Engine"
    needs_network = True

    # -- pacing -------------------------------------------------------------
    def _pace(self) -> None:
        """Keep at least `delay` seconds between outgoing requests."""
        if self.delay <= 0:
            return
        wait = self.delay - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def _mark_call(self) -> None:
        self._last_call = time.monotonic()
        self.requests += 1

    # -- interface ----------------------------------------------------------
    def preflight(self) -> list[Problem]:
        raise NotImplementedError

    def connect(self) -> None:
        raise NotImplementedError

    def suggest_batch_size(self, texts: list[str]) -> int:
        raise NotImplementedError

    def translate(self, texts: list[str]) -> list[str]:
        raise NotImplementedError

    def describe(self) -> str:
        return self.label


def _auto_batch(texts: list[str], target_chars: int, overhead: int) -> int:
    """Largest batch whose combined source text stays under `target_chars`.

    Bigger batches give the model more context and waste fewer tokens on the
    prompt preamble; the ceiling keeps a single failure from costing much.
    """
    if not texts:
        return config.AUTO_BATCH_MIN
    avg = sum(len(t) for t in texts) / len(texts) + overhead
    size = int(target_chars / max(1.0, avg))
    return max(config.AUTO_BATCH_MIN, min(config.AUTO_BATCH_MAX, size))


# --- Gemini -----------------------------------------------------------------

class GeminiEngine(Engine):
    name = "gemini"
    label = "Gemini API"

    def __init__(self, model: str | None = None, api_key: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.model = model or os.environ.get("GEMINI_MODEL") or config.DEFAULT_MODEL
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self.client = None

    def describe(self) -> str:
        return f"Gemini API · {self.model}"

    def preflight(self) -> list[Problem]:
        problems: list[Problem] = []
        try:
            import google.genai  # noqa: F401
        except ImportError:
            problems.append(Problem(
                "error", "python package 'google-genai' is not installed",
                config.pip_command(),
            ))
        key = (self.api_key or "").strip()
        if not key:
            problems.append(Problem(
                "error", "GEMINI_API_KEY is not set",
                "create a .env file with GEMINI_API_KEY=your_key "
                "(get one at https://aistudio.google.com/apikey)",
            ))
        else:
            if key != self.api_key:
                problems.append(Problem(
                    "warn", "GEMINI_API_KEY has surrounding whitespace",
                    "remove the stray spaces or quotes in .env",
                ))
            if key.startswith(("'", '"')) or key.endswith(("'", '"')):
                problems.append(Problem(
                    "warn", "GEMINI_API_KEY looks quoted",
                    "write the key without quotes in .env",
                ))
            elif len(key) < 30:
                problems.append(Problem(
                    "warn", f"GEMINI_API_KEY looks too short ({len(key)} chars)",
                    "double-check the key was copied in full",
                ))
            else:
                problems.append(Problem("ok", f"GEMINI_API_KEY present ({mask(key)})"))
        problems.append(Problem("ok", f"model: {self.model}"))
        return problems

    def connect(self) -> None:
        try:
            from google import genai
        except ImportError as exc:
            raise EngineError(
                f"google-genai is not installed — run: {config.pip_command()}"
            ) from exc
        if not (self.api_key or "").strip():
            raise EngineError("GEMINI_API_KEY is not set — see .env.example")
        try:
            self.client = genai.Client(api_key=self.api_key.strip())
        except Exception as exc:
            raise EngineError(f"could not initialise the Gemini client: {exc}") from exc

    def list_models(self) -> list[str]:
        """Live call: model names the key can actually use."""
        if self.client is None:
            self.connect()
        names = []
        for model in self.client.models.list():
            name = getattr(model, "name", "") or ""
            names.append(name.split("/")[-1])
        return names

    def suggest_batch_size(self, texts: list[str]) -> int:
        # +4 covers the "\n|||\n" separator around every block.
        return _auto_batch(texts, config.AUTO_BATCH_TARGET_CHARS, len(SEPARATOR) + 2)

    # -- translation ---
    def _prompt(self, combined: str, count: int) -> str:
        target_name = lang_info(self.target)[1]
        source_name = (
            "The source language may vary and must be auto-detected."
            if self.source in ("auto", "")
            else f"The source language is {lang_info(self.source)[1]}."
        )
        return (
            f"You are a professional movie subtitle translator. Translate the following "
            f"subtitles into {target_name}.\n"
            f"{source_name} Adapt idioms, slang and cultural references naturally while "
            f"keeping the original tone, register and emotion. Keep the translation short "
            f"enough to read on screen.\n"
            f"CRITICAL FORMAT RULES:\n"
            f"- The input contains exactly {count} text blocks separated by the exact "
            f"string '{SEPARATOR}'.\n"
            f"- Return exactly {count} blocks separated by '{SEPARATOR}', in the same order.\n"
            f"- Never merge, split, reorder, drop or add blocks, even if a block is a "
            f"fragment of a sentence.\n"
            f"- Preserve line breaks and any markup tags (<i>, {{\\an8}}, ...) inside a block.\n"
            f"- Output the translation only: no commentary, no numbering, no code fences.\n\n"
            f"{combined}"
        )

    def _request(self, texts: list[str]) -> list[str] | None:
        """One API call. Returns blocks only when the count matches exactly."""
        combined = f"\n{SEPARATOR}\n".join(texts)
        self._pace()
        response = self.client.models.generate_content(
            model=self.model, contents=self._prompt(combined, len(texts))
        )
        self._mark_call()
        raw = (response.text or "").strip()
        if not raw:
            reason = _blocked_reason(response)
            raise ValueError(f"empty response from the API{reason}")
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
        chunks = [chunk.strip() for chunk in raw.split(SEPARATOR)]
        if len(chunks) != len(texts):
            return None
        return chunks

    def translate(self, texts: list[str]) -> list[str]:
        return self._translate_chunk(texts, depth=0)

    def _translate_chunk(self, texts: list[str], depth: int) -> list[str]:
        if not texts:
            return []
        attempts = 3 if depth == 0 else 2

        for attempt in range(attempts):
            try:
                chunks = self._request(texts)
            except Exception as exc:
                message = str(exc)
                if _is_rate_limited(message):
                    wait = config.RATE_LIMIT_BACKOFF * (attempt + 1)
                    self.notes.append(f"rate limited, waiting {wait:.0f}s")
                    time.sleep(wait)
                    continue
                if _is_fatal(message):
                    raise EngineError(_clean(message)) from exc
                if attempt == attempts - 1:
                    self.notes.append(f"batch failed: {_clean(message)}")
                    break
                time.sleep(2 * (attempt + 1))
                continue

            if chunks is not None:
                # Never overwrite a line with an empty translation.
                return [new if new else old for new, old in zip(chunks, texts)]

            if len(texts) == 1:
                self.notes.append("model returned an unusable block for a single line")
                break
            self.notes.append(f"block-count mismatch in a batch of {len(texts)}, retrying")

        # Still wrong: halve the batch instead of abandoning the whole file.
        if len(texts) > 1 and depth < 4:
            mid = len(texts) // 2
            return (self._translate_chunk(texts[:mid], depth + 1)
                    + self._translate_chunk(texts[mid:], depth + 1))

        self.untranslated += len(texts)
        return list(texts)


def _blocked_reason(response) -> str:
    feedback = getattr(response, "prompt_feedback", None)
    reason = getattr(feedback, "block_reason", None) if feedback else None
    return f" (blocked: {reason})" if reason else " (possibly a safety filter)"


def _is_rate_limited(message: str) -> bool:
    low = message.lower()
    return "429" in low or "resource_exhausted" in low or "rate limit" in low


def _is_fatal(message: str) -> bool:
    low = message.lower()
    return any(marker in low for marker in (
        "api key not valid", "api_key_invalid", "permission_denied",
        "unauthenticated", "401", "403", "is not found for api version",
        "not found for api version",
    ))


def _clean(message: str) -> str:
    return " ".join(message.split())[:200]


def mask(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]}"


# --- Google Translate (free) ------------------------------------------------

# deep-translator expects a few regional tags rather than bare ISO codes.
_GT_OVERRIDES = {"zh": "zh-CN", "he": "iw", "no": "no"}


class GoogleEngine(Engine):
    name = "google"
    label = "Google Translate (free)"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.translator = None

    def _code(self, code: str) -> str:
        key = normalize_lang(code)
        return _GT_OVERRIDES.get(key, key or "auto")

    def preflight(self) -> list[Problem]:
        try:
            import deep_translator  # noqa: F401
        except ImportError:
            return [Problem(
                "error", "python package 'deep-translator' is not installed",
                config.pip_command(),
            )]
        return [Problem("ok", "deep-translator available (no API key required)")]

    def connect(self) -> None:
        try:
            from deep_translator import GoogleTranslator
        except ImportError as exc:
            raise EngineError(
                f"deep-translator is not installed — run: {config.pip_command()}"
            ) from exc
        try:
            self.translator = GoogleTranslator(
                source=self._code(self.source) if self.source != "auto" else "auto",
                target=self._code(self.target),
            )
        except Exception as exc:
            raise EngineError(f"could not initialise Google Translate: {exc}") from exc

    def suggest_batch_size(self, texts: list[str]) -> int:
        # The free endpoint translates each line separately, so the batch size
        # only controls how often progress updates; keep it modest.
        return min(config.DEFAULT_BATCH_GOOGLE, config.AUTO_BATCH_MAX)

    def translate(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        for attempt in range(3):
            try:
                self._pace()
                result = self.translator.translate_batch(list(texts))
                self._mark_call()
            except Exception as exc:
                if attempt == 2:
                    self.notes.append(f"batch failed: {_clean(str(exc))}")
                    self.untranslated += len(texts)
                    return list(texts)
                time.sleep(2 * (attempt + 1))
                continue

            if result and len(result) == len(texts):
                return [
                    (new or "").strip() or old
                    for new, old in zip(result, texts)
                ]
            self.notes.append(f"unexpected result count in a batch of {len(texts)}")

        self.untranslated += len(texts)
        return list(texts)


ENGINES = {"gemini": GeminiEngine, "google": GoogleEngine}


def build_engine(name: str, **kwargs) -> Engine:
    try:
        factory = ENGINES[name]
    except KeyError:
        raise EngineError(f"unknown engine '{name}' (expected: {', '.join(ENGINES)})") from None
    return factory(**kwargs)
