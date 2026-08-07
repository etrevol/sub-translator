"""The processing pipeline: probe -> extract -> translate -> mux -> verify."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import checks as checks_mod
from . import config, media
from .engines import Engine, EngineError, build_engine
from .ui import (
    ACCENT, Bar, Console, ERR, MUTED, OK, TEXT, WARN,
    ellipsize, fmt_duration, fmt_size,
)

# Rough subtitles-per-second-of-runtime, used only for --dry-run estimates.
_SUBS_PER_SECOND = 1 / 3.2


@dataclass
class Options:
    engine: str = "gemini"
    target: str = config.DEFAULT_TARGET
    source: str = "auto"
    input: Path = Path(config.DEFAULT_INPUT)
    output: Path = Path(config.DEFAULT_OUTPUT)
    model: str | None = None
    batch_size: int | None = None
    auto_batch: bool = False
    delay: float | None = None
    force: bool = False
    keep_srt: bool = True
    delete_source: bool = False
    dry_run: bool = False
    assume_yes: bool = False
    online_checks: bool = True


@dataclass
class FileResult:
    path: Path
    status: str          # done | skipped | failed
    detail: str = ""
    subtitles: int = 0
    seconds: float = 0.0


@dataclass
class Plan:
    """What a single input file would turn into."""
    source: Path
    out_dir: Path
    orig_srt: Path
    translated_srt: Path
    final_mkv: Path
    stream: media.SubStream | None = None
    reason: str = ""
    skip: str = ""              # non-empty -> nothing to do
    problem: str = ""           # non-empty -> cannot be processed
    hint: str = ""
    duration: float = 0.0
    all_streams: list[media.SubStream] = field(default_factory=list)


def build_plan(path: Path, options: Options) -> Plan:
    tag = config.lang_tag(options.target)
    stem = path.stem
    out_dir = options.output / f"{stem}_{tag}-sub"
    plan = Plan(
        source=path,
        out_dir=out_dir,
        orig_srt=out_dir / f"{stem}_orig.srt",
        translated_srt=out_dir / f"{stem}_{config.normalize_lang(options.target)}.srt",
        final_mkv=out_dir / f"{stem}_{tag}-sub.mkv",
    )

    if f"_{tag}-sub" in path.stem:
        plan.skip = "already an output of this tool"
        return plan
    if plan.final_mkv.exists() and plan.final_mkv.stat().st_size > 0 and not options.force:
        plan.skip = "already processed"
        return plan

    try:
        info = media.probe(path)
    except media.MediaError as exc:
        plan.problem = f"unreadable container: {exc}"
        plan.hint = "the file may be corrupt or still downloading"
        return plan

    plan.duration = media.container_duration(info)
    plan.all_streams = media.subtitle_streams(info)
    if not plan.all_streams:
        plan.problem = "no subtitle tracks at all"
        plan.hint = "this release has no embedded subtitles to translate"
        return plan

    stream, reason = media.select_stream(plan.all_streams, options.source)
    if stream is None:
        codecs = ", ".join(sorted({s.codec for s in plan.all_streams}))
        plan.problem = f"only bitmap subtitles ({codecs})"
        plan.hint = "PGS/VobSub tracks are images and would need OCR, which this tool does not do"
        return plan
    plan.stream, plan.reason = stream, reason
    return plan


def _open_subtitles(path: Path):
    """Load an SRT, tolerating the encodings scene releases actually ship."""
    import pysrt

    last_error: Exception | None = None
    for encoding in ("utf-8", "utf-8-sig", "cp1251", "cp1252", "latin-1"):
        try:
            return pysrt.open(str(path), encoding=encoding)
        except (UnicodeDecodeError, UnicodeError) as exc:
            last_error = exc
    raise last_error or RuntimeError("unreadable subtitle file")


class Pipeline:
    def __init__(self, options: Options, console: Console):
        self.options = options
        self.console = console
        self.results: list[FileResult] = []
        self.deleted_sources = 0

    # -- entry point --------------------------------------------------------

    def run(self) -> int:
        console, options = self.console, self.options
        console.banner()

        try:
            engine = build_engine(
                options.engine, target=options.target, source=options.source,
                **({"model": options.model} if options.engine == "gemini" else {}),
            )
        except EngineError as exc:
            console.error(str(exc))
            return 2
        engine.delay = options.delay if options.delay is not None else (
            config.DEFAULT_DELAY_GEMINI if engine.name == "gemini" else config.DEFAULT_DELAY_GOOGLE
        )

        check_list, files = checks_mod.run_checks(
            engine, options.input, options.output, console,
            delete_source=options.delete_source,
            online=options.online_checks and not options.dry_run,
        )
        checks_mod.report(console, check_list)
        if checks_mod.fatal_count(check_list):
            console.error(
                f"{checks_mod.fatal_count(check_list)} check(s) failed — nothing was processed."
            )
            console.hint("run `python subtrans.py doctor` for the full diagnosis")
            return 2

        plans = [build_plan(path, options) for path in files]
        todo = [p for p in plans if not p.skip and not p.problem]

        self._show_plan(plans, engine)

        if options.dry_run:
            console.blank()
            console.ok("Dry run — nothing was extracted, translated or written.")
            return 0

        if not todo:
            console.blank()
            console.ok("Nothing to do: every input file is already processed or unusable.")
            return 0

        if options.delete_source and not self._confirm_deletion(len(todo)):
            return 1

        try:
            engine.connect()
        except EngineError as exc:
            console.blank()
            console.error(str(exc))
            return 2

        return self._process(todo, engine)

    # -- planning output ----------------------------------------------------

    def _show_plan(self, plans: list[Plan], engine: Engine) -> None:
        console, options = self.console, self.options
        todo = [p for p in plans if not p.skip and not p.problem]

        console.blank()
        console.kv("engine", console.paint(engine.describe(), TEXT))
        console.kv("target", f"{config.lang_info(options.target)[1]} "
                             f"({config.normalize_lang(options.target)})")
        console.kv("source", "auto-detect" if options.source == "auto"
                             else config.lang_info(options.source)[1])
        if options.auto_batch and options.batch_size is None:
            batch = "auto-sized"
        else:
            batch = f"{options.batch_size or self._default_batch(engine)} subtitles/request"
        console.kv("batching", f"{batch}, {engine.delay:g}s between requests")
        console.kv("output", str(options.output))
        if options.delete_source:
            console.kv("cleanup",
                       console.paint("originals are deleted after verification", ACCENT))

        console.blank()
        console.rule(console.paint("Plan", MUTED))
        rows, estimated_subs, estimated_requests = [], 0, 0
        for plan in plans:
            size = fmt_size(plan.source.stat().st_size) if plan.source.exists() else "-"
            if plan.skip:
                state = console.paint(f"{console.sym.info} {plan.skip}", MUTED)
            elif plan.problem:
                state = console.paint(f"{console.sym.fail} {plan.problem}", ERR)
            else:
                subs = int(plan.duration * _SUBS_PER_SECOND) if plan.duration else 0
                estimated_subs += subs
                per_batch = options.batch_size or self._default_batch(engine)
                estimated_requests += max(1, -(-subs // per_batch)) if subs else 1
                counted = f"≈{subs} subs" if subs else "?"
                state = console.paint(
                    f"{console.sym.ok} {plan.reason} · {counted}", OK)
            # Release names run long; the table has to stay inside the rule.
            rows.append([ellipsize(plan.source.name, max(30, console.width - 56)),
                         size, state])
        console.table(["file", "size", "plan"], rows)

        console.blank()
        if todo and estimated_requests:
            eta = estimated_requests * (engine.delay + 1.5)
            console.info(
                f"{len(todo)} file(s) to process · ≈{estimated_subs} subtitles · "
                f"≈{estimated_requests} API requests · ≈{fmt_duration(eta)} at the current pacing"
            )
        for plan in plans:
            if plan.hint:
                console.hint(f"{plan.source.name}: {plan.hint}")

    def _default_batch(self, engine: Engine) -> int:
        return (config.DEFAULT_BATCH_GEMINI if engine.name == "gemini"
                else config.DEFAULT_BATCH_GOOGLE)

    def _confirm_deletion(self, count: int) -> bool:
        console = self.console
        console.blank()
        console.alert(f"--delete-source will permanently delete {count} original .mkv file(s)")
        console.hint("each original is removed only after its output has been verified")
        if self.options.assume_yes:
            console.info("--yes given, continuing")
            return True
        if console.confirm("Continue?", default=False):
            return True
        console.info("Aborted — nothing was deleted.")
        return False

    # -- processing ---------------------------------------------------------

    def _process(self, plans: list[Plan], engine: Engine) -> int:
        console = self.console
        started = time.monotonic()

        files_bar = Bar("Files", total=len(plans), show_rate=False, unit="files")
        work_bar = Bar("Waiting", total=0, unit="subs")
        group = console.progress_group([files_bar, work_bar])

        console.blank()
        console.rule(console.paint("Processing", ACCENT))
        group.start()
        try:
            for plan in plans:
                self._process_one(plan, engine, work_bar, group)
                files_bar.advance()
                group.render(force=True)
        except KeyboardInterrupt:
            group.stop()
            console.blank()
            console.warn("Interrupted — finished files are intact in the output folder.")
            self._summary(engine, time.monotonic() - started)
            return 130
        except EngineError as exc:
            # The back-end itself is unusable (revoked key, missing model):
            # stopping now beats failing the same way on every remaining file.
            group.stop()
            console.blank()
            console.error(f"engine stopped: {exc}")
            console.hint("run `python subtrans.py doctor` to check the key and model")
            self._summary(engine, time.monotonic() - started)
            return 2
        finally:
            group.stop()

        self._summary(engine, time.monotonic() - started)
        return 1 if any(r.status == "failed" for r in self.results) else 0

    def _process_one(self, plan: Plan, engine: Engine, bar: Bar, group) -> None:
        console = self.console
        started = time.monotonic()
        size = fmt_size(plan.source.stat().st_size)
        short = ellipsize(plan.source.name, 28)
        console.write("")
        console.write(f"  {console.paint(console.sym.step, ACCENT)} "
                      f"{console.paint(ellipsize(plan.source.name, console.width - 14), TEXT, bold=True)} "
                      f"{console.paint(size, MUTED)}")
        console.hint(f"track {media.describe_stream(plan.stream)} — {plan.reason}")

        plan.out_dir.mkdir(parents=True, exist_ok=True)

        # 1. extract
        bar.reset(0, label="Extracting", suffix=short)
        group.render(force=True)
        try:
            media.extract_subtitle(plan.source, plan.stream.rel_index, plan.orig_srt)
        except media.MediaError as exc:
            self._fail(plan, f"extraction failed: {exc}", started)
            return

        # 2. load
        try:
            subs = _open_subtitles(plan.orig_srt)
        except Exception as exc:
            self._fail(plan, f"could not parse the extracted subtitles: {exc}", started)
            return
        if len(subs) == 0:
            self._fail(plan, "the extracted subtitle track is empty", started)
            return
        console.ok(f"extracted {len(subs)} subtitles")

        # 3. translate
        texts = [sub.text for sub in subs]
        batch_size = self.options.batch_size
        if batch_size is None:
            batch_size = (engine.suggest_batch_size(texts) if self.options.auto_batch
                          else self._default_batch(engine))
            if self.options.auto_batch:
                console.info(f"auto batch size: {batch_size} subtitles/request "
                             f"(avg line {sum(map(len, texts)) // len(texts)} chars)")

        before_untranslated = engine.untranslated
        bar.reset(len(subs), label="Translating", suffix=short)
        group.render(force=True)
        try:
            for start in range(0, len(subs), batch_size):
                chunk = subs[start:start + batch_size]
                translated = engine.translate([sub.text for sub in chunk])
                for sub, text in zip(chunk, translated):
                    sub.text = text
                bar.advance(len(chunk))
                group.render()
        except EngineError as exc:
            self._fail(plan, str(exc), started)
            raise
        group.render(force=True)

        lost = engine.untranslated - before_untranslated
        try:
            subs.save(str(plan.translated_srt), encoding="utf-8")
        except OSError as exc:
            self._fail(plan, f"could not write the translated subtitles: {exc}", started)
            return
        if lost:
            console.warn(f"translated {len(subs) - lost}/{len(subs)} subtitles "
                         f"({lost} kept in the original language)")
        else:
            console.ok(f"translated {len(subs)} subtitles")

        # 4. mux
        bar.reset(0, label="Muxing", suffix=short)
        group.render(force=True)
        try:
            info = media.probe(plan.source)
            media.inject_subtitle(plan.source, plan.translated_srt, plan.final_mkv,
                                  self.options.target, media.total_stream_count(info))
        except media.MediaError as exc:
            self._fail(plan, f"muxing failed: {exc}", started)
            return

        # 5. verify
        verified, detail = media.verify_output(plan.final_mkv, plan.source, self.options.target)
        if not verified:
            self._fail(plan, f"output failed verification: {detail}", started)
            return
        console.ok(f"muxed {console.paint(plan.final_mkv.name, TEXT)} "
                   f"{console.paint('· verified', MUTED)}")

        # 6. cleanup
        if not self.options.keep_srt:
            for temp in (plan.orig_srt, plan.translated_srt):
                temp.unlink(missing_ok=True)
        if self.options.delete_source:
            try:
                plan.source.unlink()
                self.deleted_sources += 1
                console.info(f"deleted original {plan.source.name} ({size} reclaimed)")
            except OSError as exc:
                console.warn(f"could not delete {plan.source.name}: {exc}")

        self.results.append(FileResult(
            plan.source, "done", subtitles=len(subs), seconds=time.monotonic() - started
        ))

    def _fail(self, plan: Plan, detail: str, started: float) -> None:
        self.console.error(detail)
        self.results.append(
            FileResult(plan.source, "failed", detail, seconds=time.monotonic() - started)
        )

    # -- summary ------------------------------------------------------------

    def _summary(self, engine: Engine, elapsed: float) -> None:
        console = self.console
        done = [r for r in self.results if r.status == "done"]
        failed = [r for r in self.results if r.status == "failed"]

        console.blank()
        console.rule(console.paint("Summary", ACCENT))
        parts = [console.paint(f"{len(done)} done", OK)]
        if failed:
            parts.append(console.paint(f"{len(failed)} failed", ERR))
        if engine.untranslated:
            parts.append(console.paint(f"{engine.untranslated} lines untranslated", WARN))
        console.write("  " + console.paint(" · ".join(parts), TEXT))
        console.kv("subtitles", str(sum(r.subtitles for r in done)))
        console.kv("api requests", str(engine.requests))
        console.kv("elapsed", fmt_duration(elapsed))
        if self.deleted_sources:
            console.kv("originals removed", str(self.deleted_sources))
        console.kv("output", str(self.options.output.resolve()))

        for result in failed:
            console.error(f"{result.path.name}: {result.detail}")
        seen = set()
        for note in engine.notes:
            if note not in seen:
                seen.add(note)
                console.hint(f"{console.sym.info} {note}")
        console.rule()
