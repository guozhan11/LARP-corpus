# LARP corpus: download, extraction and evaluation

Five scripts implement a fixed study of 852 Chinese murder-mystery scripts
(剧本杀). Read them in the order below; `common.py` holds settings, file helpers
and run records.
Commercial scripts and OCR text are restricted data, not part of this code package.

## Run the study

Use Python 3.12 on macOS, LibreOffice, and BaiduPCS-Go logged into the research
account. macOS supplies `bsdtar`. From this repository's root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export LARP_DATA="/absolute/path/to/private/research-data"
# Set DASHSCOPE_API_KEY, or put it in a .env file here (ignored by Git), for new OCR requests.
# Set SOFFICE only if LibreOffice is installed outside /Applications.
python -B review_checks.py --record /tmp/replication-checks.json

python make_book_list.py --scope /path/to/scope.json --manifest /path/to/manifest.csv
python download.py
python snapshot.py --name batch1
python ocr.py --snapshot batch1
python ocr.py --snapshot batch1 --offline
python evaluate.py draw --snapshot batch1 --name eval1 --n 300 --seed 20261020
# Under "$LARP_DATA/evaluation/eval1/", correct corrected/*.txt against images/.
# Fill the reviewer column in that directory's sample.csv.
python evaluate.py score --name eval1 --seed 1
```

`LARP_DATA` defaults to `~/LARP_data/replication` and must be outside this code
folder. A full run needs roughly 300 GB (downloads without audio/video, about
265 GB, plus archives and text), so point `LARP_DATA` at a disk with that much
room; downloading stops while less than 20 GiB would remain free. If
`LARP_DATA` (or a link to it) points to an external disk under `/Volumes`
that is not mounted, every script stops at once with that message; a new
folder on a mounted disk is simply created. Run one command at a time per data directory; internal worker threads
are supported. New model requests cost money; the estimated cost limit can be
exceeded by in-flight requests or differences in provider pricing.

| Step | Reads | Writes under `LARP_DATA` |
|---|---|---|
| `make_book_list.py` | Fixed selection and link manifest | `books.csv`, once only |
| `download.py` | `books.csv` | `raw/`, `download/<book>.json` |
| `snapshot.py` | Verified downloads | Fixed `inputs/`, `snapshot/<name>/` |
| `ocr.py` | Snapshot | `ocr/<name>/text/`, per-file records, full `results.csv` |
| `ocr.py --offline` | Fixed inputs, conversions, responses | `ocr/<name>_replay/replay_check.json` |
| `evaluate.py` | Completed OCR and human corrections | `evaluation/<name>/`, versioned `scores/` |

## Selection and downloads

The historical selection is 852 titles rated at least 7.0 on 谜圈, one version
per title. This code imports that selection; it does not reconstruct historical
ratings. Reproduction materials must supply fixed selection metadata, its date,
inclusion/exclusion rationale, and authorized access to the source data.

`scope.json` contains `rows` with `title` and
`selected: {"idx": "0001", "folder": "0001_example"}`. The link manifest has
CSV columns `idx,folder,url_with_pwd`, with URLs beginning
`https://pan.baidu.com/s/`. These examples describe the schema, not observations.
The script checks 852 distinct folders. Use a new data directory for a different selection.

Downloads retain folder structure and omit audio/video. Size and content MD5
are checked; completed files are rechecked against recorded SHA-256 before
skipping. A missing/different cloud MD5 yields `checksum_unverified`, not an
assumption of corruption or success. Candidates stay in `download_unverified/`;
replaced inputs stay in `download_replaced/`. Local files that are no longer
in the cloud folder, and audio/video left by older downloads, are moved to
`download_removed/` and listed under `moved_out` in the book record. A share is
saved into the cloud drive again only when its top folder is missing; a listing
error in a subfolder marks the book failed for the next run. A cloud folder
with two names the Mac disk treats as one file (differing only in letter case
or Unicode form) is marked failed with both names, for manual handling. Resuming compares
finished files by size and modification time; snapshots verify SHA-256.

If an independent check verifies a candidate, record its exact bytes in
`download_decisions.csv`: `folder,file,sha256,reviewer,reason,evidence`, then
rerun the download. Evidence must identify the check and accessible evidence;
do not approve all mismatches by default. Decisions are retained in run records.

Snapshots include only books whose download is verified complete and whose
local file list matches the download record (Unicode NFC is used for name
comparison only; original filenames and content SHA-256 checks are preserved);
every other book is listed in the
snapshot's `books.csv` with `included=no` and the reason, and `summary.json`
gives the count. Report both counts. Snapshots freeze input bytes as APFS clones
(independent copies that take no extra space; an ordinary copy elsewhere),
expand archives to three levels, and list each source,
hash, reader or exclusion in `files.csv`. Duplicate ZIP members are rejected;
other archives are checked for duplicate names before extraction without overwriting. Failed/encrypted
archives, unsupported formats, system files, audio/video and empty partial-download
placeholders have recorded exclusion reasons. Original archives remain available.
Review exclusions and counts in `summary.json`: a valid snapshot can still be
incomplete as a research corpus. Retain the fixed `inputs/` directory.

## Extraction method

| Input | Method and limits |
|---|---|
| Images/PDF pages | DashScope `qwen3.7-flash-2026-07-15`, `prompt.txt`, thinking disabled, maximum 8192 output tokens, high-resolution mode; other sampling settings use provider defaults. |
| Page images | EXIF orientation, white background, long side capped at 3200 px, JPEG quality 85. Tall image frames use 5% overlapping tiles; wide images are scaled. |
| Slides | LibreOffice to PDF; hidden slides included, speaker notes excluded. |
| Word | Body paragraphs, tables and text boxes; legacy formats converted to DOCX. Drawing/picture elements are counted as a warning, not an exact image count. Images, headers, footers and footnotes are not read. |
| Spreadsheets | Saved cell values, one line per row; missing formula caches counted. Charts/images omitted. |
| Text | UTF-8, otherwise GB18030. Other encodings, including Big5, need inspection. |

PDF and slide pages are checked while rendering. MuPDF warnings (damaged
objects, missing resources) and repaired file structures are listed per file
with page numbers (page 0 = opening the file) and set `render_needs_review`; non-embedded CID fonts (about
8% of PDFs, usually rendered correctly) are listed under `unembedded_cid_fonts`.
`summary` counts both.

Overlapping tiles retain repeated seam text. Affected files are flagged
`overlapping_tiles_need_review` and counted in the summary. Resolve seams with
recorded human decisions before downstream counting; the code does not guess
which repeated text to delete. `done` means extraction finished, not accuracy approved.

Responses and converted Office files are retained. The exact JPEG sent for a
page is kept only with `--keep-page-images` (about 0.3-0.7 MB per page); any
page can otherwise be re-rendered and checked against its recorded hash, as
`evaluate.py` does. A conversion found without its receipt (an interrupted run)
is moved to `ocr/converted_set_aside/` and redone.
Truncated/rejected responses and request errors are recorded separately.
Per-key locking prevents simultaneous duplicate requests within a run.
`--workers` sets the requests in flight; pages of one long PDF are sent in
parallel. Resume checks source/output hashes (each file read once per run) and
rebuilds missing or changed outputs.
Use `--redo-failed` for failures, `--only 1395` for one book; `results.csv` always
covers the full snapshot. `not_started` means not yet processed, including files
outside `--only` and work paused at the cost limit. Repeat the OCR command to
continue; use `--redo-failed` when retrying recorded failures. Check `results.csv`
before evaluation: a successful command exit alone does not mean every file succeeded.

## Reproduction and evaluation

Each study run creates `runs/<step>_<time>/`: actual source/prompt/requirements
copies in `code/`, explicit input copies in `inputs/`, and `run.json` with
arguments, timestamps, environment/tool versions, hashes and completion summary.
Untracked code is included. A record left at `started` is unfinished; file records
hold individual errors. Retain these records with fixed inputs, conversions,
responses and outputs. Changing OCR code, parameters, prompt or snapshot requires
a new snapshot name; old runs require their saved code and environment. The
recipe binds `ocr.py`, the prompt, model parameters and page-image settings,
not unrelated helpers in `common.py`. Kept responses are reused under a new
snapshot name, so finished pages are not paid for again.

Offline replay covers the whole snapshot, so it cannot use `--only`. It makes
no model requests or Office conversions. It checks every file the original run
finished; files that failed there are listed under `not_done_in_original_run`.
A finished file whose text is missing or changed, a missing response, a failed
replay or a text difference prevents `passed: true` and gives a nonzero exit. This verifies text reproduction, not accuracy.
Fresh model calls are not guaranteed identical; rendering also depends on
library versions. Requirements pin direct Python dependencies; each run also
records the full installed package list and available external-tool versions.

Evaluation samples without replacement using the stated seed from valid,
completed OCR pages/tiles. Its record reports eligible files/pages and unfinished
or invalid OCR files. Direct-text files and excluded/unreadable inputs are outside
this estimate and require separate coverage and extraction-quality assessment.

Review saved images, edit `corrected/*.txt`, and enter reviewer names in
`sample.csv`. Sample identity, images and model text remain fixed. Each score
freezes the human answers and writes new `scores/<run-name>/score.json` and
`per_page.csv`. The run record preserves the full sampling metadata; score reports
summarize it. After corrections, recompute and cite the new score, not an old version.

CER is total character edit distance divided by corrected characters, ignoring
whitespace, with a 2,000-resample percentile-bootstrap interval that resamples
books, since pages of one book are not independent. It describes the eligible
fixed corpus, not all LARP books or failed pages. Small groups, overlapping tiles and reviewer
anchoring on prefilled OCR limit inference. An annotation protocol, independent
quality checks, missing-source assessment and downstream measurement validation
remain necessary.

## Submission and validation scope

Submit this folder and authorized selection metadata. The folder alone does not include `scope.json`, the link manifest,
source files or accepted responses. A live rerun needs authorized share access
and a model credential; offline replay needs the saved private data directory
and the matching code/environment. Keep credentials, share
links, full scripts, OCR text, responses, evaluation excerpts and private run
directories out of Git/public supplements. Provide a data-access statement;
without authorized fixed inputs, only the synthetic tests can be reproduced.

`review_checks.py` groups key checks into sample integrity, extracted text, and
reproducibility/evaluation. They cover download verification, archive/name
collisions, fixed inputs, page order, incomplete results, resumption, offline
replay, human decisions, run records and versioned scores. They do not exhaustively
test CLI help, storage options or performance. `--record` saves the checked code,
dependency versions and log. Tests use no real corpus or live services.
Live downloads, model availability/accuracy, Office fidelity and actual exclusions
still need a documented pilot. Passing tests does not establish publication readiness.
