"""Download the fixed book list, preserving cloud folder structure.

Verify size and content MD5, or apply a recorded hash-specific human decision.
Keep replacements and unverified candidates; record every file's status.
Requires BaiduPCS-Go logged in. Run one script instance per data directory.

python download.py [--only 1395] [--workers 3]
"""
import argparse
import common
import concurrent.futures
import hashlib
import tempfile
import shutil
import re
import subprocess
import time
import unicodedata
from pathlib import Path
from common import CLOUD_ROOT, DATA, now, read_json, record_run, read_csv, finish_run, sha256, write_json

PCS = 'BaiduPCS-Go'
AUDIO_VIDEO = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.wma', '.aiff', '.aif', '.opus', '.ape', '.amr',
               '.ncm', '.mflac', '.mgg', '.qmc0', '.mov', '.mp4', '.m4v', '.avi', '.mkv', '.wmv', '.flv', '.rmvb',
               '.ts', '.webm'}
ROW = re.compile(r'^\s*\d+\s+\d+\s+\d+\s+(\S+)\s+\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\s+\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  (.*?) *$')
MD5 = re.compile(r'([0-9a-fA-F]{32})  (.*)$')
UNITS = {'B': 1, 'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3, 'TB': 1024 ** 4}


def pcs(*args, timeout=600):
    p = subprocess.run([PCS, *args], capture_output=True, text=True, timeout=timeout)
    return p.stdout + p.stderr


def list_folder(remote):
    """All files under a cloud folder: [{'path', 'size_shown', 'md5'}]. None if the folder is missing."""
    files, queue = [], [remote]
    while queue:
        folder = queue.pop()
        out = pcs('ls', '-l', folder, timeout=120)
        totals = re.search(r'文件总数:\s*(\d+),\s*目录总数:\s*(\d+)', out)
        if not totals:
            # Only a missing top folder means the share was never saved. A subfolder
            # reported missing is a listing error; saving the share again would duplicate it.
            if '不存在' in out and folder == remote:
                return None
            raise RuntimeError(f'cloud listing failed for {folder}: {out[-300:]}')
        rows = 0
        for line in out.splitlines():
            m = ROW.match(line)
            if not m:
                continue
            rows += 1
            size, tail = m.groups()
            md5 = MD5.match(tail)
            name = md5.group(2) if md5 else tail.lstrip(' ')
            if name.endswith('/'):
                queue.append(f'{folder}/{name.rstrip("/")}')
            else:
                files.append({'path': f'{folder}/{name}', 'size_shown': size, 'md5': md5.group(1).lower() if md5 else None})
        if rows != sum(map(int, totals.groups())):
            raise RuntimeError(f'cloud listing incomplete for {folder}')
    return sorted(files, key=lambda f: f['path'])


def size_matches(local_bytes, shown):
    """The listing shows sizes rounded to two decimals (e.g. 300.15KB)."""
    m = re.fullmatch(r'([\d.]+)([KMGT]?B)', shown)
    if not m:
        return False
    unit = UNITS[m.group(2)]
    return abs(local_bytes - float(m.group(1)) * unit) <= 0.005 * unit + 1


def save_share(url, remote):
    """Save a shared link into the cloud folder `remote` (created first)."""
    pcs('mkdir', remote, timeout=60)
    if '改变工作目录' not in pcs('cd', remote, timeout=60):
        raise RuntimeError(f'cannot open cloud folder {remote}')
    out = pcs('transfer', url, '--collect', timeout=1800)
    if '转存到网盘成功' not in out:
        raise RuntimeError(f'saving the share failed: {out[-300:]}')
    time.sleep(20)  # saved files become visible to listing/download after a short delay


def inspect_download(path, item):
    """Read each checksum once. Size alone leaves the file unverified."""
    if not path.is_file() or not size_matches(path.stat().st_size, item['size_shown']):
        return None
    with path.open('rb') as f:
        md5 = hashlib.file_digest(f, 'md5').hexdigest()
    result = {'bytes': path.stat().st_size, 'sha256': sha256(path), 'status': 'checksum_unverified'}
    if md5 == item.get('md5'):
        result.update(status='ok', verification='cloud_md5')
    elif result['sha256'] == item.get('decision', {}).get('sha256'):
        result.update(status='ok', verification='manual', decision=item['decision'])
    return result


RESERVE_BYTES = 20 * 1024 ** 3   # stop downloading when less than this would remain free


class DiskFull(Exception):
    pass


def room_for(item):
    m = re.fullmatch(r'([\d.]+)([KMGT]?B)', item['size_shown'])
    need = float(m.group(1)) * UNITS[m.group(2)] if m else 0
    DATA.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(DATA).free - need < RESERVE_BYTES:
        raise DiskFull(f'less than {RESERVE_BYTES // 1024 ** 3} GiB would remain free on the LARP_DATA disk')


def fetch(item, remote_root, local_root):
    relative = item['path'][len(remote_root) + 1:]
    if not item['path'].startswith(remote_root + '/') or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('unsafe cloud path')
    local = local_root / relative
    entry = {'cloud_path': item['path'], 'file': relative, 'cloud_size': item['size_shown'], 'cloud_md5': item['md5']}
    if Path(relative).suffix.lower() in AUDIO_VIDEO:
        return {**entry, 'status': 'skipped_audio_video'}
    try:
        checked = inspect_download(local, item)
        if checked and checked['status'] == 'ok':
            return {**entry, **checked, 'mtime_ns': local.stat().st_mtime_ns}
        room_for(item)
        local.parent.mkdir(parents=True, exist_ok=True)
        decision = item.get('decision', {})
        candidate = DATA / 'download_unverified' / decision.get('sha256', 'none')
        # Stage retries separately so they cannot destroy the previous input.
        # Staged outside raw/ (same disk) so an interrupted run leaves nothing in the book folder.
        (DATA / 'download_tmp').mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=DATA / 'download_tmp') as tmp:
            staged = Path(tmp) / local.name
            out = ''
            for attempt in range(3):
                staged.unlink(missing_ok=True)
                if decision and candidate.is_file():
                    shutil.copy2(candidate, staged)
                elif item['size_shown'] in ('0B', '0.00B'):
                    staged.write_bytes(b'')
                else:
                    out = pcs('download', item['path'], '--saveto', tmp, timeout=7200)
                checked = inspect_download(staged, item)
                if checked:
                    break
                time.sleep(20 * (attempt + 1))
            if not checked:
                return {**entry, 'status': 'missing_or_unverified', 'client_output': out[-300:]}
            if checked['status'] != 'ok':
                # A different cloud MD5 is unresolved, not proof of corruption.
                kept = DATA / 'download_unverified' / checked['sha256']
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged, kept)
                return {**entry, **checked, 'candidate': str(kept.relative_to(DATA))}
            if local.exists():
                backup = DATA / 'download_replaced' / sha256(local)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local, backup)
                entry['previous_input'] = str(backup.relative_to(DATA))
            staged.replace(local)
            return {**entry, **checked, 'mtime_ns': local.stat().st_mtime_ns}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**entry, 'status': 'failed', 'error': str(exc)[:300]}


def unchanged(path, entry, strict):
    """strict: the SHA-256 still matches. Otherwise size and modification time,
    enough to resume a download without re-reading every finished file."""
    if not path.is_file():
        return False
    if strict or 'mtime_ns' not in entry:
        return sha256(path) == entry.get('sha256')
    stat = path.stat()
    return stat.st_size == entry.get('bytes') and stat.st_mtime_ns == entry['mtime_ns']


def completed_download(record, root, share_url, strict=True):
    """A status label alone is not evidence that files still exist unchanged."""
    return (record.get('status') == 'complete' and record.get('share_url') == share_url
            and bool(record.get('files')) and all(
                f['status'] == 'skipped_audio_video' or
                (f['status'] == 'ok' and unchanged(root / f['file'], f, strict)) for f in record['files']))


def same_name(name):
    """How APFS compares names: case- and Unicode-normalization-insensitive."""
    return unicodedata.normalize('NFC', name).casefold()


def name_clashes(listing, remote):
    """Cloud paths that would land on the same local file (e.g. A.jpg and a.jpg).
    Downloading both would replace one with the other on every run."""
    seen, clashes = {}, []
    for item in listing:
        relative = item['path'][len(remote) + 1:]
        if Path(relative).suffix.lower() in AUDIO_VIDEO:
            continue
        key = same_name(relative)
        if key in seen:
            clashes.append(f'{seen[key]} | {relative}')
        seen[key] = relative
    return clashes


def move_unlisted(local_root, files, folder):
    """Move local files that are not (or no longer) part of the book aside, so the
    raw folder matches its record: files gone from the cloud folder, and audio/video
    left by older downloads. Nothing is deleted; each move is recorded."""
    listed = {same_name(f['file']): f['status'] for f in files}
    moved = []
    if not local_root.is_dir():
        return moved
    stamp = now().replace(':', '')
    for path in sorted(p for p in local_root.rglob('*') if p.is_file()):
        relative = str(path.relative_to(local_root))
        status = listed.get(same_name(relative))
        if status is not None and status != 'skipped_audio_video':
            continue
        target = DATA / 'download_removed' / folder / stamp / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        moved.append({'file': relative, 'sha256': sha256(target), 'moved_to': str(target.relative_to(DATA)),
                      'reason': 'audio/video, not part of the corpus' if status else 'not in the cloud folder'})
    for folder_path in sorted((p for p in local_root.rglob('*') if p.is_dir()), reverse=True):
        if not any(folder_path.iterdir()):
            folder_path.rmdir()
    return moved


def download_book(book, workers, decisions=()):
    remote = f"{CLOUD_ROOT}/{book['folder']}"
    record = {'idx': book['idx'], 'folder': book['folder'], 'cloud_folder': remote, 'started': now(), 'share_url': book['share_url']}
    try:
        listing = list_folder(remote)
        if not listing:
            save_share(book['share_url'], remote)
            record['share_saved'] = now()
            listing = list_folder(remote) or []
        if not listing:
            raise RuntimeError('cloud folder is empty after saving the share')
        clashes = name_clashes(listing, remote)
        if clashes:
            raise RuntimeError('cloud names that the Mac disk treats as one file: ' + '; '.join(clashes)[:400])
        for item in listing:
            relative = item['path'][len(remote) + 1:]
            item['decision'] = next((d for d in decisions if d['folder'] == book['folder'] and d['file'] == relative), {})
        local_root = DATA / 'raw' / book['folder']
        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            files = list(pool.map(lambda item: fetch(item, remote, local_root), listing))
        # Parallel downloads now and then lose a file that downloads fine alone; one more pass, one at a time.
        files = [fetch(item, remote, local_root) if f['status'] in ('missing_or_unverified', 'failed') else f
                 for item, f in zip(listing, files)]
        record['files'] = files
        record['moved_out'] = move_unlisted(local_root, files, book['folder'])
        bad = [f for f in files if f['status'] not in ('ok', 'skipped_audio_video')]
        record['status'] = 'complete' if not bad else 'incomplete'
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        record.update(status='failed', error=str(exc)[:500])
    record['finished'] = now()
    write_json(DATA / 'download' / f"{book['folder']}.json", record)
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--only', nargs='*', help='book numbers (idx) to download; default all')
    p.add_argument('--workers', type=int, default=3, help='files downloaded at the same time')
    a = p.parse_args()
    common.require_data_disk()
    if a.workers < 1:
        p.error("--workers must be positive")
    all_books = read_csv(DATA / 'books.csv')
    books = all_books
    if a.only:
        books = [b for b in books if b['idx'] in {x.zfill(4) for x in a.only}]
    decision_path = DATA / 'download_decisions.csv'
    decisions = read_csv(decision_path) if decision_path.exists() else []
    for d in decisions:
        if (any(not d.get(k, '').strip() for k in ('folder', 'file', 'reviewer', 'reason', 'evidence'))
                or not re.fullmatch('[0-9a-f]{64}', d.get('sha256', ''))):
            raise SystemExit('download_decisions.csv needs a SHA-256, reviewer, reason and evidence for every approval')
    if len({(d['folder'], d['file']) for d in decisions}) != len(decisions):
        raise SystemExit('duplicate download decisions')
    run = record_run('download', vars(a), [DATA / 'books.csv',
                     *([decision_path] if decision_path.exists() else []),
                     *sorted((DATA / 'download').glob('*.json'))])
    stopped = None
    for book in books:
        old = read_json(DATA / 'download' / f"{book['folder']}.json", {})
        if completed_download(old, DATA / 'raw' / book['folder'], book['share_url'], strict=False):
            continue
        try:
            r = download_book(book, a.workers, decisions)
        except DiskFull as exc:   # stop the whole run; the book record is left as it was
            stopped = f"stopped at {book['folder']}: {exc}"
            print(stopped, flush=True)
            break
        print(book['folder'], r['status'], len(r.get('files', [])), flush=True)
    summary = {}
    for book in all_books:
        status = (read_json(DATA / 'download' / f"{book['folder']}.json") or {}).get('status', 'not_started')
        summary[status] = summary.get(status, 0) + 1
    records = [DATA / 'download' / f"{b['folder']}.json" for b in books]
    finish_run(run, [r for r in records if r.exists()], books=summary, stopped=stopped)
    print(summary)


if __name__ == '__main__':
    main()
