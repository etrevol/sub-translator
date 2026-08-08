# sub-translator — Automated MKV Subtitle Translator

Extract a subtitle track from your `.mkv` files, translate it, and inject it back
as a new default track — **without re-encoding a single frame**.

Ukrainian by default, 36 languages supported, two translation engines, and a CLI
that tells you what is wrong *before* it spends an hour on it.

*Vibecoded by Artem etrevol Holovashchenko.*

---

## Quick start

```bash
git clone <this repo> && cd sub-translator
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Everything is installed into the project's own `.venv`, so **nothing lands in your
system Python**. Delete the folder and the machine is exactly as it was.

1. Drop your `.mkv` files into `input/`.
2. Run it:

```bash
python subtrans.py
```

3. Collect the results from `output/`.

On Windows you can simply **double-click `run.bat`** — it uses `.venv` if it is
there, runs the defaults and keeps the window open at the end.

Forgot a step? Nothing breaks and nothing gets installed behind your back: the
preflight names the interpreter it is running on and prints the three commands
above.

```
  ▲ interpreter     system python — C:\...\Python312\python.exe
    › no .venv yet; the setup below keeps this project's packages off your system Python
  ✖ google-genai    not installed
```

Not sure the environment is ready? Ask first:

```bash
python subtrans.py doctor
```

---

## The CLI

```
  USAGE
    python subtrans.py [command] [options]

  COMMANDS
    run     Extract, translate and mux every .mkv found in the input folder  (default)
    doctor  Check the environment: tools, key, disk space, network, model
    probe   List the subtitle tracks of a file and show which one would be used
    langs   List the supported languages and their codes
```

`run` is the default, so `python subtrans.py` and `python subtrans.py run` are the
same thing. Every command supports `--help`.

### `run` options

| Option | Meaning |
|---|---|
| `-e, --engine {gemini,google}` | Translation back-end. Default `gemini`. |
| `-l, --lang CODE` | Target language. Default `uk`. See `langs`. |
| `-s, --source CODE` | Preferred source language, or `auto` to pick the best track. |
| `-i, --input PATH` | Folder to scan **or a single `.mkv` file**. Default `input`. |
| `-o, --output PATH` | Where results are written. Default `output`. |
| `-m, --model NAME` | Gemini model. Defaults to `$GEMINI_MODEL`. |
| `-b, --batch-size N` | Subtitles per request. Default 60 (gemini) / 50 (google). |
| `-a, --auto-batch` | Size batches from the real text length — see below. |
| `--delay SEC` | Minimum seconds between requests (rate-limit pacing). |
| `-f, --force` | Reprocess files that already have an output. |
| `-n, --dry-run` | Print the full plan and estimates, touch nothing. |
| `--delete-source` | Delete each original `.mkv` once its output is verified. |
| `--no-keep-srt` | Remove the intermediate `.srt` files after muxing. |
| `--offline-checks` | Skip the network reachability probe. |
| `-y, --yes` | Answer yes to every confirmation prompt. |
| `-q, --quiet` | Errors only. |
| `--no-color` | Plain text, no ANSI. |

### Examples

```bash
python subtrans.py run --engine google           # free, no API key at all
python subtrans.py run --lang pl --auto-batch    # Polish, adaptive batching
python subtrans.py run -i "D:/Movies" -o "D:/Done" --no-keep-srt
python subtrans.py run --dry-run                 # estimate cost and time first
python subtrans.py probe input/movie.mkv         # which track would be used?
```

---

## Two engines

| | `--engine gemini` (default) | `--engine google` |
|---|---|---|
| Quality | Contextual: whole batches at once, keeps tone, idioms and register | Line-by-line machine translation |
| Cost | Free tier of the Gemini API (~15 req/min) | Completely free, no key |
| Speed | Paced at 4s/request to respect the quota | ~1s/request |
| Needs | `GEMINI_API_KEY` | nothing |

### Gemini setup

```bash
cp .env.example .env      # then paste your key
```

```env
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-3.5-flash-lite
```

Get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
`python subtrans.py doctor` verifies the key **and** confirms the configured model
is actually available to it.

### Adaptive batching (`--auto-batch`)

The fixed batch size is a compromise: too small wastes tokens re-sending the prompt
and starves the model of context, too large risks losing a whole batch to one bad
response. `--auto-batch` measures the average subtitle length in the file and picks
the largest batch that still fits a safe request budget (clamped to 20–220 lines).
Dense dialogue gets smaller batches, sparse films get bigger ones.

---

## What it checks before touching anything

Every run starts with a preflight; nothing is extracted until it passes.

- Python version, and which interpreter is running — the project `.venv`, some other
  virtualenv, or your system Python
- Every required package, with the exact command that installs it into the right
  interpreter
- `ffmpeg` **and** `ffprobe` on PATH, with versions
- `.env` present; `GEMINI_API_KEY` present, unquoted, untrimmed, plausible length
- The configured Gemini model exists for your key (`doctor`)
- Network reachability of the translation endpoint
- Input folder exists, is readable, and actually contains `.mkv` files
- Output folder exists and is genuinely writable (real write test, not a guess)
- Free disk space against the **actual** total size of your inputs
- Per file: container parses, has subtitle tracks, and at least one is *text-based*
- After muxing: the output re-probes cleanly, is not truncated, and really contains
  the new track — before anything is deleted

Failures name the file, the reason **and** the fix. Bitmap subtitle tracks
(PGS/VobSub) are reported as unusable instead of silently producing an empty file.

---

## Smart track selection

`probe` shows exactly what the picker sees:

```
    map    codec   type  lang  title           flags
    ──────────────────────────────────────────────────
    0:s:0  subrip  text  eng   Signs & Songs   default
  › 0:s:1  subrip  text  eng   English (Full)  -
  ✔ would translate 0:s:1 — English dialogue track
```

Preference order: your `--source` language without a *signs / songs / karaoke /
forced / commentary* title → any track in that language → the default track that
isn't signs-only → the first full track. Image-based tracks are never chosen.

---

## Resilience

- **Resumes automatically.** A file whose verified output already exists is
  skipped; `--force` overrides.
- **Never shifts your timings.** A translated batch is only accepted when it comes
  back with exactly the same number of blocks. If it doesn't, the batch is retried,
  then split in half and retried again, down to single lines. Whatever still fails
  keeps its original text and is counted in the summary — losing one line is
  recoverable, shifting every subsequent subtitle is not.
- **Rate limits.** HTTP 429 backs off progressively instead of failing.
- **Fatal vs. transient.** A revoked key or a missing model stops the run
  immediately; a flaky batch does not.
- **Encoding.** Extracted subtitles are read as UTF-8, then UTF-8-BOM, CP1251,
  CP1252, Latin-1 — whichever parses.
- **Ctrl-C** leaves every finished file intact and still prints the summary.

## Reclaiming disk space

`--delete-source` removes each original `.mkv` **after** its output has been
verified (probes cleanly, is not smaller than the source, and contains a track in
the target language). It asks for confirmation once at the start; `--yes` skips the
prompt. Without both of those, nothing is ever deleted.

---

## Output layout

```
output/
  The.Expanse.S03E07_UA-sub/
    The.Expanse.S03E07_orig.srt     ← extracted source subtitles
    The.Expanse.S03E07_uk.srt       ← translation
    The.Expanse.S03E07_UA-sub.mkv   ← all original streams + new default UA track
```

The muxed file is a stream copy: identical video, identical audio, every original
subtitle track kept, plus one new track tagged `language=ukr`, titled
`UA - Ukrainian` and marked default.

---

## Requirements

- **Python 3.8+**
- **FFmpeg** on PATH ([download](https://ffmpeg.org/download.html))
- Four packages, installed into `.venv` by `pip install -r requirements.txt`:
  `google-genai`, `pysrt`, `python-dotenv`, `deep-translator`

The tool never installs anything itself and never writes outside the project
folder. `.venv/` is gitignored, so it is yours to delete at any time.

The terminal UI has **no dependencies at all**: colour, tables and progress bars are
plain ANSI, degrading from truecolor → 256 colours → 16 → none, and Unicode → ASCII.
Redirect the output to a file and it turns itself into clean plain-text logs.

## Project structure

```
subtrans.py                            CLI entry point
run.bat                                double-click launcher (Windows)
subcore/
  config.py     defaults, languages, .env loading
  ui.py         colours, tables, progress bars (zero dependencies)
  checks.py     every preflight check
  media.py      ffmpeg / ffprobe: probe, select, extract, mux, verify
  engines.py    Gemini and Google Translate back-ends
  pipeline.py   plan → extract → translate → mux → verify
translator.py                          compat shim → run --engine gemini
Google_Translate_Engine_translator.py  compat shim → run --engine google
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Everything processed (or nothing to do) |
| `1` | At least one file failed |
| `2` | Preflight failed, bad arguments, or the engine is unusable |
| `130` | Interrupted with Ctrl-C |
