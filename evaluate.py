"""Estimate CER from a seeded random sample of completed OCR pages/tiles.

Draw saves images, model text, and editable corrected text. Reviewers correct
those copies and fill sample.csv's reviewer column. Score freezes the answers
and saves a new result with a book-cluster bootstrap interval; it never overwrites one.

python evaluate.py draw --snapshot batch1 --name eval1 --n 300 --seed 20261020
python evaluate.py score --name eval1
"""
import argparse
import common
import hashlib
import json
import random
import re
from rapidfuzz.distance import Levenshtein
from common import DATA, now, read_json, record_run, read_csv, write_csv, finish_run, sha256, write_json
import ocr


def page_images(row, record):
    path = DATA / row['path']
    if sha256(path) != row['sha256']:
        raise ValueError('input differs from snapshot')
    if record['method'] == 'ocr_image':
        return ocr.image_pages(path)
    if record['method'] == 'ocr_pdf':
        return list(ocr.pdf_pages(path))
    converted = DATA / 'ocr' / 'converted' / (row['sha256'] + '.pdf')
    if sha256(converted) != read_json(converted.with_suffix('.pdf.json'), {}).get('sha256'):
        raise ValueError('converted PDF differs from saved intermediate')
    return list(ocr.pdf_pages(converted))


def draw(a):
    manifest = DATA / 'snapshot' / a.snapshot / 'files.csv'
    if sha256(manifest) != read_json(manifest.parent / 'summary.json')['files_csv_sha256']:
        raise SystemExit('snapshot manifest changed')
    snapshot = {r['source']: r for r in read_csv(manifest)}
    results = DATA / 'ocr' / a.snapshot
    pages, eligible_files, excluded_files = [], 0, 0
    for source, row in sorted(snapshot.items()):
        record = read_json(ocr.file_record(results, snapshot[source]))
        if row['kind'] not in ('image', 'pdf', 'slides'):
            continue
        if record and ocr.valid_done(row, results, record):
            eligible_files += 1
            pages += [(source, i) for i in range(record['pages'])]
        else:
            excluded_files += 1
    if not 0 < a.n <= len(pages):
        raise SystemExit(f'--n must be between 1 and {len(pages)}')
    sample = random.Random(a.seed).sample(pages, a.n)
    out = DATA / 'evaluation' / a.name
    if out.exists():
        raise SystemExit(f'{out} exists; a drawn sample is never replaced')
    inputs = [manifest, results / 'recipe.json']
    for source in sorted({source for source, _ in sample}):
        inputs += [ocr.file_record(results, snapshot[source]), results / 'text' / (source + '.txt')]
    run = record_run('evaluate_draw', vars(a), inputs)
    for folder in ('images', 'model', 'corrected'):
        (out / folder).mkdir(parents=True)
    rows = []
    for k, (source, i) in enumerate(sample, 1):
        record = read_json(ocr.file_record(results, snapshot[source]))
        image = page_images(snapshot[source], record)[i]
        if hashlib.sha256(image).hexdigest() != record['page_image_sha256'][i]:
            raise SystemExit(f'page image of {source} p{i + 1} differs from the one sent to the model')
        text_path = results / 'text' / (source + '.txt')
        if sha256(text_path) != record['text_sha256']:
            raise SystemExit('OCR text changed during sampling')
        texts = text_path.read_text(encoding='utf-8').split('\f')
        if len(texts) != record['pages']:
            raise SystemExit(f'{source}: page breaks in the text do not match the page count')
        text = texts[i]
        (out / 'images' / f'{k:04d}.jpg').write_bytes(image)
        for folder in ('model', 'corrected'):
            (out / folder / f'{k:04d}.txt').write_text(text, encoding='utf-8')
        rows.append({'k': f'{k:04d}', 'source': source, 'page': i + 1, 'kind': snapshot[source]['kind'], 'reviewer': ''})
    write_csv(out / 'sample.csv', rows, ['k', 'source', 'page', 'kind', 'reviewer'])
    write_json(out / 'draw.json', {'drawn': now(), 'snapshot': a.snapshot, 'seed': a.seed, 'n': a.n,
                                   'population_pages': len(pages), 'eligible_files': eligible_files,
                                   'excluded_or_unfinished_ocr_files': excluded_files,
                                   'population': 'pages/tiles from valid completed image, PDF and slide files only',
                                   'fixed_files': {str(p.relative_to(out)): sha256(p) for folder in ('images', 'model')
                                                   for p in sorted((out / folder).iterdir())},
                                   'sample_identity': [{k: v for k, v in r.items() if k != 'reviewer'} for r in rows]})
    finish_run(run, sorted(p for p in out.rglob('*') if p.is_file()), sampled_pages=a.n, population_pages=len(pages))
    print(f'{a.n} of {len(pages)} pages -> {out}')


def character_error_rate(pages):
    chars = sum(p['chars'] for p in pages)
    return sum(p['errors'] for p in pages) / chars if chars else None


def book_bootstrap(pages, seed, draws=2000):
    """95% percentile interval of the CER. Pages of one book share scan quality,
    layout and fonts, so they are not independent: whole books are resampled."""
    books = {}
    for page in pages:
        books.setdefault(page['source'].split('/', 1)[0], []).append(page)
    clusters = [books[b] for b in sorted(books)]
    rng = random.Random(seed)
    values = sorted(v for v in (character_error_rate([p for _ in clusters for p in rng.choice(clusters)])
                                for _ in range(draws)) if v is not None)
    return [values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]] if values else None


def score(a):
    out = DATA / 'evaluation' / a.name
    rows = read_csv(out / 'sample.csv')
    draw_record = read_json(out / 'draw.json')
    if not rows or [{k: v for k, v in r.items() if k != 'reviewer'} for r in rows] != [
            {k: str(v) for k, v in r.items()} for r in draw_record['sample_identity']]:
        raise SystemExit('sample identity changed; only the reviewer column may be edited')
    for name, digest in draw_record['fixed_files'].items():
        if sha256(out / name) != digest:
            raise SystemExit(f'fixed evaluation input changed: {name}')
    if any(not r['reviewer'].strip() for r in rows):
        raise SystemExit('every page needs a reviewer name in sample.csv before scoring')
    inputs = [out / 'sample.csv', out / 'draw.json'] + [out / folder / f"{r['k']}.txt"
              for r in rows for folder in ('model', 'corrected')]
    run = record_run('evaluate_score', vars(a), inputs)
    saved = read_json(run)['inputs']
    # Score the frozen copies, so later reviewer edits cannot alter this result.
    def frozen_text(path):
        return (run.parent / saved[str(path)]['copy']).read_text(encoding='utf-8')

    squeeze = lambda s: re.sub(r'\s+', '', s)
    per_page = []
    for r in rows:
        model = squeeze(frozen_text(out / 'model' / f"{r['k']}.txt"))
        truth = squeeze(frozen_text(out / 'corrected' / f"{r['k']}.txt"))
        per_page.append({**r, 'errors': Levenshtein.distance(model, truth), 'chars': len(truth)})

    cer, interval = character_error_rate, lambda pages: book_bootstrap(pages, a.seed)

    report = {'scored': now(), 'draw': {k: v for k, v in draw_record.items() if k not in ('fixed_files', 'sample_identity')}, 'run_record': str(run),
              'interval_method': 'percentile bootstrap resampling books (clusters of sampled pages/tiles); descriptive for the eligible fixed corpus',
              'books_in_sample': len({p['source'].split('/', 1)[0] for p in per_page}),
              'bootstrap_seed': a.seed, 'bootstrap_draws': 2000, 'pages': len(per_page), 'cer': cer(per_page), 'cer_95ci': interval(per_page),
              'pages_with_no_error': sum(p['errors'] == 0 for p in per_page), 'by_kind': {}}
    for kind in sorted({p['kind'] for p in per_page}):
        subset = [p for p in per_page if p['kind'] == kind]
        report['by_kind'][kind] = {'pages': len(subset), 'cer': cer(subset), 'cer_95ci': interval(subset)}
    scores = out / 'scores' / run.parent.name
    scores.mkdir(parents=True)
    write_json(scores / 'score.json', report)
    write_csv(scores / 'per_page.csv', per_page, list(per_page[0]))
    finish_run(run, [scores / 'score.json', scores / 'per_page.csv'], pages=len(rows), cer=report['cer'])
    print(json.dumps(report, ensure_ascii=False, indent=1))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    d = sub.add_parser('draw')
    d.add_argument('--snapshot', required=True)
    d.add_argument('--name', required=True)
    d.add_argument('--n', type=int, default=300)
    d.add_argument('--seed', type=int, required=True)
    s = sub.add_parser('score')
    s.add_argument('--name', required=True)
    s.add_argument('--seed', type=int, default=1, help='seed for the bootstrap interval')
    a = p.parse_args()
    common.require_data_disk()
    draw(a) if a.command == 'draw' else score(a)


if __name__ == '__main__':
    main()
