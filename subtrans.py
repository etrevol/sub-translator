"""sub-translator — the command line interface.

Run with no arguments and it does the obvious thing: translate every .mkv in
`input/` into Ukrainian using the Gemini engine. Everything else is opt-in.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from subcore import checks as checks_mod
from subcore import config, media
from subcore.config import LANGUAGES, VERSION, lang_info, normalize_lang
from subcore.engines import EngineError, build_engine
from subcore.pipeline import Options, Pipeline
from subcore.ui import ACCENT, Console, MUTED, TEXT

COMMANDS = {
    "run": "Extract, translate and mux every .mkv found in the input folder",
    "doctor": "Check the environment: tools, key, disk space, network, model",
    "probe": "List the subtitle tracks of a file and show which one would be used",
    "langs": "List the supported languages and their codes",
}

EXAMPLES = [
    ("python subtrans.py", "translate input/*.mkv into Ukrainian with Gemini"),
    ("python subtrans.py run --engine google", "use free Google Translate, no API key"),
    ("python subtrans.py run --lang pl --auto-batch", "Polish, with an auto-sized batch"),
    ("python subtrans.py run --delete-source --yes", "reclaim disk space as it goes"),
    ("python subtrans.py doctor", "diagnose the setup before a long run"),
    ("python subtrans.py probe input/movie.mkv", "see which track would be translated"),
]


# --- styled argparse --------------------------------------------------------

class StyledParser(argparse.ArgumentParser):
    """argparse for correctness, hand-rendered help for looks."""

    console: Console = None      # injected in main()
    blurb: str = ""
    examples: list[tuple[str, str]] = []

    def print_help(self, file=None) -> None:
        render_help(self, self.console or Console())

    def error(self, message: str):
        console = self.console or Console()
        console.blank()
        console.error(message)
        console.hint(f"try: python {self.prog} --help")
        console.blank()
        raise SystemExit(2)


def _invocation(action: argparse.Action) -> str:
    if not action.option_strings:
        return action.metavar or action.dest
    text = ", ".join(action.option_strings)
    if action.nargs != 0:
        if action.choices:
            text += " {" + ",".join(str(c) for c in action.choices) + "}"
        else:
            text += " " + (action.metavar or action.dest.upper())
    return text


def render_help(parser: StyledParser, console: Console) -> None:
    console.banner()

    console.blank()
    console.write("  " + console.paint("USAGE", ACCENT, bold=True))
    console.write(f"    {parser.format_usage().replace('usage: ', '').strip()}")
    if parser.blurb:
        console.write(f"    {console.paint(parser.blurb, MUTED)}")

    if parser.prog.endswith("subtrans.py"):
        console.blank()
        console.write("  " + console.paint("COMMANDS", ACCENT, bold=True))
        pad = max(len(name) for name in COMMANDS) + 2
        for name, blurb in COMMANDS.items():
            marker = "  " + console.paint("(default)", MUTED) if name == "run" else ""
            console.write(f"    {console.paint(name.ljust(pad), TEXT, bold=True)}"
                          f"{blurb}{marker}")

    for group in parser._action_groups:
        actions = [a for a in group._group_actions
                   if a.help != argparse.SUPPRESS and not isinstance(
                       a, argparse._SubParsersAction)]
        if not actions:
            continue
        title = {"positional arguments": "ARGUMENTS",
                 "options": "OPTIONS",
                 "optional arguments": "OPTIONS"}.get(group.title, group.title.upper())
        console.blank()
        console.write("  " + console.paint(title, ACCENT, bold=True))
        entries = [(_invocation(a), a) for a in actions]
        pad = min(34, max(len(text) for text, _ in entries) + 2)
        for text, action in entries:
            help_text = (action.help or "").strip()
            default = action.default
            if (default not in (None, False, argparse.SUPPRESS)
                    and action.nargs != 0 and "%(default)" not in help_text):
                help_text += console.paint(f"  [{default}]", MUTED)
            if len(text) + 2 > pad:
                console.write(f"    {console.paint(text, TEXT)}")
                console.write(f"    {' ' * pad}{help_text}")
            else:
                console.write(f"    {console.paint(text.ljust(pad), TEXT)}{help_text}")

    if parser.examples:
        console.blank()
        console.write("  " + console.paint("EXAMPLES", ACCENT, bold=True))
        pad = max(len(cmd) for cmd, _ in parser.examples) + 2
        for cmd, note in parser.examples:
            console.write(f"    {console.paint(cmd.ljust(pad), TEXT)}"
                          f"{console.paint(console.sym.info + ' ' + note, MUTED)}")
    console.blank()


# --- parser -----------------------------------------------------------------

def build_parser(console: Console) -> StyledParser:
    StyledParser.console = console

    common = argparse.ArgumentParser(add_help=False)
    globals_group = common.add_argument_group("global options")
    globals_group.add_argument("-h", "--help", action="help",
                               help="show this help and exit")
    globals_group.add_argument("--no-color", action="store_true",
                               help="disable all colour output")
    globals_group.add_argument("-q", "--quiet", action="store_true",
                               help="print nothing but errors")
    globals_group.add_argument("-y", "--yes", action="store_true",
                               help="answer yes to every confirmation prompt")

    parser = StyledParser(prog="subtrans.py", add_help=False, allow_abbrev=False,
                          usage="python subtrans.py [command] [options]")
    parser.blurb = "Extract, translate and re-inject MKV subtitle tracks — losslessly"
    parser.examples = EXAMPLES
    parser.add_argument("-h", "--help", action="help", help="show this help and exit")
    parser.add_argument("-V", "--version", action="version",
                        version=f"sub-translator {VERSION}", help="show the version and exit")

    subparsers = parser.add_subparsers(dest="command")

    # -- run --
    run_p = subparsers.add_parser("run", parents=[common], add_help=False,
                                  help=COMMANDS["run"], allow_abbrev=False,
                                  usage="python subtrans.py [run] [options]")
    run_p.blurb = COMMANDS["run"]
    run_p.examples = EXAMPLES[:4]

    src = run_p.add_argument_group("input & output")
    src.add_argument("-i", "--input", type=Path, default=Path(config.DEFAULT_INPUT),
                     metavar="PATH", help="folder to scan, or a single .mkv file")
    src.add_argument("-o", "--output", type=Path, default=Path(config.DEFAULT_OUTPUT),
                     metavar="PATH", help="folder that receives the results")

    trn = run_p.add_argument_group("translation")
    trn.add_argument("-e", "--engine", choices=("gemini", "google"), default="gemini",
                     help="translation back-end")
    trn.add_argument("-l", "--lang", "--target", dest="lang", default=config.DEFAULT_TARGET,
                     metavar="CODE", help="target language code")
    trn.add_argument("-s", "--source", default="auto", metavar="CODE",
                     help="source language, or 'auto' to pick the best track")
    trn.add_argument("-m", "--model", metavar="NAME",
                     help="Gemini model (default: $GEMINI_MODEL)")
    trn.add_argument("-b", "--batch-size", type=int, metavar="N",
                     help="subtitles per request (default: 60 gemini / 50 google)")
    trn.add_argument("-a", "--auto-batch", action="store_true",
                     help="size batches from the actual text length: more context "
                          "per request, fewer wasted tokens")
    trn.add_argument("--delay", type=float, metavar="SEC",
                     help="minimum seconds between requests (rate-limit pacing)")

    beh = run_p.add_argument_group("behaviour")
    beh.add_argument("-f", "--force", action="store_true",
                     help="reprocess files that already have an output")
    beh.add_argument("-n", "--dry-run", action="store_true",
                     help="show the full plan and estimates, touch nothing")
    beh.add_argument("--delete-source", action="store_true",
                     help="delete each original .mkv once its output is verified")
    beh.add_argument("--no-keep-srt", dest="keep_srt", action="store_false",
                     help="remove the intermediate .srt files after muxing")
    beh.add_argument("--offline-checks", dest="online_checks", action="store_false",
                     help="skip the network reachability check")

    # -- doctor --
    doc_p = subparsers.add_parser("doctor", parents=[common], add_help=False,
                                  help=COMMANDS["doctor"], allow_abbrev=False,
                                  usage="python subtrans.py doctor [options]")
    doc_p.blurb = COMMANDS["doctor"]
    doc_p.examples = [("python subtrans.py doctor", "full check, including a live API call"),
                      ("python subtrans.py doctor --offline", "local checks only")]
    doc_p.add_argument("-e", "--engine", choices=("gemini", "google"), default="gemini",
                       help="engine to validate")
    doc_p.add_argument("-i", "--input", type=Path, default=Path(config.DEFAULT_INPUT),
                       metavar="PATH", help="input folder to validate")
    doc_p.add_argument("-o", "--output", type=Path, default=Path(config.DEFAULT_OUTPUT),
                       metavar="PATH", help="output folder to validate")
    doc_p.add_argument("--offline", action="store_true",
                       help="skip network and API checks")

    # -- probe --
    prb_p = subparsers.add_parser("probe", parents=[common], add_help=False,
                                  help=COMMANDS["probe"], allow_abbrev=False,
                                  usage="python subtrans.py probe [PATH ...] [options]")
    prb_p.blurb = COMMANDS["probe"]
    prb_p.examples = [("python subtrans.py probe input/movie.mkv", "inspect one file"),
                      ("python subtrans.py probe input", "inspect a whole folder")]
    prb_p.add_argument("paths", nargs="*", type=Path, metavar="PATH",
                       help="files or folders to inspect (default: input/)")
    prb_p.add_argument("-s", "--source", default="auto", metavar="CODE",
                       help="preferred source language when picking a track")

    # -- langs --
    lng_p = subparsers.add_parser("langs", parents=[common], add_help=False,
                                  help=COMMANDS["langs"], allow_abbrev=False,
                                  usage="python subtrans.py langs")
    lng_p.blurb = COMMANDS["langs"]

    return parser


# --- commands ---------------------------------------------------------------

def cmd_run(args, console: Console) -> int:
    target = normalize_lang(args.lang)
    if target not in LANGUAGES:
        console.blank()
        console.error(f"unknown target language '{args.lang}'")
        console.hint("run `python subtrans.py langs` to see the supported codes")
        return 2
    if args.batch_size is not None and args.batch_size < 1:
        console.error("--batch-size must be at least 1")
        return 2

    options = Options(
        engine=args.engine, target=target, source=normalize_lang(args.source) or "auto",
        input=args.input, output=args.output, model=args.model,
        batch_size=args.batch_size, auto_batch=args.auto_batch, delay=args.delay,
        force=args.force, keep_srt=args.keep_srt, delete_source=args.delete_source,
        dry_run=args.dry_run, assume_yes=args.yes, online_checks=args.online_checks,
    )
    return Pipeline(options, console).run()


def cmd_doctor(args, console: Console) -> int:
    console.banner()
    try:
        engine = build_engine(args.engine)
    except EngineError as exc:
        console.error(str(exc))
        return 2

    check_list, files = checks_mod.run_checks(
        engine, args.input, args.output, console,
        online=not args.offline, deep=not args.offline, include_terminal=True,
    )
    checks_mod.report(console, check_list, title="Diagnosis")

    failures = checks_mod.fatal_count(check_list)
    warnings = checks_mod.warn_count(check_list)
    if checks_mod.needs_setup(check_list) or not config.venv_exists():
        checks_mod.setup_block(console)
    console.blank()
    if failures:
        console.error(f"{failures} problem(s) must be fixed before a run.")
    elif warnings:
        console.warn(f"Ready to run, with {warnings} warning(s).")
    else:
        console.ok("Everything checks out — you are ready to run.")
    if files:
        console.hint(f"{len(files)} file(s) waiting in {args.input}")
    console.blank()
    return 2 if failures else 0


def _collect_media(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(path.glob("*.mkv")))
        elif path.is_file():
            found.append(path)
    return found


def cmd_probe(args, console: Console) -> int:
    console.banner()
    paths = args.paths or [Path(config.DEFAULT_INPUT)]
    files = _collect_media(paths)
    if not files:
        console.blank()
        console.error(f"no .mkv files found in: {', '.join(str(p) for p in paths)}")
        return 2

    exit_code = 0
    for path in files:
        console.blank()
        console.write(f"  {console.paint(console.sym.step, ACCENT)} "
                      f"{console.paint(path.name, TEXT, bold=True)}")
        try:
            info = media.probe(path)
        except media.MediaError as exc:
            console.error(str(exc))
            exit_code = 1
            continue

        streams = media.subtitle_streams(info)
        if not streams:
            console.warn("no subtitle tracks in this file")
            exit_code = 1
            continue

        chosen, reason = media.select_stream(streams, args.source)
        rows = []
        for stream in streams:
            kind = "text" if stream.is_text else ("image" if stream.is_image else "?")
            flags = " ".join(f for f, on in (("default", stream.default),
                                             ("forced", stream.forced)) if on)
            rows.append([
                f"0:s:{stream.rel_index}", stream.codec, kind,
                stream.language or "-", (stream.title or "-")[:34], flags or "-",
            ])
        highlight = chosen.rel_index if chosen else None
        console.table(["map", "codec", "type", "lang", "title", "flags"], rows,
                      highlight=highlight)
        if chosen:
            console.ok(f"would translate 0:s:{chosen.rel_index} — {reason}")
        else:
            codecs = ", ".join(sorted({s.codec for s in streams}))
            console.error(f"nothing usable: only bitmap subtitles ({codecs})")
            console.hint("PGS/VobSub tracks are images and would need OCR")
            exit_code = 1
    console.blank()
    return exit_code


def cmd_langs(args, console: Console) -> int:
    console.banner()
    console.blank()
    rows = [[code, iso3, english, native]
            for code, (iso3, english, native) in sorted(LANGUAGES.items())]
    default_row = next(i for i, r in enumerate(rows) if r[0] == config.DEFAULT_TARGET)
    console.table(["code", "iso-3", "language", "native"], rows, highlight=default_row)
    console.blank()
    console.info(f"default target: {lang_info(config.DEFAULT_TARGET)[1]} "
                 f"({config.DEFAULT_TARGET}) — override with --lang")
    console.info("any other ISO 639-1 code is accepted too; the table lists the tested ones")
    console.blank()
    return 0


# --- entry point ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Make stdout able to carry the block glyphs on legacy Windows consoles.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    config.load_env()

    # Bare `python subtrans.py`, or options without a command, mean "run".
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help",
                                                               "-V", "--version")):
        argv.insert(0, "run")

    console = Console(color="never" if "--no-color" in argv else "auto")
    parser = build_parser(console)
    args = parser.parse_args(argv)

    console.quiet = getattr(args, "quiet", False)

    handlers = {"run": cmd_run, "doctor": cmd_doctor, "probe": cmd_probe, "langs": cmd_langs}
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return handler(args, console)
    except KeyboardInterrupt:
        console.blank()
        console.warn("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
