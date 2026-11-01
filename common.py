"""Settings, atomic writes, and small reproducibility records."""
import csv
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('LARP_DATA', str(Path.home() / 'LARP_data' / 'replication'))).resolve()
if DATA.is_relative_to(HERE):
    raise ValueError('Keep LARP_DATA outside the code folder (restricted corpus data).')
CLOUD_ROOT = '/剧本杀语料/batch'
SOFFICE = os.environ.get('SOFFICE', '/Applications/LibreOffice.app/Contents/MacOS/soffice')
MODEL = 'qwen3.7-flash-2026-07-15'
API_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
PROMPT = (HERE / 'prompt.txt').read_text(encoding='utf-8')
PARAMETERS = {'model': MODEL, 'enable_thinking': False, 'max_tokens': 8192, 'vl_high_resolution_images': True}
PRICE_CNY_PER_MILLION = {'input': 0.2, 'output': 0.8}  # estimate, not a billing guarantee
LONG_SIDE = 3200
JPEG_QUALITY = 85
TALL_RATIO = 2.0
TILE_OVERLAP = 0.05


def missing_disk(path):
    """The external disk (/Volumes/<name>) a data path is on, if it is not mounted.
    A new folder on a mounted disk is fine: first runs create it."""
    parts = Path(path).resolve().parts   # follows links such as ~/LARP_data -> /Volumes/T7/...
    if len(parts) >= 3 and parts[1] == 'Volumes':
        volume = Path('/Volumes', parts[2])
        if not volume.is_mount():
            return volume
    return None


def require_data_disk():
    """Stop with a plain message instead of failing on a missing external disk."""
    volume = missing_disk(DATA)
    if volume:
        raise SystemExit(f'{volume} is not mounted: connect the data disk or set LARP_DATA (now {DATA}).')


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def write_json(path, data):
    """Each writer owns its temporary file; readers see only complete JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        json.dump(data, f, ensure_ascii=False, indent=1)
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def read_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def _output(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def record_run(step, arguments, inputs=()):
    """Keep actual code and input copies, even outside Git. Inputs must be files.

    A record left with status='started' denotes an interrupted/unfinished run.
    Copies may contain restricted data: keep the runs directory private.
    """
    started = now()
    folder = DATA / 'runs' / f"{step}_{started.replace(':', '')}"
    folder.mkdir(parents=True, exist_ok=False)
    code, saved_inputs = {}, {}
    for p in sorted(HERE.iterdir()):
        if p.suffix in ('.py', '.txt', '.md'):
            dest = folder / 'code' / p.name
            dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(p, dest)
            code[p.name] = sha256(dest)
    for i, p in enumerate(inputs):
        p = Path(p)
        dest = folder / 'inputs' / f'{i:04d}_{p.name}'
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(p, dest)
        saved_inputs[str(p)] = {'copy': str(dest.relative_to(folder)), 'sha256': sha256(dest)}
    run = {'step': step, 'started': started, 'status': 'started', 'arguments': arguments,
           'command': sys.argv, 'git_commit': _output(['git', '-C', str(HERE), 'rev-parse', 'HEAD']),
           'code_sha256': code, 'python': sys.version, 'platform': platform.platform(),
           'packages': (_output([sys.executable, '-m', 'pip', 'freeze']) or '').splitlines(),
           'tools': {'BaiduPCS-Go': _output(['BaiduPCS-Go', '--version']),
                     'LibreOffice': _output([SOFFICE, '--version']), 'bsdtar': _output(['bsdtar', '--version'])},
           'inputs': saved_inputs}
    path = folder / 'run.json'
    write_json(path, run)
    return path


def finish_run(path, outputs=(), **summary):
    """Record actual output hashes and completion; per-file records hold errors."""
    run = read_json(path)
    run.update(finished=now(), status='finished', summary=summary,
               outputs={str(p): sha256(p) for p in outputs})
    write_json(path, run)
