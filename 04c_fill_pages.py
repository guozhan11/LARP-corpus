"""Fill pages the main OCR model could not read (mostly Aliyun moderation rejections).

Step 04 leaves a page empty when no accepted response exists for it, which is
almost always a 400 DataInspectionFailed from the moderation filter. Rejection
depends on the model: qwen3.8-flash passed 99% of these pages in a test, and the
local qwen3.5:9b (Ollama, no filter) reads the rest; accuracy on 40 hand-checked
pages was close to the main model.

Same prompt as step 04. Responses are kept under ocr/fill_responses/ keyed by
model, image, prompt and settings, so a rerun costs nothing. Step 04's own text
is never changed: the filled pages go to ocr/<name>_fill/, which step 06 reads.

python 04c_fill_pages.py --ocr batch1 [--max-cost-cny 10]
"""
import argparse
import base64
import hashlib
import json
import shutil
import time
import urllib.error
import urllib.request
from importlib import import_module
from common import API_URL, DATA, PROMPT, finish_run, now, read_csv, read_json, record_run, write_csv, write_json

ocr = import_module('04_ocr')
CLOUD = {'model': 'qwen3.8-flash', 'enable_thinking': False, 'max_tokens': 8192, 'vl_high_resolution_images': True}
CLOUD_PRICE = {'input': 0.8, 'output': 2.7}   # CNY per million tokens, estimate
LOCAL_URL = 'http://localhost:11434/api'
LOCAL = {'model': 'qwen3.5:9b', 'think': False, 'options': {'temperature': 0, 'num_ctx': 8192}}
STORE = DATA / 'ocr' / 'fill_responses'
PROMPT_SHA256 = hashlib.sha256(PROMPT.encode('utf-8')).hexdigest()


def key(settings, image_sha):
    return hashlib.sha256(json.dumps([settings, image_sha, PROMPT_SHA256], sort_keys=True).encode()).hexdigest()


def post(url, body, headers, timeout):
    request = urllib.request.Request(url, json.dumps(body).encode(), {'Content-Type': 'application/json', **headers})
    with urllib.request.urlopen(request, timeout=timeout) as r:
        return json.loads(r.read())


def cloud(image, image_sha):
    """(text or None, status, cost). Kept responses are reused."""
    path = STORE / 'cloud' / f'{key(CLOUD, image_sha)}.json'
    kept = read_json(path)
    if not kept:
        body = {**CLOUD, 'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(image).decode()}},
            {'type': 'text', 'text': PROMPT}]}]}
        for attempt in range(4):
            try:
                response = post(API_URL, body, {'Authorization': 'Bearer ' + ocr.api_key()}, 300)
                break
            except urllib.error.HTTPError as exc:
                error = exc.read().decode('utf-8', 'replace')
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    kept = {'received': now(), 'image_sha256': image_sha, 'settings': CLOUD,
                            'prompt_sha256': PROMPT_SHA256, 'status': exc.code, 'error': error[:2000]}
                    write_json(path, kept)   # a moderation verdict is kept too, so it is not paid for again
                    return None, f'cloud {exc.code}', 0.0
            except OSError:
                if attempt == 3:
                    return None, 'cloud unreachable', 0.0
            time.sleep(30 * (attempt + 1))
        kept = {'received': now(), 'image_sha256': image_sha, 'settings': CLOUD,
                'prompt_sha256': PROMPT_SHA256, 'response': response}
        write_json(path, kept)
        usage = response.get('usage', {})
        cost = (usage.get('prompt_tokens', 0) * CLOUD_PRICE['input']
                + usage.get('completion_tokens', 0) * CLOUD_PRICE['output']) / 1e6
    else:
        cost = 0.0
    if 'response' not in kept:
        return None, f"cloud {kept['status']}", cost
    choice = (kept['response'].get('choices') or [{}])[0]
    text = choice.get('message', {}).get('content')
    if choice.get('finish_reason') != 'stop' or not isinstance(text, str):
        return None, f"cloud not accepted ({choice.get('finish_reason')})", cost
    return text, 'cloud ok', cost


def local_digest():
    tags = json.loads(urllib.request.urlopen(LOCAL_URL + '/tags', timeout=30).read())
    return next(m['digest'] for m in tags['models'] if m['name'] == LOCAL['model'])


def local(image, image_sha, digest):
    settings = {**LOCAL, 'digest': digest}   # the exact model weights are part of the key
    path = STORE / 'local' / f'{key(settings, image_sha)}.json'
    kept = read_json(path)
    if not kept:
        body = {**LOCAL, 'stream': False,
                'messages': [{'role': 'user', 'content': PROMPT, 'images': [base64.b64encode(image).decode()]}]}
        try:
            response = post(LOCAL_URL + '/chat', body, {}, 1800)
        except OSError as exc:
            return None, f'local error {type(exc).__name__}'
        kept = {'received': now(), 'image_sha256': image_sha, 'settings': settings,
                'prompt_sha256': PROMPT_SHA256, 'response': response}
        write_json(path, kept)
    text = kept['response'].get('message', {}).get('content')
    if kept['response'].get('done_reason') != 'stop' or not isinstance(text, str):
        return None, 'local not accepted'
    return text, 'local ok'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ocr', required=True, help='name of the step-04 run under ocr/ (and its snapshot)')
    p.add_argument('--max-cost-cny', type=float, default=10.0)
    a = p.parse_args()
    src_dir, out = DATA / 'ocr' / a.ocr, DATA / 'ocr' / (a.ocr + '_fill')
    snapshot = DATA / 'snapshot' / a.ocr / 'files.csv'
    run = record_run('04c_fill_pages', vars(a), [snapshot, src_dir / 'results.csv'])
    budget = ocr.Budget(0, offline=True)
    shutil.rmtree(out / 'text', ignore_errors=True)   # rebuilt from kept responses
    digest = local_digest()
    spent, rows, skipped = 0.0, [], []
    for row in read_csv(snapshot):
        record = read_json(ocr.file_record(src_dir, row), {})
        if record.get('status') not in ('partial', 'failed') or row['kind'] not in ocr.INDEXED_KINDS:
            continue
        try:   # render again: a fully failed file has no page hashes in its record
            images = list(ocr.INDEXED_KINDS[row['kind']][1](DATA / row['path']))
        except Exception as exc:   # e.g. a password-protected PDF or a damaged image
            skipped.append({'source': row['source'], 'reason': f'{type(exc).__name__}: {exc}'[:200]})
            continue
        known = record.get('page_image_sha256')
        for number, image in enumerate(images, 1):
            image_sha = hashlib.sha256(image).hexdigest()
            if known and known[number - 1] != image_sha:
                raise SystemExit(f"page {number} of {row['source']} renders differently than in step 04")
            if ocr.kept_text(image_sha, budget) is not None:
                continue   # step 04 has this page
            if spent >= a.max_cost_cny:
                raise SystemExit(f'cost limit reached ({spent:.2f} CNY); rerun to continue, kept responses are reused')
            text, status, cost = cloud(image, image_sha)
            spent += cost
            model = CLOUD['model']
            if text is None:
                cloud_status = status
                text, status = local(image, image_sha, digest)
                model, status = LOCAL['model'], f'{cloud_status}; {status}'
            item = {'source': row['source'], 'kind': row['kind'], 'page': number, 'image_sha256': image_sha,
                    'model': model if text is not None else '', 'status': status, 'chars': len(text or '')}
            rows.append(item)
            if text is not None:
                target = out / 'text' / f"{row['source']}.{number}.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8')
            print(item['status'], row['source'], number, flush=True)
    write_csv(out / 'pages.csv', rows, ['source', 'kind', 'page', 'image_sha256', 'model', 'status', 'chars'])
    write_csv(out / 'skipped_files.csv', skipped, ['source', 'reason'])
    summary = {'pages': len(rows), 'filled_cloud': sum(r['model'] == CLOUD['model'] for r in rows),
               'filled_local': sum(r['model'] == LOCAL['model'] for r in rows),
               'not_filled': sum(not r['model'] for r in rows), 'files_not_renderable': len(skipped),
               'new_cost_cny_estimate': round(spent, 2), 'local_model_digest': digest}
    finish_run(run, [out / 'pages.csv', out / 'skipped_files.csv', *sorted((out / 'text').rglob('*.txt'))], **summary)
    print(summary)


if __name__ == '__main__':
    main()
