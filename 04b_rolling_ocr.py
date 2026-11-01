"""OCR finished books while 02_download.py runs, in rounds of snapshot + OCR.

Each round freezes every verified download as a new rolling_NN snapshot and
sends only books not yet fully done to 04_ocr.py. Rolling snapshots are working
products: after the download, make one full snapshot for the study; kept
responses are reused, so finished pages are not paid for again.

The limit covers all rounds, using 04_ocr.py's cost estimates from finished runs.
Stop with Ctrl-C if needed; an interrupted OCR run's spending is not counted.

python 04b_rolling_ocr.py [--max-total-cny 1000] [--min-new-books 5] [--interval 600]
"""
import argparse
import common
import fcntl
import json
import re
import subprocess
import sys
import time
from common import DATA, HERE, finish_run, read_csv, read_json, record_run

PREFIX = 'rolling_'


def step(script, *args):
    """Run one study step in its own process, so it keeps its own run record."""
    result = subprocess.run([sys.executable, '-B', str(HERE / script), *args], cwd=HERE)
    if result.returncode:
        raise SystemExit(f'{script} {args[1]} stopped with exit code {result.returncode}; see its output above.')


def download_running():
    pattern = r'[ /]02_download\.py( |$)'
    return subprocess.run(['pgrep', '-f', pattern], capture_output=True).returncode == 0


def rounds():
    folder = DATA / 'snapshot'
    names = [p.name for p in folder.iterdir() if re.fullmatch(PREFIX + r'\d+', p.name)] if folder.is_dir() else []
    return sorted(names, key=lambda name: int(name[len(PREFIX):]))


def completed_books():
    return {p.stem for p in (DATA / 'download').glob('*.json')
            if (read_json(p) or {}).get('status') == 'complete'}


def seen_books():
    """Books an earlier round already considered, whether or not it could include them."""
    return {b['folder'] for name in rounds() for b in read_csv(DATA / 'snapshot' / name / 'books.csv')
            if b['download_status'] == 'complete'}


def settled_books():
    """Books one round got through. Failed files wait for the full snapshot: retrying
    each round would pay again for pages the model keeps rejecting."""
    settled = set()
    for name in rounds():
        results = DATA / 'ocr' / name / 'results.csv'
        if not results.exists():
            continue
        status = {r['source']: r['status'] for r in read_csv(results)}
        rows = [r for r in read_csv(DATA / 'snapshot' / name / 'files.csv') if r['kind'] not in ('excluded', 'archive')]
        unfinished = {r['book'] for r in rows if status.get(r['source'], 'not_started') == 'not_started'}
        settled |= {b['folder'] for b in read_csv(DATA / 'snapshot' / name / 'books.csv')
                     if b['included'] == 'yes' and b['folder'] not in unfinished}
    return settled


def unfinished_books():
    """Books the latest round froze but did not get through, as after an interruption."""
    if not rounds():
        return set()
    latest = read_csv(DATA / 'snapshot' / rounds()[-1] / 'books.csv')
    return {b['folder'] for b in latest if b['included'] == 'yes'} - settled_books()


def spent():
    """Estimated cost of all finished OCR runs on rolling snapshots."""
    total = 0.0
    for path in (DATA / 'runs').glob('ocr_2*/run.json'):
        run = json.loads(path.read_text(encoding='utf-8'))
        if run['arguments'].get('snapshot', '').startswith(PREFIX):
            total += (run.get('summary') or {}).get('new_cost_cny_estimate', 0.0)
    return total


def one_round(limit, workers):
    existing = rounds()
    number = int(existing[-1][len(PREFIX):]) + 1 if existing else 1
    name = f'{PREFIX}{number:02d}'
    step('03_snapshot.py', '--name', name)
    done = settled_books()
    todo = [b['idx'] for b in read_csv(DATA / 'snapshot' / name / 'books.csv')
            if b['included'] == 'yes' and b['folder'] not in done]
    if todo:
        step('04_ocr.py', '--snapshot', name, '--only', *todo, '--workers', str(workers),
             '--max-cost-cny', str(limit - spent()))
        status = [r['status'] for r in read_csv(DATA / 'ocr' / name / 'results.csv')]
        if 'failed' in status and 'done' not in status:
            raise SystemExit(f'every file in {name} failed; check the API key and network before continuing.')
    print(f'{name}: {len(todo)} books sent to OCR; estimated total {spent():.2f} CNY', flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--max-total-cny', type=float, default=1000.0, help='estimated limit across all rounds')
    p.add_argument('--min-new-books', type=int, default=5, help='wait for this many newly finished books')
    p.add_argument('--interval', type=int, default=600, help='seconds between checks')
    p.add_argument('--workers', type=int, default=4, help='passed to 04_ocr.py')
    a = p.parse_args()
    common.require_data_disk()
    if a.max_total_cny < 0 or a.min_new_books < 1 or a.interval < 1 or a.workers < 1:
        p.error('the limit must be nonnegative; other numbers must be positive')
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / 'rolling_ocr.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('another 04b_rolling_ocr.py is using this data directory')
        run = record_run('rolling_ocr', vars(a))
        stopped = None
        while True:
            active = download_running()   # checked first: a book finished before exit is still seen below
            new = completed_books() - seen_books()
            if spent() >= a.max_total_cny:
                stopped = f'estimated limit of {a.max_total_cny} CNY reached'
                break
            if unfinished_books() or (new and (not active or len(new) >= a.min_new_books)):
                one_round(a.max_total_cny, a.workers)
                continue
            if not active:
                break
            print(f'download running; {len(new)} new finished books, waiting', flush=True)
            time.sleep(a.interval)
        finish_run(run, rounds=rounds(), cost_cny_estimate=round(spent(), 2), stopped=stopped)
    print(stopped or 'download not running and no new books; make a full snapshot for the study results.')


if __name__ == '__main__':
    main()
