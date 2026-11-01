# LARP corpus: from downloaded scripts to text

This code turns a fixed list of 852 Chinese murder-mystery scripts (剧本杀) into
text files. It downloads each script, freezes an exact copy of the files, and
extracts their text. An optional last step estimates how accurate the text is.

The scripts and extracted text are restricted data, so they are not in this
repository. Neither are the book list, share links or API key.

## The steps

| Script | What it does | Main result |
|---|---|---|
| `01_make_book_list.py` | Fixes the list of 852 books and their share links | `books.csv` |
| `02_download.py` | Downloads each book and checks that the files arrived intact | `raw/` |
| `03_snapshot.py` | Freezes the verified books so the inputs can never change | `snapshot/<name>/`, `inputs/` |
| `04_ocr.py` | Extracts text from every readable file | `ocr/<name>/text/` |
| `04b_rolling_ocr.py` | Optional: repeats steps 03 and 04 while a download is running | `rolling_NN` snapshots |
| `05_evaluate.py` | Optional: samples pages for people to correct, then measures the error rate | `evaluation/<name>/` |

`common.py` holds shared settings, `prompt.txt` holds the model's instructions,
and `review_checks.py` holds automated tests that use made-up data.

## Running it

You need a Mac with Python 3.12, LibreOffice, BaiduPCS-Go logged in to the
research account, and about 300 GB of disk space.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -B review_checks.py

python 01_make_book_list.py --scope /path/to/scope.json --manifest /path/to/manifest.csv
python 02_download.py
python 03_snapshot.py --name batch1
python 04_ocr.py --snapshot batch1 --max-cost-cny 1000
python 04_ocr.py --snapshot batch1 --offline
```

Put two settings in a `.env` file in this folder, which Git ignores:

```
DASHSCOPE_API_KEY=your-key
LARP_DATA=/absolute/path/to/data-folder
```

`LARP_DATA` must be outside this folder. It defaults to
`~/LARP_data/replication`. An external disk is needed only if that is where you
point it. Run one command at a time, except `04b_rolling_ocr.py` alongside the
download. Any stopped step can simply be run again and continues where it left off.
The download stops by itself if the Baidu account is no longer reachable: log in and rerun.

**To extract text during a download**, run `python 04b_rolling_ocr.py` instead of
steps 03 and 04. Its estimated spending limit is 1000 CNY across all rounds,
excluding interrupted runs. Its results are working copies: after the download,
take one full snapshot and run step 04 on it. Pages already paid for are reused,
and files that failed are retried. Use that final snapshot for any reported results.

## How text is extracted

| File type | Method | Not captured |
|---|---|---|
| Images, PDFs | Each page is sent to the model `qwen3.7-flash-2026-07-15` | |
| Slides | Converted to PDF, then treated as above | Speaker notes |
| Word | Text is read directly | Images, headers, footers, footnotes |
| Spreadsheets | Saved cell values are read directly | Charts, images |
| Plain text | Read as UTF-8, UTF-16 or GB18030 | Doubtful files are flagged `encoding_needs_review` |

Audio and video are not downloaded. Unsupported files and encrypted or damaged
archives are left out, with the reason recorded in the snapshot's `files.csv`.

Images too tall to send at full resolution are cut into pieces in the blank space between lines. Where no
blank space exists, pieces overlap by 5%, so some text may repeat. These files are
flagged `overlapping_tiles_need_review`.

## Before using the results

- **A finished run is not proof of a complete corpus.** Check the snapshot's
  `summary.json` for books left out and archives that could not be opened, which
  need a person to re-download them or find the password. Check `results.csv` for
  files that `failed`, and `partial` files, which are missing the pages listed as
  `failed_pages`. Retry both with `--redo-failed`; saved pages cost nothing.
- **The text is not guaranteed to match the original.** The model can misread or
  miss words. Only `05_evaluate.py` measures this, and it covers model-read pages
  only. If it has not been run, report that accuracy was not measured.
- **Books are not assembled.** Each source file gets its own text file. Combining
  files into a book, ordering them and removing repeated text are not done here.
- **Costs are estimates.** The limit uses token counts and the prices in
  `common.py`. Requests in progress can exceed it. The provider's bill is final.

## Checking and reproducing

- **Download check:** a file counts as verified if its size and MD5 fingerprint
  match the cloud copy. A mismatch needs a person's decision, recorded in
  `download_decisions.csv` with the file's SHA-256, reviewer, reason and evidence.
- **Offline replay:** `04_ocr.py --offline` rebuilds the text from saved model
  responses, with no model requests or cost. A pass shows that the text can be
  reproduced, not that it is accurate.
- **Run records:** every run saves its code, settings and versions in `runs/`.
  A record still marked `started` did not finish.
- **Changing the method:** changing `04_ocr.py`, the prompt or the model settings
  requires a new snapshot name.

## Evaluation (optional)

```bash
python 05_evaluate.py draw --snapshot batch1 --name eval1 --n 150 --seed 20261020
# Correct evaluation/eval1/corrected/*.txt against images/, and fill in the reviewer column of sample.csv.
python 05_evaluate.py score --name eval1 --seed 1
```

The result is the character error rate: the share of characters that are wrong,
ignoring whitespace. It comes with a 95% interval that resamples whole books. It
describes the completed pages of this corpus, not all such scripts. Files read
directly and files left out need separate assessment.

## Sharing

Keep credentials, share links, scripts, extracted text, model responses and run
records out of Git and public supplements. Without authorised access to the
data, others can run only the automated tests.
