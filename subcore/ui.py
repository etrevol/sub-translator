"""Terminal presentation layer.

Zero third-party dependencies. Everything degrades on its own:
truecolor -> 256 colors -> 16 colors -> plain text, Unicode -> ASCII,
animated bars -> periodic plain lines when stdout is not a terminal.

The accent colour of the project is #FF007F.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass, field

ESC = "\x1b"
RESET = f"{ESC}[0m"
BOLD = f"{ESC}[1m"
DIM = f"{ESC}[2m"
HIDE_CURSOR = f"{ESC}[?25l"
SHOW_CURSOR = f"{ESC}[?25h"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Capability levels
NO_COLOR, BASIC, XTERM256, TRUECOLOR = 0, 1, 2, 3


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def visible_len(text: str) -> int:
    """Printed width, not character count: CJK glyphs take two cells and
    combining marks take none, which is what keeps tables aligned."""
    width = 0
    for char in strip_ansi(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


@dataclass(frozen=True)
class Ink:
    """One palette entry, expressed once per colour depth."""
    rgb: tuple[int, int, int]
    xterm: int
    basic: int  # SGR foreground code (30-37 / 90-97)

    def fg(self, level: int) -> str:
        if level >= TRUECOLOR:
            r, g, b = self.rgb
            return f"{ESC}[38;2;{r};{g};{b}m"
        if level == XTERM256:
            return f"{ESC}[38;5;{self.xterm}m"
        if level == BASIC:
            return f"{ESC}[{self.basic}m"
        return ""


# --- palette (built around #FF007F) ----------------------------------------
#
# Monochrome by design: the accent plus neutrals, nothing else. Status is
# carried by the glyph, not by hue — ✔ reads white, ✖ reads accent, ▲ recedes
# into the muted grey. Adding a green or a yellow here would break that.

ACCENT = Ink((255, 0, 127), 198, 95)
ACCENT_SOFT = Ink((255, 122, 183), 211, 95)
TEXT = Ink((222, 222, 226), 253, 37)
MUTED = Ink((136, 136, 146), 245, 90)
TRACK = Ink((58, 58, 64), 237, 90)

OK = Ink((240, 240, 244), 255, 97)   # ✔ success
WARN = MUTED                         # ▲ worth knowing, not worth stopping for
ERR = ACCENT                         # ✖ failure


@dataclass
class Symbols:
    """Glyphs with an ASCII fallback for consoles that cannot encode them."""
    unicode: bool = True

    @property
    def block(self) -> str:
        return "█" if self.unicode else "#"

    @property
    def track(self) -> str:
        return "█" if self.unicode else "."

    @property
    def ok(self) -> str:
        return "✔" if self.unicode else "+"

    @property
    def fail(self) -> str:
        return "✖" if self.unicode else "x"

    @property
    def warn(self) -> str:
        return "▲" if self.unicode else "!"

    @property
    def info(self) -> str:
        return "•" if self.unicode else "-"

    @property
    def step(self) -> str:
        return "›" if self.unicode else ">"

    @property
    def rule(self) -> str:
        return "─" if self.unicode else "-"

    @property
    def logo(self) -> str:
        return "██" if self.unicode else "##"


def _enable_windows_vt() -> bool:
    """Turn on ANSI escape processing in the Windows console. Win10 1703+."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _detect_color_level(stream, when: str) -> int:
    if when == "never" or os.environ.get("NO_COLOR"):
        return NO_COLOR
    tty = hasattr(stream, "isatty") and stream.isatty()
    if not tty and when != "always":
        return NO_COLOR

    if os.name == "nt":
        # Windows Terminal, VS Code and ConEmu all handle 24-bit colour.
        if _enable_windows_vt():
            return TRUECOLOR
        return NO_COLOR if when != "always" else BASIC

    term = os.environ.get("TERM", "")
    if term == "dumb":
        return NO_COLOR if when != "always" else BASIC
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return TRUECOLOR
    if os.environ.get("TERM_PROGRAM") in ("iTerm.app", "vscode", "WezTerm", "Hyper"):
        return TRUECOLOR
    if "256" in term:
        return XTERM256
    return BASIC


def _detect_unicode(stream) -> bool:
    enc = (getattr(stream, "encoding", None) or "").lower()
    if not enc:
        return False
    try:
        "█✔›─".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def ellipsize(text: str, max_len: int) -> str:
    """Shorten from the middle — release names carry information at both ends."""
    if max_len < 8 or len(text) <= max_len:
        return text
    head = (max_len - 1) // 2
    tail = max_len - 1 - head
    return f"{text[:head]}…{text[-tail:]}"


def fmt_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024 or unit == "TB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def fmt_duration(seconds: float) -> str:
    return _fmt_eta(seconds)


class Console:
    """Everything that writes to the terminal goes through here.

    Keeping a single owner of the output stream is what makes it possible to
    print log lines while progress bars are on screen without tearing them.
    """

    def __init__(self, stream=None, color: str = "auto", unicode_ok: bool | None = None):
        self.stream = stream or sys.stdout
        self.level = _detect_color_level(self.stream, color)
        self.is_tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        if unicode_ok is None:
            unicode_ok = _detect_unicode(self.stream)
        self.sym = Symbols(unicode=unicode_ok)
        self.quiet = False
        self._progress: "ProgressGroup | None" = None

    # -- primitives ---------------------------------------------------------

    @property
    def width(self) -> int:
        try:
            return max(60, min(shutil.get_terminal_size((100, 24)).columns, 120))
        except OSError:
            return 100

    def paint(self, text: str, ink: Ink | None = None, bold: bool = False,
              dim: bool = False) -> str:
        if self.level == NO_COLOR or not text:
            return text
        prefix = ""
        if ink is not None:
            prefix += ink.fg(self.level)
        if bold:
            prefix += BOLD
        if dim:
            prefix += DIM
        return f"{prefix}{text}{RESET}" if prefix else text

    def write(self, text: str = "") -> None:
        """Write a line, stepping around an active progress block."""
        if self.quiet:
            return
        if self._progress is not None and self._progress.live:
            self._progress.clear()
            self._safe_write(text + "\n")
            self._progress.render(force=True)
        else:
            self._safe_write(text + "\n")

    def _safe_write(self, raw: str) -> None:
        try:
            self.stream.write(raw)
            self.stream.flush()
        except UnicodeEncodeError:
            # Console cannot encode a glyph: drop to ASCII and keep going.
            self.sym = Symbols(unicode=False)
            self.stream.write(raw.encode("ascii", "replace").decode("ascii"))
            self.stream.flush()
        except (BrokenPipeError, OSError):
            pass

    # -- building blocks ----------------------------------------------------

    def banner(self) -> None:
        logo = self.paint(self.sym.logo, ACCENT, bold=True)
        name = self.paint("sub", ACCENT, bold=True) + self.paint("-translator", TEXT, bold=True)
        self.write("")
        self.write(f"  {logo}  {name}")
        self.rule()

    def rule(self, label: str = "") -> None:
        width = self.width - 4
        if label:
            head = f"{self.sym.rule * 2} {label} "
            tail = self.sym.rule * max(0, width - visible_len(head))
            self.write("  " + self.paint(head + tail, TRACK))
        else:
            self.write("  " + self.paint(self.sym.rule * width, TRACK))

    def blank(self) -> None:
        self.write("")

    def step(self, text: str) -> None:
        self.write(f"  {self.paint(self.sym.step, ACCENT)} {text}")

    def info(self, text: str) -> None:
        self.write(f"  {self.paint(self.sym.info, MUTED)} {self.paint(text, MUTED)}")

    def ok(self, text: str) -> None:
        self.write(f"  {self.paint(self.sym.ok, OK)} {text}")

    def warn(self, text: str) -> None:
        self.write(f"  {self.paint(self.sym.warn, WARN)} {text}")

    def alert(self, text: str) -> None:
        """A warning about something destructive — the one case where a mere
        warning earns the accent colour instead of receding into the grey."""
        self.write(f"  {self.paint(self.sym.warn, ACCENT, bold=True)} "
                   f"{self.paint(text, TEXT)}")

    def error(self, text: str) -> None:
        self.write(f"  {self.paint(self.sym.fail, ERR)} {text}")

    def hint(self, text: str) -> None:
        self.write(f"    {self.paint(text, MUTED)}")

    def kv(self, key: str, value: str, pad: int = 16) -> None:
        self.write(f"  {self.paint(key.ljust(pad), MUTED)} {value}")

    def table(self, headers: list[str], rows: list[list[str]],
              highlight: int | None = None) -> None:
        """Compact table. `highlight` marks one row index with the accent colour."""
        if not rows:
            return
        widths = [visible_len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], visible_len(cell))
        head = "  ".join(
            h + " " * (widths[i] - visible_len(h)) for i, h in enumerate(headers)
        )
        self.write("    " + self.paint(head, MUTED))
        self.write("    " + self.paint(self.sym.rule * visible_len(head), TRACK))
        for idx, row in enumerate(rows):
            line = "  ".join(
                cell + " " * (widths[i] - visible_len(cell)) for i, cell in enumerate(row)
            )
            if idx == highlight:
                marker = self.paint(self.sym.step, ACCENT)
                self.write(f"  {marker} " + self.paint(line, ACCENT_SOFT))
            else:
                self.write("    " + line)

    def panel(self, title: str, lines: list[str]) -> None:
        self.blank()
        self.rule(self.paint(title, ACCENT, bold=True))
        for line in lines:
            self.write(f"  {line}")
        self.rule()

    def confirm(self, question: str, default: bool = False) -> bool:
        """Ask a yes/no question. Non-interactive input keeps the default."""
        if not sys.stdin or not sys.stdin.isatty():
            return default
        suffix = "[y/N]" if not default else "[Y/n]"
        prompt = f"  {self.paint('?', ACCENT, bold=True)} {question} {self.paint(suffix, MUTED)} "
        try:
            self._safe_write(prompt)
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            self.write("")
            return False
        if not answer:
            return default
        return answer in ("y", "yes", "т", "так", "д")

    # -- progress -----------------------------------------------------------

    def progress_group(self, bars: list["Bar"]) -> "ProgressGroup":
        group = ProgressGroup(self, bars)
        self._progress = group
        return group


@dataclass
class Bar:
    """One progress line. Mutated in place, rendered by its ProgressGroup."""

    label: str
    total: int = 0
    current: int = 0
    unit: str = ""
    suffix: str = ""
    show_rate: bool = True
    width: int = 26
    started: float = field(default_factory=time.monotonic)
    _base: int = 0

    def reset(self, total: int, label: str | None = None, suffix: str = "") -> None:
        self.total = max(0, total)
        self.current = 0
        self.suffix = suffix
        self.started = time.monotonic()
        self._base = 0
        if label is not None:
            self.label = label

    def advance(self, amount: int = 1) -> None:
        self.current = min(self.total, self.current + amount) if self.total else self.current + amount

    @property
    def ratio(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.current / self.total)

    @property
    def eta(self) -> float:
        done = self.current - self._base
        if done <= 0 or self.total <= 0:
            return float("inf")
        elapsed = time.monotonic() - self.started
        rate = done / elapsed if elapsed > 0 else 0
        if rate <= 0:
            return float("inf")
        return (self.total - self.current) / rate

    @property
    def rate(self) -> float:
        done = self.current - self._base
        elapsed = time.monotonic() - self.started
        return done / elapsed if elapsed > 0 and done > 0 else 0.0


class ProgressGroup:
    """A block of bars redrawn in place, plus plain-text mode for pipes/logs."""

    def __init__(self, console: Console, bars: list[Bar]):
        self.console = console
        self.bars = bars
        self.live = False
        self._lines = 0
        self._last_draw = 0.0
        self._last_plain = -1.0
        self._last_key = ""

    # -- lifecycle ---
    def __enter__(self) -> "ProgressGroup":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if not self.console.is_tty or self.console.quiet:
            self.live = self.console.is_tty and not self.console.quiet
            return
        self.live = True
        self.console._safe_write(HIDE_CURSOR)
        self.render(force=True)

    def stop(self, clear: bool = True) -> None:
        if self.live:
            if clear:
                self.clear()
            self.console._safe_write(SHOW_CURSOR)
        self.live = False
        self._lines = 0
        if self.console._progress is self:
            self.console._progress = None

    # -- drawing ---
    def clear(self) -> None:
        if not self.live or self._lines == 0:
            return
        self.console._safe_write(f"{ESC}[{self._lines}F{ESC}[0J")
        self._lines = 0

    def render(self, force: bool = False) -> None:
        if not self.live:
            self._render_plain(force)
            return
        now = time.monotonic()
        if not force and now - self._last_draw < 0.08:  # cap at ~12 fps
            return
        self._last_draw = now
        self.clear()
        out = "".join(self._format(bar) + "\n" for bar in self.bars)
        self.console._safe_write(out)
        self._lines = len(self.bars)

    def _render_plain(self, force: bool) -> None:
        """Fallback for redirected output: one line per 10% of the main bar.

        Indeterminate stages are silent here — in a log file a bar that cannot
        move is just noise, and the pipeline already logs each stage.
        """
        if self.console.quiet or not self.bars:
            return
        main = self.bars[-1]
        if main.total <= 0:
            return
        pct = main.ratio * 100
        key = f"{main.label}{main.suffix}{int(pct // 10)}"
        if key == self._last_key or (not force and pct - self._last_plain < 10):
            return
        self._last_key = key
        self._last_plain = pct
        self.console._safe_write(
            f"  {main.label}: {pct:.0f}% ({main.current}/{main.total}) {main.suffix}\n"
        )

    def _format(self, bar: Bar) -> str:
        c = self.console
        label = c.paint(bar.label.ljust(12)[:12], TEXT)
        meter = self._meter(bar)

        if bar.total > 0:
            pct = f"{bar.ratio * 100:5.1f}%"
            counter = f"{bar.current}/{bar.total}"
        else:
            pct = "  --  "
            counter = str(bar.current)

        parts = [c.paint(pct, ACCENT_SOFT), c.paint(counter, TEXT)]
        if bar.unit:
            parts[-1] = c.paint(f"{counter} {bar.unit}", TEXT)
        if bar.show_rate and bar.total > 0 and bar.current < bar.total:
            parts.append(c.paint(f"ETA {_fmt_eta(bar.eta)}", MUTED))
        if bar.suffix:
            parts.append(c.paint(bar.suffix, MUTED))

        sep = c.paint(" · ", TRACK)
        line = f"  {label} {meter}  " + sep.join(parts)
        # Trim to terminal width so a long filename never wraps and breaks redraw.
        max_w = c.width
        if visible_len(line) > max_w:
            line = self._truncate(line, max_w)
        return line

    def _truncate(self, line: str, max_w: int) -> str:
        out, width = [], 0
        i = 0
        while i < len(line):
            if line[i] == "\x1b":
                match = _ANSI_RE.match(line, i)
                if match:
                    out.append(match.group())
                    i = match.end()
                    continue
            if width >= max_w - 1:
                break
            out.append(line[i])
            width += 1
            i += 1
        return "".join(out) + (RESET if self.console.level else "") + "…"

    def _meter(self, bar: Bar) -> str:
        """The bar itself: accent gradient on filled, dark track behind."""
        c = self.console
        width = bar.width
        filled = int(round(bar.ratio * width))
        if bar.total <= 0:
            filled = 0
        block, track = c.sym.block, c.sym.track

        if c.level == NO_COLOR:
            return "[" + "#" * filled + "-" * (width - filled) + "]"

        if c.level >= TRUECOLOR and filled:
            # Interpolate #FF007F -> #FF7AB7 across the filled section.
            chunks = []
            r0, g0, b0 = ACCENT.rgb
            r1, g1, b1 = ACCENT_SOFT.rgb
            span = max(1, width - 1)
            for i in range(filled):
                t = i / span
                r = int(r0 + (r1 - r0) * t)
                g = int(g0 + (g1 - g0) * t)
                b = int(b0 + (b1 - b0) * t)
                chunks.append(f"{ESC}[38;2;{r};{g};{b}m{block}")
            head = "".join(chunks) + RESET
        else:
            head = c.paint(block * filled, ACCENT) if filled else ""

        tail = c.paint(track * (width - filled), TRACK) if width - filled else ""
        return head + tail
