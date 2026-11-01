"""Freeze verified downloads and expanded archive members with SHA-256 hashes.

Books whose download is incomplete are left out and listed in books.csv with
the reason, so a snapshot can be taken while some links remain unavailable.

Keep bytes under inputs/, and list sources, readers and exclusions in
snapshot/<name>/files.csv. Archive sources use <archive>!/<member> notation;
nesting is limited to three levels. Existing snapshots are never replaced.

python 03_snapshot.py --name batch1
"""
import argparse
import common
import shutil
import subprocess
import zipfile
import unicodedata
from importlib import import_module
from pathlib import Path, PurePosixPath
from common import DATA, now, read_json, record_run, read_csv, write_csv, finish_run, sha256, write_json

completed_download = import_module('02_download').completed_download

KINDS = {
    'image': {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.heic', '.gif'},
    'pdf': {'.pdf'},
    'slides': {'.ppt', '.pptx', '.pps', '.ppsx', '.odp'},      # rendered to page images, then OCR
    'word': {'.doc', '.docx', '.wps', '.rtf', '.odt'},         # text read directly
    'sheet': {'.xls', '.xlsx', '.ods'},                        # cell text read directly
    'text': {'.txt', '.md', '.csv'},
}
ARCHIVES = {'.zip', '.rar', '.7z'}
AUDIO_VIDEO = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.wma', '.mov', '.mp4', '.m4v', '.avi', '.mkv',
               '.wmv', '.flv', '.rmvb', '.webm', '.amr', '.ncm', '.mflac', '.mgg', '.qmc0', '.aiff', '.opus', '.ape'}
SYSTEM = {'.ds_store', 'thumbs.db', 'desktop.ini'}
UPLOADED_PARTIAL = ('.baiduyun.p.downloading', '.baidupcs-go-downloading')


def classify(path):
    """(kind, reason). kind is None for files that are left out."""
    name = path.name.lower()
    if name.startswith('._') or name in SYSTEM or '__macosx' in [p.lower() for p in path.parts]:
        return None, 'system file'
    for marker in UPLOADED_PARTIAL:   # a file the sharer uploaded under a partial-download name
        if name.endswith(marker):
            if path.stat().st_size == 0:
                return None, 'empty placeholder uploaded by the sharer'
            name = name[:-len(marker)]
    suffix = Path(name).suffix
    if suffix in ARCHIVES:
        return 'archive', 'unpacked; its files are listed separately'
    if suffix in AUDIO_VIDEO:
        return None, 'audio or video'
    for kind, suffixes in KINDS.items():
        if suffix in suffixes:
            return kind, ''
    with path.open('rb') as f:
        head = f.read(8)
    if head.startswith(b'\xff\xd8\xff') or head.startswith(b'\x89PNG'):
        return 'image', 'image without its file extension'
    return None, f'no reader for {suffix or "files without an extension"}'


def member_name(info):
    """Unicode member name. Old Chinese zip tools store GBK names without the UTF-8 flag."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode('cp437')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('gb18030')


def unpack(archive, target):
    """Unpack into a new folder `target`. Raises on unsafe or unreadable archives."""
    work = target.with_name(target.name + '.partial')
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    if archive.suffix.lower() == '.zip':
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                name = PurePosixPath(member_name(info).replace('\\', '/'))   # Windows tools write backslashes
                if info.is_dir() or not name.parts or name.parts == ('/',):
                    continue
                if name.is_absolute() or '..' in name.parts:
                    raise ValueError(f'unsafe member path {name}')
                out = work / name
                out.parent.mkdir(parents=True, exist_ok=True)
                if out.exists():
                    raise ValueError(f'duplicate archive member: {name}')
                with z.open(info) as src, open(out, 'xb') as dst:
                    shutil.copyfileobj(src, dst)
    else:   # rar/7z store Unicode names; bsdtar (libarchive) ships with macOS
        listing = subprocess.run(['bsdtar', '-t', '-f', str(archive)], capture_output=True, text=True)
        if listing.returncode:
            raise ValueError(listing.stderr.strip()[-300:])
        names = [PurePosixPath(n.replace('\\', '/')) for n in listing.stdout.splitlines() if not n.endswith('/')]
        if any(n.is_absolute() or '..' in n.parts for n in names):
            raise ValueError('unsafe archive member path')
        normalized = [unicodedata.normalize('NFC', str(n)).casefold() for n in names]
        if len(normalized) != len(set(normalized)):
            raise ValueError('duplicate archive member (including case/Unicode aliases)')
        p = subprocess.run(['bsdtar', '-x', '-k', '-f', str(archive), '-C', str(work)], capture_output=True, text=True)
        if p.returncode:
            raise ValueError(p.stderr.strip()[-300:])
        for f in work.rglob('*'):
            if f.is_symlink() or not f.resolve().is_relative_to(work.resolve()):
                raise ValueError('archive contains links')
    work.rename(target)


def walk(root, source_prefix, book, rows, depth=0, known=None):
    """List files under root; source_prefix names root in the snapshot.
    known: SHA-256 per NFC-normalized relative path, verified just before (downloads)."""
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        relative = path.relative_to(root)
        kind, reason = classify(path)
        digest = (known or {}).get(unicodedata.normalize('NFC', str(relative))) or sha256(path)
        # Keep bytes, not just a pointer to a mutable download directory.
        name = path.name
        for marker in UPLOADED_PARTIAL:
            if name.lower().endswith(marker):
                name = name[:-len(marker)]
        frozen = DATA / 'inputs' / digest / name
        frozen.parent.mkdir(parents=True, exist_ok=True)
        if not frozen.exists():
            # An APFS clone is an independent copy that takes no space until one side
            # changes, so later edits to the download cannot alter the frozen input.
            partial = frozen.parent / '.partial'   # an interrupted copy never takes the input's name
            partial.unlink(missing_ok=True)
            if subprocess.run(['cp', '-c', str(path), str(partial)], capture_output=True).returncode:
                partial.unlink(missing_ok=True)
                shutil.copy2(path, partial)   # not APFS: an ordinary copy
            if sha256(partial) != digest:
                partial.unlink()
                raise ValueError(f'copy differs from source: {frozen}')
            partial.replace(frozen)
        elif sha256(frozen) != digest:   # left by an earlier snapshot: must be unchanged
            raise ValueError(f'frozen input changed: {frozen}')
        row = {'book': book, 'source': f'{source_prefix}/{relative}', 'path': str(frozen.relative_to(DATA)),
               'kind': kind or 'excluded', 'note': reason, 'bytes': path.stat().st_size, 'sha256': digest}
        if kind == 'archive':
            target = DATA / 'expanded' / digest
            try:
                if depth >= 3:
                    raise ValueError('archive nested more than 3 levels')
                shutil.rmtree(target, ignore_errors=True)
                unpack(frozen, target)
                children = []
                walk(target, row['source'] + '!', book, children, depth + 1)
                rows.extend(children)
            except Exception as exc:   # encrypted, damaged or unsupported archives are listed, not fatal
                row.update(kind='excluded', note=f'archive could not be unpacked: {exc}'[:300])
        rows.append(row)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--name', required=True, help='snapshot name, e.g. the date; an existing one is never replaced')
    a = p.parse_args()
    common.require_data_disk()
    out = DATA / 'snapshot' / a.name
    if out.exists():
        raise SystemExit(f'{out} exists; a snapshot is fixed once written')
    records = sorted((DATA / 'download').glob('*.json'))
    run = record_run('snapshot', vars(a), [DATA / 'books.csv', *records])
    rows, books = [], []
    for book in read_csv(DATA / 'books.csv'):
        record = read_json(DATA / 'download' / f"{book['folder']}.json", {})
        status = record.get('status', 'not_started')
        root = DATA / 'raw' / book['folder']
        # Only verified complete books enter the study input; every other book is
        # listed with why it is out, so the sample loss is reported, not hidden.
        if not completed_download(record, root, book['share_url']):
            books.append({'idx': book['idx'], 'folder': book['folder'], 'download_status': status,
                          'included': 'no', 'reason': 'download not complete or files changed since download'})
            continue
        # Compare equivalent Unicode spellings; keep original names in the output.
        known = {unicodedata.normalize('NFC', f['file']): f['sha256']
                 for f in record['files'] if f['status'] == 'ok'}
        expected = set(known)
        actual = {unicodedata.normalize('NFC', str(f.relative_to(root)))
                  for f in root.rglob('*') if f.is_file()}
        if actual != expected:
            books.append({'idx': book['idx'], 'folder': book['folder'], 'download_status': status,
                          'included': 'no', 'reason': 'raw folder holds files the download record does not list'})
            continue
        books.append({'idx': book['idx'], 'folder': book['folder'], 'download_status': status,
                      'included': 'yes', 'reason': ''})
        walk(root, book['folder'], book['folder'], rows, known=known)
    if not rows:
        raise SystemExit('no completely downloaded book to freeze')
    if len({r['source'] for r in rows}) != len(rows):
        raise ValueError('source names collide; snapshot not written')
    work = DATA / 'snapshot_partial' / a.name   # an interrupted run leaves no snapshot behind
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    write_csv(work / 'files.csv', rows, ['book', 'source', 'path', 'kind', 'note', 'bytes', 'sha256'])
    write_csv(work / 'books.csv', books, ['idx', 'folder', 'download_status', 'included', 'reason'])
    counts = {}
    for r in rows:
        counts[r['kind']] = counts.get(r['kind'], 0) + 1
    write_json(work / 'summary.json', {'created': now(), 'files_by_kind': counts,
               'archives_not_unpacked': [r['source'] for r in rows if r['note'].startswith('archive could not')],
               'books_included': sum(b['included'] == 'yes' for b in books), 'books_listed': len(books),
               'books_by_download_status': {s: sum(b['download_status'] == s for b in books)
                                            for s in sorted({b['download_status'] for b in books})},
               'files_csv_sha256': sha256(work / 'files.csv'), 'books_csv_sha256': sha256(work / 'books.csv')})
    out.parent.mkdir(exist_ok=True)
    work.rename(out)
    finish_run(run, [out / 'files.csv', out / 'books.csv', out / 'summary.json'], files_by_kind=counts)
    print(counts)


if __name__ == '__main__':
    main()
