"""ffmpeg / ffprobe wrappers.

Unlike the original scripts, stderr is captured and surfaced: a failed extract
or mux now says *why* instead of silently producing nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import (
    IMAGE_SUB_CODECS, TEXT_SUB_CODECS, lang_info, lang_matches, lang_tag,
)

# Track titles that usually contain only on-screen text or karaoke, not dialogue.
_JUNK_TITLE_MARKERS = ("sign", "song", "karaoke", "forced", "commentary")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


class MediaError(RuntimeError):
    """An ffmpeg/ffprobe call failed; the message carries ffmpeg's own words."""


@dataclass
class SubStream:
    rel_index: int      # index among subtitle streams -> used as 0:s:N
    abs_index: int      # index among all streams
    codec: str
    language: str
    title: str
    default: bool
    forced: bool

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_SUB_CODECS

    @property
    def is_image(self) -> bool:
        return self.codec in IMAGE_SUB_CODECS

    @property
    def looks_like_signs(self) -> bool:
        low = self.title.lower()
        return any(marker in low for marker in _JUNK_TITLE_MARKERS)


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise MediaError(f"{cmd[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{cmd[0]} timed out after {timeout}s") from exc


def _last_error_line(stderr: str) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        if not line.startswith(("frame=", "size=", "video:", "  ")):
            return line
    return lines[-1] if lines else "unknown error"


def tool_version(name: str) -> str | None:
    """First line of `<tool> -version`, or None when the tool is missing."""
    if shutil.which(name) is None:
        return None
    try:
        result = _run([name, "-version"], timeout=15)
    except MediaError:
        return None
    line = (result.stdout or "").splitlines()
    return line[0].strip() if line else name


def probe(path: Path) -> dict:
    """Full ffprobe JSON for a container. Raises MediaError on unreadable files."""
    result = _run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path),
    ], timeout=120)
    if result.returncode != 0:
        raise MediaError(_last_error_line(result.stderr))
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"could not parse ffprobe output: {exc}") from exc


def container_duration(info: dict) -> float:
    try:
        return float(info.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        return 0.0


def total_stream_count(info: dict) -> int:
    return len(info.get("streams", []))


def subtitle_streams(info: dict) -> list[SubStream]:
    streams = []
    rel = 0
    for stream in info.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        streams.append(SubStream(
            rel_index=rel,
            abs_index=int(stream.get("index", rel)),
            codec=(stream.get("codec_name") or "unknown").lower(),
            language=(tags.get("language") or "").lower(),
            title=tags.get("title") or "",
            default=bool(disposition.get("default")),
            forced=bool(disposition.get("forced")),
        ))
        rel += 1
    return streams


def select_stream(streams: list[SubStream], source_lang: str | None) -> tuple[SubStream | None, str]:
    """Pick the best track to translate. Returns (stream, human-readable reason).

    Only text-based tracks are eligible — PGS/VobSub are bitmaps and would need
    OCR, so they are reported rather than silently mis-extracted.
    """
    text_tracks = [s for s in streams if s.is_text]
    if not text_tracks:
        return None, "no text-based subtitle track"

    wanted = [source_lang] if source_lang and source_lang != "auto" else ["en"]

    def by(predicate) -> SubStream | None:
        return next((s for s in text_tracks if predicate(s)), None)

    for lang in wanted:
        hit = by(lambda s: lang_matches(s.language, lang) and not s.looks_like_signs)
        if hit:
            return hit, f"{lang_info(lang)[1]} dialogue track"
        hit = by(lambda s: lang_matches(s.language, lang))
        if hit:
            return hit, f"{lang_info(lang)[1]} track"

    hit = by(lambda s: s.default and not s.looks_like_signs)
    if hit:
        return hit, "default track"
    hit = by(lambda s: not s.looks_like_signs)
    if hit:
        return hit, "first full track"
    hit = by(lambda s: s.default)
    if hit:
        return hit, "default track (fallback)"
    return text_tracks[0], "first subtitle track (fallback)"


def describe_stream(stream: SubStream) -> str:
    bits = [f"0:s:{stream.rel_index}", stream.codec]
    if stream.language:
        bits.append(stream.language)
    if stream.title:
        bits.append(f'"{stream.title}"')
    flags = [f for f, on in (("default", stream.default), ("forced", stream.forced)) if on]
    if flags:
        bits.append("+".join(flags))
    return " · ".join(bits)


def extract_subtitle(mkv_path: Path, rel_index: int, out_srt: Path) -> None:
    """Demux one subtitle track to SRT."""
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    result = _run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(mkv_path),
        "-map", f"0:s:{rel_index}", "-c:s", "srt", str(out_srt),
    ])
    if result.returncode != 0:
        raise MediaError(_last_error_line(result.stderr))
    if not out_srt.exists() or out_srt.stat().st_size == 0:
        raise MediaError("ffmpeg produced an empty subtitle file")


def inject_subtitle(source_mkv: Path, srt_path: Path, out_mkv: Path,
                    target_lang: str, stream_count: int) -> None:
    """Copy every original stream and append the translated track as default."""
    iso3, english, _ = lang_info(target_lang)
    new_index = stream_count  # the appended track lands at the end
    result = _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(source_mkv), "-i", str(srt_path),
        "-map", "0", "-map", "1:0", "-c", "copy", "-c:s:m:0", "srt",
        f"-metadata:s:{new_index}", f"language={iso3}",
        f"-metadata:s:{new_index}", f"title={lang_tag(target_lang)} - {english}",
        f"-disposition:s:{new_index}", "default",
        str(out_mkv),
    ])
    if result.returncode != 0:
        # Re-encoding subtitles is the usual culprit; retry with a plain copy.
        retry = _run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(source_mkv), "-i", str(srt_path),
            "-map", "0", "-map", "1:0", "-c", "copy",
            f"-metadata:s:{new_index}", f"language={iso3}",
            f"-metadata:s:{new_index}", f"title={lang_tag(target_lang)} - {english}",
            f"-disposition:s:{new_index}", "default",
            str(out_mkv),
        ])
        if retry.returncode != 0:
            raise MediaError(_last_error_line(result.stderr))
    if not out_mkv.exists() or out_mkv.stat().st_size == 0:
        raise MediaError("ffmpeg produced an empty output file")


def verify_output(out_mkv: Path, source_mkv: Path, target_lang: str) -> tuple[bool, str]:
    """Confirm the muxed file is sane before anything destructive happens."""
    if not out_mkv.exists() or out_mkv.stat().st_size == 0:
        return False, "output file is missing or empty"
    src_size = source_mkv.stat().st_size if source_mkv.exists() else 0
    if src_size and out_mkv.stat().st_size < src_size * 0.9:
        return False, "output is suspiciously smaller than the source"
    try:
        info = probe(out_mkv)
    except MediaError as exc:
        return False, f"output does not probe cleanly: {exc}"
    iso3 = lang_info(target_lang)[0]
    for stream in subtitle_streams(info):
        if stream.language == iso3:
            return True, "verified"
    return False, f"no '{iso3}' subtitle track found in the output"
