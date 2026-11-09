"""Write the fixed list of scripts, with their share links.

The scope (one version of each title) was decided before this code; it is read
from the scope file and the link manifest and written once as books.csv. Every later step reads only books.csv.

python3 01_make_book_list.py \
    --scope /path/to/scope.json \
    --manifest data/manifest.csv \
    --expect N          # optional: stop unless the list has exactly N books
"""
import argparse
import common
import json
from pathlib import Path
from common import DATA, record_run, read_csv, write_csv, finish_run


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--scope', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--expect', type=int, help='number of books the scope must contain')
    a = p.parse_args()
    common.require_data_disk()
    if (DATA / 'books.csv').exists():
        raise SystemExit('books.csv already exists; use a new LARP_DATA for a new selection')
    run = record_run('make_book_list', vars(a) | {'scope': str(a.scope), 'manifest': str(a.manifest)}, [a.scope, a.manifest])
    links = {row['idx'].zfill(4): row for row in read_csv(a.manifest)}
    rows = []
    for item in json.loads(a.scope.read_text(encoding='utf-8'))['rows']:
        idx = item['selected']['idx'].zfill(4)
        link = links[idx]
        if link['folder'] != item['selected']['folder'] or not link['url_with_pwd'].startswith('https://pan.baidu.com/s/'):
            raise SystemExit(f'manifest and scope disagree for {idx}')
        rows.append({'idx': idx, 'folder': link['folder'], 'title': item['title'], 'share_url': link['url_with_pwd']})
    if not rows or len({r['folder'] for r in rows}) != len(rows):
        raise SystemExit('the scope must list distinct books')
    if a.expect is not None and len(rows) != a.expect:
        raise SystemExit(f'expected {a.expect} books, found {len(rows)}')
    out = DATA / 'books.csv'
    out.parent.mkdir(parents=True, exist_ok=True)
    write_csv(out, sorted(rows, key=lambda r: r['idx']), ['idx', 'folder', 'title', 'share_url'])
    finish_run(run, [out], books=len(rows))
    print(f'{len(rows)} books -> {out}')


if __name__ == '__main__':
    main()
