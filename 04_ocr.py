"""Extract text from a fixed snapshot; see README.md for reader limitations.

Images/PDFs use OCR, slides use LibreOffice then OCR, and Word/sheet/text
files use direct readers. Keep page responses and conversion intermediates.
Output: text/<source>.txt (pages separated by form feed), files/<id>.json,
and a full-snapshot results.csv. Offline replay compares every text file.

python 04_ocr.py --snapshot batch1 [--only 1395] [--redo-failed]
python 04_ocr.py --snapshot batch1 --offline
"""
import argparse
import common
import base64
import concurrent.futures
import concurrent.futures.process
import hashlib
import io
import json
import multiprocessing
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree
import fitz
import openpyxl
import pillow_heif
from PIL import Image, ImageOps
from common import (API_URL, DATA, HERE, JPEG_QUALITY, LONG_SIDE, MODEL, PARAMETERS, PRICE_CNY_PER_MILLION, PROMPT,
                    SOFFICE, TALL_RATIO, TILE_OVERLAP, env_setting, now, read_json, record_run, read_csv, write_csv, finish_run, sha256, write_json)

pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = None   # large scans are expected; the inputs are the downloaded scripts
PROMPT_SHA256 = hashlib.sha256(PROMPT.encode('utf-8')).hexdigest()
RESPONSES = DATA / 'ocr' / 'responses'
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
# LibreOffice leaves hidden slides out of a PDF unless told otherwise.
SLIDES_FILTER = ':impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}'
# One lock per page key: a page is never requested twice at once, and unrelated
# pages never wait for each other (a fixed set of shared locks made them queue).
_KEY_LOCKS, _KEY_LOCKS_GUARD = {}, threading.Lock()
CONVERT_LOCK = threading.Lock()
fitz.TOOLS.mupdf_display_errors(False)   # kept per file in its record instead of printed
MC_FALLBACK = '{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback'


class LimitReached(Exception):
    """The run's cost limit: the file is left for the next run, not marked failed."""


class Budget:
    def __init__(self, limit_cny, offline, keep_page_images=False):
        self.limit, self.offline, self.spent, self.lock = limit_cny, offline, 0.0, threading.Lock()
        self.keep_page_images = keep_page_images
        self.receipts = set()  # responses touched in this run, for output hashes


# ---------- page images ----------

def flat_rgb(im):
    if im.mode in ('RGBA', 'LA', 'PA') or (im.mode == 'P' and 'transparency' in im.info):
        rgba = im.convert('RGBA')
        flat = Image.new('RGB', rgba.size, 'white')
        flat.paste(rgba, mask=rgba.getchannel('A'))
        im = flat
    return im.convert('RGB')


def jpeg(im):
    im = flat_rgb(im)
    im.thumbnail((LONG_SIDE, LONG_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.save(out, format='JPEG', quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


SEAM_SEARCH = 0.3    # look this share of a tile above its bottom for a blank band
BLANK_RANGE = 48     # gray levels from a row's background still blank (JPEG noise around text)
BLANK_BAND = 0.004   # of the width: the thinnest gap between lines to cut in


def blank_row(gray, y):
    counts = gray.crop((0, y, gray.width, y + 1)).histogram()
    background = counts.index(max(counts))
    return sum(counts[max(background - BLANK_RANGE, 0):background + BLANK_RANGE + 1]) == gray.width


def blank_cut(gray, bottom, lowest):
    """The row nearest above `bottom` in the middle of a blank band, or None."""
    band = max(3, int(BLANK_BAND * gray.width))
    run = 0
    for y in range(bottom - 1, lowest - 1, -1):
        run = run + 1 if blank_row(gray, y) else 0
        if run == band:
            return y + band // 2
    return None


def tall_tiles(im, notes):
    """Cut a tall frame in blank bands between lines, so no text repeats or splits.
    Where no band is found, the seam overlaps and is flagged for review."""
    im = flat_rgb(im)
    gray = im.convert('L')
    tile = int(TALL_RATIO * im.width)
    tiles, top = [], 0
    while top + tile < im.height:
        cut = blank_cut(gray, top + tile, top + int(tile * (1 - SEAM_SEARCH)))
        tiles.append(jpeg(im.crop((0, top, im.width, cut or top + tile))))
        top = cut or top + int(tile * (1 - TILE_OVERLAP))
        if not cut:
            notes['overlapping_tiles_need_review'] = True
    tiles.append(jpeg(im.crop((0, top, im.width, im.height))))
    return tiles


def image_pages(path, notes=None):
    """JPEG bytes for each frame, tall frames cut into tiles."""
    notes = {} if notes is None else notes
    pages = []
    with Image.open(path) as opened:
        for frame in range(getattr(opened, 'n_frames', 1)):
            opened.seek(frame)
            im = ImageOps.exif_transpose(opened.copy())
            if im.height <= max(TALL_RATIO * im.width, LONG_SIDE):   # sent whole, at full resolution
                pages.append(jpeg(im))
            else:
                pages += tall_tiles(im, notes)
    return pages


# MuPDF is not safe across threads: sharing it crashed a real run with memory
# corruption while a document was being freed, which a lock cannot prevent. All
# MuPDF work therefore happens in separate single-threaded renderer processes.
RENDER_PROCESSES = 4
_renderer, _renderer_guard = None, threading.Lock()
_open_pdf = (None, None)   # per renderer process: the document being read


def pdf_info(path):
    """Renderer process: (password needed, pages, warnings from opening the file)."""
    fitz.TOOLS.reset_mupdf_warnings()
    doc = fitz.open(path)
    try:
        return doc.needs_pass, doc.page_count, fitz.TOOLS.mupdf_warnings()
    finally:
        doc.close()


def render_page(path, number):
    """Renderer process: (JPEG, warnings, fonts, repaired) for page `number` (from 1)."""
    global _open_pdf
    key = (str(path), Path(path).stat().st_mtime_ns)
    if _open_pdf[0] != key:
        if _open_pdf[1] is not None:
            _open_pdf[1].close()
        _open_pdf = (key, fitz.open(path))
    doc = _open_pdf[1]
    fitz.TOOLS.reset_mupdf_warnings()
    page = doc[number - 1]
    scale = LONG_SIDE / max(page.rect.width, page.rect.height)
    png = page.get_pixmap(matrix=fitz.Matrix(scale, scale)).tobytes('png')
    fonts = [(f[1], f[2], f[3]) for f in page.get_fonts()]
    warnings = fitz.TOOLS.mupdf_warnings()
    with Image.open(io.BytesIO(png)) as im:
        return jpeg(im), warnings, fonts, doc.is_repaired


def in_renderer(function, *args):
    """Run in a renderer process. If one dies (a damaged PDF can crash MuPDF), start
    new ones and try once more, so only the file that keeps crashing is affected."""
    global _renderer
    for attempt in (1, 2):
        with _renderer_guard:
            if _renderer is None:
                _renderer = concurrent.futures.ProcessPoolExecutor(
                    RENDER_PROCESSES, mp_context=multiprocessing.get_context('spawn'))
            pool = _renderer
        try:
            return pool.submit(function, *args).result()
        except concurrent.futures.process.BrokenProcessPool:
            with _renderer_guard:
                if _renderer is pool:
                    _renderer = None
    raise ValueError('PDF renderer crashed on this file')


def pdf_pages(path, notes=None):
    """JPEG bytes per page, produced one page at a time (long PDFs).

    With `notes` (a dict), records what may make a page image differ from the
    printed page: MuPDF warnings (damaged objects, missing resources; page 0 =
    opening the file), non-embedded CID fonts (substituted glyphs) and a repaired
    file structure."""
    def note(kind, what, number):
        if notes is not None:
            # MuPDF messages can quote broken strings from the file as lone surrogates,
            # which cannot be written as UTF-8; keep them as visible escapes.
            what = what.encode('utf-8', 'backslashreplace').decode('utf-8')
            pages = notes.setdefault(kind, {}).setdefault(what, [])
            if number not in pages:
                pages.append(number)

    locked, count, opened = in_renderer(pdf_info, str(path))
    if locked:
        raise ValueError('password-protected PDF')
    for line in filter(None, opened.splitlines()):
        note('render_warnings', line[:200], 0)
    for number in range(1, count + 1):
        image, warnings, fonts, repaired = in_renderer(render_page, str(path), number)
        for line in filter(None, warnings.splitlines()):
            note('render_warnings', line[:200], number)
        for extension, font_type, name in fonts:
            if extension == 'n/a' and font_type == 'Type0':
                note('unembedded_cid_fonts', name, number)
        if repaired and notes is not None:
            notes['repaired_file_structure'] = True
        yield image


def convert(path, target_format, keep_as, export_filter='', offline=False):
    """Serialize conversion and verify the saved intermediate before reuse."""
    with CONVERT_LOCK:
        receipt = keep_as.with_suffix(keep_as.suffix + '.json')
        saved = read_json(receipt, {})
        if (keep_as.exists() and saved.get('sha256') == sha256(keep_as)
                and saved.get('source_sha256') == sha256(path)
                and saved.get('format') == target_format and saved.get('filter') == export_filter):
            return keep_as
        if offline:
            raise ValueError('conversion missing or changed; restore the fixed intermediate')
        if keep_as.exists():
            # No matching receipt: a conversion interrupted before its receipt, or a
            # changed file. Keep it aside as evidence and convert again.
            aside = DATA / 'ocr' / 'converted_set_aside' / f"{keep_as.name}_{now().replace(':', '')}"
            aside.parent.mkdir(parents=True, exist_ok=True)
            keep_as.replace(aside)
            if receipt.exists():
                receipt.replace(aside.with_name(aside.name + '.json'))
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / ('input' + path.suffix.lower())
            shutil.copy2(path, src)
            subprocess.run([SOFFICE, f'-env:UserInstallation={Path(tmp, "profile").as_uri()}', '--headless',
                            '--convert-to', target_format + export_filter, '--outdir', tmp, str(src)],
                           capture_output=True, timeout=600, check=True)
            result = Path(tmp) / f'input.{target_format}'
            if not result.is_file():
                raise ValueError(f'LibreOffice produced no {target_format}')
            keep_as.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(result, keep_as)
        write_json(receipt, {'sha256': sha256(keep_as), 'source_sha256': sha256(path),
                             'format': target_format, 'filter': export_filter, 'created': now()})
        return keep_as


# ---------- direct text ----------

def docx_text(path):
    """Paragraphs in document order, including tables and text boxes. Text-box
    content also stored in a compatibility copy (mc:Fallback) is read once."""
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read('word/document.xml'))
    skip = {id(e) for fb in root.iter(MC_FALLBACK) for e in fb.iter()}
    lines, images = [], sum(1 for _ in root.iter(W + 'drawing')) + sum(1 for _ in root.iter(W + 'pict'))

    def own_text(node, parts):
        # A text box sits inside a paragraph but holds paragraphs of its own;
        # those are read as separate lines, not again as part of this one.
        for child in node:
            if child.tag in (W + 'txbxContent', MC_FALLBACK):
                continue
            if child.tag == W + 't':
                parts.append(child.text or '')
            elif child.tag == W + 'tab':
                parts.append('\t')
            elif child.tag in (W + 'br', W + 'cr'):
                parts.append('\n')
            own_text(child, parts)
        return parts

    for p in root.iter(W + 'p'):
        if id(p) not in skip:
            lines.append(''.join(own_text(p, [])))
    return '\n'.join(lines).strip('\n'), {'embedded_images_not_read': images}


def xlsx_text(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    blocks, uncached = [], 0
    try:
        formulas = openpyxl.load_workbook(path, read_only=True, data_only=False)
        for sheet in wb.worksheets:
            lines = [f'## {sheet.title}']
            for values, cells in zip(sheet.iter_rows(values_only=True), formulas[sheet.title].iter_rows(values_only=True)):
                uncached += sum(v is None and isinstance(c, str) and c.startswith('=') for v, c in zip(values, cells))
                if any(v is not None and str(v).strip() for v in values):
                    lines.append('\t'.join('' if v is None else str(v) for v in values).rstrip('\t'))
            blocks.append('\n'.join(lines))
        formulas.close()
    finally:
        wb.close()
    return '\n\n'.join(blocks), {'formulas_without_saved_value': uncached}


def plain_text(path):
    raw = path.read_bytes()
    encodings = ('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'gb18030')
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        info = {'encoding': encoding}
        if encoding == 'gb18030':
            # Big5 and others can decode here without error, as rare characters: flag, do not guess.
            wide = ''.join(c for c in text if ord(c) > 127)
            info['encoding_needs_review'] = len(wide.encode('gb2312', 'ignore')) < 1.8 * len(wide)
        return text, info
    raise ValueError(f'text is not {" or ".join(encodings)}')


# ---------- model ----------

def api_key():
    return env_setting('DASHSCOPE_API_KEY')


def response_text(response):
    """Use the same acceptance rule for fresh and cached model responses."""
    choice = (response.get('choices') or [{}])[0]
    text = choice.get('message', {}).get('content')
    if response.get('model') != MODEL or choice.get('finish_reason') != 'stop' or not isinstance(text, str):
        raise ValueError(f"model response not accepted (finish_reason={choice.get('finish_reason')})")
    return text


def key_lock(key):
    with _KEY_LOCKS_GUARD:   # creating the lock is itself guarded
        return _KEY_LOCKS.setdefault(key, threading.Lock())


def transcribe(image, budget):
    """Model text for one page image, from the kept responses when possible."""
    image_sha = hashlib.sha256(image).hexdigest()
    key = hashlib.sha256(json.dumps([API_URL, image_sha, PROMPT_SHA256, PARAMETERS], sort_keys=True).encode()).hexdigest()
    with key_lock(key):
        result = transcribe_locked(image, image_sha, key, budget)
        with budget.lock:
            budget.receipts.add(RESPONSES / f'{key}.json')
        return result


def transcribe_locked(image, image_sha, key, budget):
    kept = read_json(RESPONSES / f'{key}.json')
    if kept:
        if (kept['image_sha256'] != image_sha or kept['prompt_sha256'] != PROMPT_SHA256
                or kept['parameters'] != PARAMETERS):
            raise ValueError('invalid cached response')
        return response_text(kept['response']), image_sha
    if budget.offline:
        raise ValueError('no kept response for this page (offline)')
    with budget.lock:
        if budget.spent >= budget.limit:
            raise LimitReached()
    body = {**PARAMETERS, 'messages': [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(image).decode()}},
        {'type': 'text', 'text': PROMPT}]}]}
    RESPONSES.mkdir(parents=True, exist_ok=True)
    if budget.keep_page_images:   # the page can always be re-rendered and checked against image_sha256
        (RESPONSES / f'{key}.jpg').write_bytes(image)
    token = api_key()
    if not token:
        raise ValueError('DASHSCOPE_API_KEY is missing')
    request = urllib.request.Request(API_URL, data=json.dumps(body).encode(),
                                     headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=300) as r:
                raw = r.read()
            try:
                response = json.loads(raw)
            except (ValueError, UnicodeError):
                failed = DATA / 'ocr' / 'request_errors' / f'{key}_{now().replace(":", "")}.bin'
                failed.parent.mkdir(parents=True, exist_ok=True)
                failed.write_bytes(raw)
                with budget.lock:
                    budget.receipts.add(failed)
                raise ValueError('model returned invalid JSON; raw response retained')
            break
        except OSError as exc:   # preserve failures separately; never cache them as accepted responses
            code = getattr(exc, 'code', None)
            error_body = exc.read().decode('utf-8', errors='replace') if isinstance(exc, urllib.error.HTTPError) else str(exc)
            failed = DATA / 'ocr' / 'request_errors' / f'{key}_{now().replace(":", "")}.json'
            write_json(failed, {'time': now(), 'image_sha256': image_sha, 'parameters': PARAMETERS,
                                'prompt_sha256': PROMPT_SHA256, 'attempt': attempt + 1, 'status': code, 'body': error_body})
            with budget.lock:
                budget.receipts.add(failed)
            if attempt == 3 or (code is not None and code not in (429, 500, 502, 503, 504)):
                raise ValueError(f'request failed: {code or type(exc).__name__}') from exc
            time.sleep(30 * (attempt + 1))
    record = {'received': now(), 'api_url': API_URL, 'image_sha256': image_sha,
              'prompt_sha256': PROMPT_SHA256, 'parameters': PARAMETERS, 'response': response}
    rejected = DATA / 'ocr' / 'responses_rejected' / f'{key}_{now().replace(":", "")}.json'
    write_json(rejected, record)  # preserve even malformed responses before inspecting them
    with budget.lock:
        budget.receipts.add(rejected)
    usage = response.get('usage', {})
    cost = (usage.get('prompt_tokens', 0) * PRICE_CNY_PER_MILLION['input']
            + usage.get('completion_tokens', 0) * PRICE_CNY_PER_MILLION['output']) / 1e6
    with budget.lock:
        budget.spent += cost
    text = response_text(response)
    rejected.replace(RESPONSES / f'{key}.json')  # promote the already saved response
    with budget.lock:
        budget.receipts.discard(rejected)
    return text, image_sha


# A long PDF used to be sent one page at a time by a single file worker. Pages
# now go to one shared pool (its size = --workers = requests in flight), in
# small batches so only a few rendered pages are held in memory at once.
PAGE_POOL, PAGE_BATCH = None, 8


def page_result(image, budget):
    """(text, image hash, error): one failed page must not lose the file's other pages."""
    try:
        return (*transcribe(image, budget), None)
    except LimitReached:
        raise
    except Exception as exc:
        return '', hashlib.sha256(image).hexdigest(), f'{type(exc).__name__}: {exc}'


def transcribe_all(images, budget):
    if PAGE_POOL is None:
        return [page_result(image, budget) for image in images]
    results, batch = [], []
    for image in images:
        batch.append(PAGE_POOL.submit(page_result, image, budget))
        if len(batch) >= PAGE_BATCH:
            results += [f.result() for f in batch]
            batch = []
    return results + [f.result() for f in batch]


# ---------- one file ----------

def process(row, out, budget):
    path = DATA / row['path']
    record = {'source': row['source'], 'kind': row['kind'], 'source_sha256': row['sha256']}
    try:
        if hashed(path) != row['sha256']:
            raise ValueError('file differs from the snapshot')
        kept = DATA / 'ocr' / 'converted' / row['sha256']
        notes = {}
        if row['kind'] in ('image', 'pdf', 'slides'):
            if row['kind'] == 'image':
                images, method = image_pages(path, notes), 'ocr_image'
            elif row['kind'] == 'pdf':
                images, method = pdf_pages(path, notes), 'ocr_pdf'
            else:
                images, method = pdf_pages(convert(path, 'pdf', kept.with_suffix('.pdf'), SLIDES_FILTER, budget.offline),
                                           notes), 'libreoffice_pdf_then_ocr'
            pages = transcribe_all(images, budget)
            failed = [(number, error) for number, (_, _, error) in enumerate(pages, 1) if error]
            if failed and len(failed) == len(pages):
                raise ValueError(failed[0][1])
            text = '\f'.join(t for t, _, _ in pages)
            record.update(method=method, pages=len(pages), page_image_sha256=[h for _, h, _ in pages], **notes)
            if failed:
                record.update(failed_pages=[number for number, _ in failed], error=failed[0][1][:300])
            # Warnings and repairs mean the page image may not match the print; unembedded
            # fonts alone are recorded (about 8% of PDFs) but usually render correctly.
            record['render_needs_review'] = bool(notes.get('render_warnings') or notes.get('repaired_file_structure'))
        else:
            if row['kind'] in ('word', 'sheet'):
                extension, reader = ('docx', docx_text) if row['kind'] == 'word' else ('xlsx', xlsx_text)
                source = path if path.suffix.lower() == '.' + extension else convert(
                    path, extension, kept.with_suffix('.' + extension), offline=budget.offline)
                text, info = reader(source)
            else:
                text, info = plain_text(path)
            record.update(method=f"direct_{row['kind']}", pages=1, **info)
        target = out / 'text' / (row['source'] + '.txt')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        record.update(status='partial' if record.get('failed_pages') else 'done', chars=len(text), text_sha256=sha256(target))
    except LimitReached:
        record.update(status='not_started')
    except Exception as exc:   # one unreadable file must not stop the run; the error is recorded
        record.update(status='failed', error=f'{type(exc).__name__}: {exc}'[:300])
    record['finished'] = now()
    write_json(file_record(out, row), record)
    return record


_hashes = {}


def hashed(path):
    """SHA-256 once per run for an unchanged file (same size and modification time)."""
    stat = Path(path).stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _hashes:
        _hashes[key] = sha256(path)
    return _hashes[key]


def file_record(out, row):
    return out / 'files' / (hashlib.sha256(row['source'].encode()).hexdigest() + '.json')


def valid_done(row, out, record):
    text = out / 'text' / (row['source'] + '.txt')
    source = DATA / row['path']
    return (record.get('status') == 'done' and record.get('source_sha256') == row['sha256']
            and source.is_file() and hashed(source) == row['sha256']
            and text.is_file() and sha256(text) == record.get('text_sha256'))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--snapshot', required=True)
    p.add_argument('--only', nargs='*', help='book numbers (idx) to process; summary still covers the full snapshot')
    p.add_argument('--offline', action='store_true', help='replay all files; no model requests or conversions')
    p.add_argument('--redo-failed', action='store_true')
    p.add_argument('--max-cost-cny', type=float, default=50.0, help='estimated limit; in-flight requests may exceed it')
    p.add_argument('--workers', type=int, default=4, help='model requests in flight (and files read) at once')
    p.add_argument('--keep-page-images', action='store_true',
                   help='also keep every JPEG sent (about 0.3-0.7 MB per page); pages can otherwise be re-rendered')
    a = p.parse_args()
    common.require_data_disk()
    if a.workers < 1 or a.max_cost_cny < 0:
        p.error('workers must be positive and cost must be nonnegative')
    if a.offline and a.only:
        p.error('offline verification must cover the whole snapshot; omit --only')
    snapshot = DATA / 'snapshot' / a.snapshot
    expected = read_json(snapshot / 'summary.json')
    if sha256(snapshot / 'files.csv') != expected['files_csv_sha256']:
        raise SystemExit('snapshot manifest changed')
    rows = [r for r in read_csv(snapshot / 'files.csv') if r['kind'] not in ('excluded', 'archive')]
    original = DATA / 'ocr' / a.snapshot
    out = DATA / 'ocr' / (a.snapshot + ('_replay' if a.offline else ''))
    out.mkdir(parents=True, exist_ok=True)
    recipe = {'api_url': API_URL, 'snapshot_sha256': sha256(snapshot / 'files.csv'), 'parameters': PARAMETERS,
              'prompt_sha256': PROMPT_SHA256, 'ocr_code_sha256': sha256(HERE / '04_ocr.py'),
              # Only the settings in common.py that shape the text; its other helpers may change.
              'render': {'long_side': LONG_SIDE, 'jpeg_quality': JPEG_QUALITY,
                         'tall_ratio': TALL_RATIO, 'tile_overlap': TILE_OVERLAP}}
    saved = read_json(original / 'recipe.json')
    if saved != recipe:
        if saved or a.offline or (original / 'files').exists():
            raise SystemExit('OCR recipe changed or missing; restore the recorded 04_ocr.py/settings, or run under a '
                             'new snapshot name (kept model responses are reused, so finished pages cost nothing)')
        write_json(original / 'recipe.json', recipe)
    run = record_run('ocr_offline' if a.offline else 'ocr', vars(a),
                     [snapshot / 'files.csv', snapshot / 'summary.json', original / 'recipe.json'])
    budget = Budget(a.max_cost_cny, a.offline, a.keep_page_images)
    selected = [r for r in rows if not a.only or r['book'][:4] in {x.zfill(4) for x in a.only}]

    def todo(row):
        done = read_json(file_record(out, row), {})
        if a.offline or not done or done['status'] == 'not_started':
            return True
        if done['status'] == 'done':
            return not valid_done(row, out, done)
        return a.redo_failed

    global PAGE_POOL, PAGE_BATCH
    PAGE_BATCH = 2 * a.workers
    with concurrent.futures.ThreadPoolExecutor(a.workers) as pages, \
            concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
        PAGE_POOL = pages
        for r in pool.map(lambda row: process(row, out, budget), [r for r in selected if todo(r)]):
            print(r['status'], r['source'], r.get('error', ''), flush=True)
    PAGE_POOL = None
    results = []
    for row in rows:
        record = read_json(file_record(out, row)) or {'source': row['source'], 'kind': row['kind'], 'status': 'not_started'}
        if record['status'] == 'done' and not valid_done(row, out, record):
            record = {**record, 'status': 'invalid', 'error': 'source or output missing/changed'}
        results.append(record)
    fields = ['source', 'kind', 'method', 'status', 'pages', 'chars', 'source_sha256', 'text_sha256', 'failed_pages', 'error']
    write_csv(out / 'results.csv', results, fields)
    counts = {s: sum(r['status'] == s for r in results) for s in sorted({r['status'] for r in results})}
    summary = {'files': counts,
               'overlapping_tile_files_needing_review': sum(bool(r.get('overlapping_tiles_need_review')) for r in results),
               'text_files_with_encoding_to_review': sum(bool(r.get('encoding_needs_review')) for r in results),
               'pdf_files_with_render_warnings': sum(bool(r.get('render_needs_review')) for r in results),
               'pdf_files_with_unembedded_cid_fonts': sum(bool(r.get('unembedded_cid_fonts')) for r in results), 'new_cost_cny_estimate': round(budget.spent, 2), 'run_record': str(run)}
    outputs = [out / 'results.csv', *sorted((out / 'files').glob('*.json')),
               *sorted((out / 'text').rglob('*.txt'))]
    if a.offline:
        # Only text the original run produced can be reproduced; files that failed
        # there (e.g. a password-protected PDF) are reported, not counted as replay failures.
        differ, unavailable, not_done_originally = [], [], []
        for row, replay in zip(rows, results):
            before = read_json(file_record(original, row), {})
            if before.get('status') != 'done':
                not_done_originally.append(row['source'])
            elif not valid_done(row, original, before) or replay['status'] != 'done':
                # recorded as done, but its text is gone/changed or cannot be rebuilt
                unavailable.append(row['source'])
            elif before['text_sha256'] != replay['text_sha256']:
                differ.append(row['source'])
        checked = len(rows) - len(not_done_originally)
        summary.update(passed=checked > 0 and not differ and not unavailable,
                       differs=differ, unavailable=unavailable, checked_files=checked,
                       not_done_in_original_run=not_done_originally)
        write_json(out / 'replay_check.json', summary)
        outputs.append(out / 'replay_check.json')
    finish_run(run, [*outputs, *sorted(budget.receipts)], **summary)
    print(summary)
    if a.offline and not summary['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
