"""Preflight checks.

Every failure mode this tool has ever had gets a check here, so problems are
reported up front with a fix, instead of surfacing as an empty output file an
hour into a run.

`run_checks` returns the list; `report` prints it; `fatal_count` decides whether
the pipeline may start.
"""

from __future__ import annotations

import importlib
import os
import socket
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, media
from .engines import Engine, GeminiEngine
from .ui import Console, ERR, MUTED, OK, WARN, fmt_size

OK_S, WARN_S, FAIL_S, SKIP_S = "ok", "warn", "fail", "skip"

XTERM_MIN = 2  # ui colour level at which the palette looks as designed


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


def fatal_count(checks: list[Check]) -> int:
    return sum(1 for c in checks if c.status == FAIL_S)


def warn_count(checks: list[Check]) -> int:
    return sum(1 for c in checks if c.status == WARN_S)


# --- individual checks ------------------------------------------------------

def check_python() -> Check:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 8):
        return Check("python", FAIL_S, f"{version} is too old",
                     "sub-translator needs Python 3.8 or newer")
    return Check("python", OK_S, version)


def check_package(module: str, package: str) -> Check:
    try:
        importlib.import_module(module)
    except ImportError:
        return Check(package, FAIL_S, "not installed",
                     "pip install -r requirements.txt")
    return Check(package, OK_S, "installed")


def check_tool(name: str) -> Check:
    version = media.tool_version(name)
    if version is None:
        return Check(name, FAIL_S, "not found on PATH",
                     "install FFmpeg and add it to PATH — https://ffmpeg.org/download.html")
    # "ffmpeg version 7.1 Copyright ..." -> "7.1"
    parts = version.split()
    short = parts[2] if len(parts) > 2 and parts[1] == "version" else version[:40]
    return Check(name, OK_S, short)


def check_env_file(path: Path = Path(".env")) -> Check:
    if path.is_file():
        return Check(".env", OK_S, str(path))
    return Check(".env", WARN_S, "not found",
                 "copy .env.example to .env and add your key "
                 "(only needed for the gemini engine)")


def check_input_dir(path: Path, pattern: str = "*.mkv") -> tuple[Check, list[Path]]:
    if not path.exists():
        return Check("input", FAIL_S, f"{path} does not exist",
                     "create it, or pass --input <folder>"), []
    if path.is_file():
        return Check("input", OK_S, f"single file: {path.name}"), [path]
    if not os.access(path, os.R_OK):
        return Check("input", FAIL_S, f"{path} is not readable"), []
    files = sorted(p for p in path.glob(pattern) if p.is_file())
    if not files:
        return Check("input", FAIL_S, f"no {pattern} files in {path}",
                     f"drop your .mkv files into {path}/"), []
    return Check("input", OK_S, f"{len(files)} file(s) in {path}"), files


def check_output_dir(path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check("output", FAIL_S, f"cannot create {path}: {exc}")
    probe_file = path / ".subtrans-write-test"
    try:
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
    except OSError as exc:
        return Check("output", FAIL_S, f"{path} is not writable: {exc}",
                     "check folder permissions or pass --output <folder>")
    return Check("output", OK_S, f"{path} is writable")


def check_disk(output_dir: Path, sources: list[Path], delete_source: bool) -> Check:
    try:
        free = shutil.disk_usage(output_dir if output_dir.exists() else Path(".")).free
    except OSError as exc:
        return Check("disk space", WARN_S, f"could not be measured: {exc}")
    # Each output is a stream copy of its source, so it needs about the same room.
    needed = sum(p.stat().st_size for p in sources if p.exists())
    if delete_source and sources:
        needed = max((p.stat().st_size for p in sources if p.exists()), default=0)
    needed = max(needed * 1.05, config.MIN_FREE_GB * 2**30)
    detail = f"{fmt_size(free)} free, ~{fmt_size(needed)} needed"
    if free < needed:
        return Check("disk space", FAIL_S, detail,
                     "free up space, or point --output at another drive")
    if free < needed * 1.5:
        return Check("disk space", WARN_S, detail + " (tight)")
    return Check("disk space", OK_S, detail)


def check_network(host: str) -> Check:
    try:
        with socket.create_connection((host, 443), timeout=6):
            return Check("network", OK_S, f"{host} reachable")
    except OSError as exc:
        return Check("network", FAIL_S, f"cannot reach {host}: {exc.__class__.__name__}",
                     "check your internet connection, VPN or proxy settings")


def check_engine(engine: Engine) -> list[Check]:
    checks = []
    for problem in engine.preflight():
        status = {"ok": OK_S, "warn": WARN_S, "error": FAIL_S}[problem.level]
        checks.append(Check(engine.name, status, problem.message, problem.hint))
    return checks


def check_model_online(engine: Engine) -> Check:
    """Live call: does the key work and does the configured model exist?"""
    if not isinstance(engine, GeminiEngine):
        return Check("model", SKIP_S, "not applicable to this engine")
    try:
        names = engine.list_models()
    except Exception as exc:
        return Check("model", FAIL_S, f"API call failed: {str(exc).splitlines()[0][:120]}",
                     "verify GEMINI_API_KEY at https://aistudio.google.com/apikey")
    if not names:
        return Check("model", WARN_S, "the API returned no models")
    if engine.model in names:
        return Check("model", OK_S, f"{engine.model} available ({len(names)} models total)")
    close = [n for n in names if "flash" in n][:3]
    return Check("model", WARN_S, f"'{engine.model}' is not in the list of available models",
                 "set GEMINI_MODEL in .env to one of: " + ", ".join(close or names[:3]))


def check_terminal(console: Console) -> Check:
    if not console.is_tty:
        return Check("terminal", SKIP_S, "output is redirected — plain text mode")
    depth = {0: "no colour", 1: "16 colours", 2: "256 colours", 3: "truecolor"}[console.level]
    glyphs = "unicode" if console.sym.unicode else "ascii fallback"
    status = OK_S if console.level >= XTERM_MIN else WARN_S
    hint = "" if status == OK_S else "Windows Terminal renders the full palette"
    return Check("terminal", status, f"{depth}, {glyphs}", hint)


# --- orchestration ----------------------------------------------------------

def run_checks(engine: Engine, input_path: Path, output_path: Path, console: Console,
               *, delete_source: bool = False, online: bool = False, deep: bool = False,
               include_terminal: bool = False) -> tuple[list[Check], list[Path]]:
    """Local checks always run; `online` adds a reachability probe and `deep`
    additionally spends one API call to confirm the key and model."""
    checks: list[Check] = [check_python()]

    checks.append(check_package("pysrt", "pysrt"))
    checks.append(check_package("dotenv", "python-dotenv"))
    checks.append(check_tool("ffmpeg"))
    checks.append(check_tool("ffprobe"))
    checks.append(check_env_file())
    checks.extend(check_engine(engine))

    input_check, files = check_input_dir(input_path)
    checks.append(input_check)
    checks.append(check_output_dir(output_path))
    checks.append(check_disk(output_path, files, delete_source))

    if online:
        host = ("generativelanguage.googleapis.com" if engine.name == "gemini"
                else "translate.googleapis.com")
        net = check_network(host)
        checks.append(net)
        if deep and net.status == OK_S and engine.name == "gemini":
            if not any(c.status == FAIL_S for c in checks if c.name == "gemini"):
                checks.append(check_model_online(engine))

    if include_terminal:
        checks.append(check_terminal(console))

    return checks, files


def report(console: Console, checks: list[Check], title: str = "Preflight") -> None:
    console.rule(console.paint(title, MUTED))
    pad = max((len(c.name) for c in checks), default=8) + 2
    for check in checks:
        if check.status == OK_S:
            mark, ink = console.sym.ok, OK
        elif check.status == WARN_S:
            mark, ink = console.sym.warn, WARN
        elif check.status == FAIL_S:
            mark, ink = console.sym.fail, ERR
        else:
            mark, ink = console.sym.info, MUTED
        name = console.paint(check.name.ljust(pad), MUTED)
        console.write(f"  {console.paint(mark, ink)} {name} {check.detail}")
        if check.hint and check.status in (WARN_S, FAIL_S):
            console.hint(f"{console.sym.step} {check.hint}")
    console.rule()
