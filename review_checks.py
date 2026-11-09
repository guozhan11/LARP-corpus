"""Offline review regressions, using synthetic data only.

Run: python3 -B review_checks.py
Checks focus on sample integrity, extracted text, and reproducibility.
No cloud commands or model requests are made; all fixtures live in temp dirs.
"""
import argparse
import importlib.metadata
import platform
import tarfile
import contextlib
import csv
import io
import json
import sys
import time
import concurrent.futures
import hashlib
import os
import tempfile
import threading
import unicodedata
import unittest
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import common
from importlib import import_module
from PIL import Image

make_book_list = import_module('01_make_book_list')
download = import_module('02_download')
snapshot = import_module('03_snapshot')
ocr = import_module('04_ocr')
rolling_ocr = import_module('04b_rolling_ocr')
evaluate = import_module('05_evaluate')
assemble = import_module('06_assemble')


class ArchiveChecks(unittest.TestCase):
    def test_zip_duplicate_members_are_not_silently_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'input.zip'
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                with zipfile.ZipFile(archive, 'w') as z:
                    z.writestr('page.txt', 'first')
                    z.writestr('page.txt', 'second')
            try:
                snapshot.unpack(archive, root / 'expanded')
            except ValueError:
                return  # explicit rejection is acceptable
            contents = [p.read_text() for p in (root / 'expanded').rglob('*') if p.is_file()]
            self.assertCountEqual(contents, ['first', 'second'])

    def test_bsdtar_does_not_silently_skip_duplicate_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # libarchive detects content, not the suffix. TAR supplies a simple
            # duplicate-member fixture for the same backend used by RAR/7z.
            archive = root / 'input.rar'
            with tarfile.open(archive, 'w') as tar:
                for content in (b'first', b'second'):
                    member = tarfile.TarInfo('page.txt')
                    member.size = len(content)
                    tar.addfile(member, io.BytesIO(content))
            with self.assertRaises(ValueError):
                snapshot.unpack(archive, root / 'expanded')


class WorkflowChecks(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (common, download, snapshot, ocr, evaluate, make_book_list, rolling_ocr):
            self.stack.enter_context(patch.object(module, 'DATA', self.root))
        self.stack.enter_context(patch.object(ocr, 'RESPONSES', self.root / 'ocr' / 'responses'))
        self.stack.enter_context(patch.object(common, '_output', return_value='synthetic-test-environment'))
        self.stack.enter_context(patch.object(ocr, 'api_key', return_value='synthetic-key'))
        self.network = self.stack.enter_context(patch.object(ocr.urllib.request, 'urlopen', side_effect=self.fake_response))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    @staticmethod
    def fake_response(*args, **kwargs):
        time.sleep(0.01)  # let simultaneous callers reach the same cache key
        return io.BytesIO(json.dumps({'model': ocr.MODEL, 'choices': [
            {'finish_reason': 'stop', 'message': {'content': 'synthetic text'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}).encode())

    @staticmethod
    def call(module, *args):
        with patch.object(sys, 'argv', [module.__name__ + '.py', *args]):
            module.main()

    def make_snapshot(self, images=False, incomplete=(), unreadable=()):
        books = []
        for idx in ('0001', '0002'):
            folder = idx + '_synthetic'
            raw = self.root / 'raw' / folder
            raw.mkdir(parents=True)
            page = raw / ('page.png' if images else 'page.txt')
            if images:
                Image.new('RGB', (40, 40), 'white').save(page)
            elif idx in unreadable:
                page.write_bytes(b'\xff\xfe\xfa\x80\x81')   # neither UTF-8 nor GB18030
            else:
                page.write_text('synthetic ' + idx)
            book = {'idx': idx, 'folder': folder, 'title': 'synthetic', 'share_url': 'synthetic:' + idx}
            books.append(book)
            common.write_json(self.root / 'download' / (folder + '.json'), {
                **book, 'status': 'incomplete' if idx in incomplete else 'complete', 'files': [{'file': page.name, 'status': 'ok',
                'bytes': page.stat().st_size, 'sha256': common.sha256(page)}]})
        with (self.root / 'books.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(books[0]))
            writer.writeheader()
            writer.writerows(books)
        self.call(snapshot, '--name', 'test')
        with (self.root / 'snapshot/test/files.csv').open() as f:
            return list(csv.DictReader(f))

    # Sample integrity and source preservation

    def test_book_list_uses_documented_schema_and_is_write_once(self):
        entries = [{'title': f'synthetic {i}', 'selected': {'idx': f'{i:04d}', 'folder': f'{i:04d}_synthetic'}}
                   for i in range(1, 21)]
        scope = self.root / 'scope.json'
        common.write_json(scope, {'rows': entries})
        manifest = self.root / 'manifest.csv'
        common.write_csv(manifest, [{**e['selected'], 'url_with_pwd': 'https://pan.baidu.com/s/synthetic'}
                                   for e in entries], ['idx', 'folder', 'url_with_pwd'])
        self.call(make_book_list, '--scope', str(scope), '--manifest', str(manifest), '--expect', '20')
        books = self.root / 'books.csv'
        self.assertEqual(len(common.read_csv(books)), 20)
        digest = common.sha256(books)
        with self.assertRaises(SystemExit):
            self.call(make_book_list, '--scope', str(scope), '--manifest', str(manifest))
        self.assertEqual(common.sha256(books), digest)

    def test_unverified_download_requires_exact_manual_decision(self):
        root = self.root / 'raw/book'
        root.mkdir(parents=True)
        candidate = self.root / 'download_unverified' / hashlib.sha256(b'RIGHT').hexdigest()
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b'RIGHT')
        item = {'path': '/remote/page.txt', 'size_shown': '5B', 'md5': '0' * 32,
                'decision': {'sha256': candidate.name, 'reviewer': 'synthetic',
                             'reason': 'fixture', 'evidence': 'fixture content'}}
        with patch.object(download, 'pcs', side_effect=AssertionError('no cloud calls')):
            result = download.fetch(item, '/remote', root)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['verification'], 'manual')
        self.assertEqual((root / 'page.txt').read_bytes(), b'RIGHT')

    def test_download_replacement_preserves_old_bytes_and_reuses_verified_file(self):
        root = self.root / 'raw/book'
        root.mkdir(parents=True)
        (root / 'page.txt').write_bytes(b'WRONG')
        item = {'path': '/remote/page.txt', 'size_shown': '5B', 'md5': hashlib.md5(b'RIGHT').hexdigest()}

        def download_file(*args, **kwargs):
            (Path(args[args.index('--saveto') + 1]) / 'page.txt').write_bytes(b'RIGHT')
            return 'synthetic download'

        with patch.object(download, 'pcs', side_effect=download_file) as client:
            result = download.fetch(item, '/remote', root)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual((root / 'page.txt').read_bytes(), b'RIGHT')
            self.assertEqual((self.root / result['previous_input']).read_bytes(), b'WRONG')
            self.assertEqual(download.fetch(item, '/remote', root)['status'], 'ok')
            self.assertEqual(client.call_count, 1)
            unresolved = download.fetch({**item, 'md5': '0' * 32}, '/remote', root)
            self.assertEqual(unresolved['status'], 'checksum_unverified')
            self.assertEqual((self.root / unresolved['candidate']).read_bytes(), b'RIGHT')
            self.assertEqual((root / 'page.txt').read_bytes(), b'RIGHT')

    def test_subfolder_listing_error_does_not_save_the_share_again(self):
        calls = []

        def client(*args, timeout=None):
            calls.append(args[0])
            if args[0] == 'ls' and args[2].endswith('/sub'):
                return '遇到错误, 文件或目录不存在'
            return '  0  1  0  -  2021-01-01 00:00:00  2021-01-01 00:00:00  sub/\n文件总数: 0, 目录总数: 1'
        with patch.object(download, 'pcs', side_effect=client):
            record = download.download_book({'idx': '0001', 'folder': '0001_a', 'share_url': 'u'}, 1)
        self.assertEqual(record['status'], 'failed')
        self.assertNotIn('transfer', calls)
        self.assertNotIn('mkdir', calls)

    def test_files_gone_from_the_cloud_are_moved_aside(self):
        root = self.root / 'raw/0001_a'
        (root / 'old').mkdir(parents=True)
        (root / 'old/removed.jpg').write_bytes(b'gone from cloud')
        (root / 'song.mp3').write_bytes(b'audio')
        (root / 'kept.txt').write_bytes(b'RIGHT')
        files = [{'file': 'kept.txt', 'status': 'ok'}, {'file': 'song.mp3', 'status': 'skipped_audio_video'}]
        moved = download.move_unlisted(root, files, '0001_a')
        self.assertEqual(sorted(m['file'] for m in moved), ['old/removed.jpg', 'song.mp3'])
        self.assertEqual(sorted(str(p.relative_to(root)) for p in root.rglob('*')), ['kept.txt'])
        self.assertTrue(all((self.root / m['moved_to']).is_file() for m in moved))

    def test_case_clashing_cloud_names_stop_the_book(self):
        listing = [{'path': '/r/0001_a/A.jpg', 'size_shown': '1B', 'md5': None},
                   {'path': '/r/0001_a/a.jpg', 'size_shown': '1B', 'md5': None}]
        with patch.object(download, 'CLOUD_ROOT', '/r'), patch.object(download, 'list_folder', return_value=listing), \
                patch.object(download, 'fetch', side_effect=AssertionError('downloaded')):
            record = download.download_book({'idx': '0001', 'folder': '0001_a', 'share_url': 'u'}, 1)
        self.assertEqual(record['status'], 'failed')
        self.assertIn('A.jpg', record['error'])

    def test_unicode_form_difference_does_not_move_a_good_file(self):
        root = self.root / 'raw/0001_a'
        root.mkdir(parents=True)
        decomposed = unicodedata.normalize('NFD', 'café.txt')
        (root / decomposed).write_bytes(b'RIGHT')
        moved = download.move_unlisted(root, [{'file': unicodedata.normalize('NFC', 'café.txt'), 'status': 'ok'}], '0001_a')
        self.assertEqual(moved, [])

    def test_snapshot_accepts_unicode_equivalent_names_but_checks_files(self):
        self.make_snapshot()
        book = '0001_synthetic'
        root = self.root / 'raw' / book
        cloud_name = 'café/résumé.txt'
        local_name = unicodedata.normalize('NFD', cloud_name)
        local = root / local_name
        local.parent.mkdir()
        (root / 'page.txt').rename(local)
        receipt = self.root / 'download' / (book + '.json')
        record = common.read_json(receipt)
        record['files'][0]['file'] = cloud_name
        common.write_json(receipt, record)

        self.call(snapshot, '--name', 'unicode')
        books = common.read_csv(self.root / 'snapshot/unicode/books.csv')
        self.assertEqual(books[0]['included'], 'yes')
        rows = common.read_csv(self.root / 'snapshot/unicode/files.csv')
        row = next(r for r in rows if r['book'] == book)
        self.assertEqual(row['source'], book + '/' + local_name)
        self.assertEqual(common.sha256(self.root / row['path']), record['files'][0]['sha256'])

        # Normalization must not hide an extra file or changed contents.
        extra = root / 'extra.txt'
        extra.write_text('unexpected synthetic file')
        self.call(snapshot, '--name', 'extra')
        self.assertEqual(common.read_csv(self.root / 'snapshot/extra/books.csv')[0]['included'], 'no')
        extra.unlink()
        local.write_text('changed synthetic contents')
        self.call(snapshot, '--name', 'changed')
        self.assertEqual(common.read_csv(self.root / 'snapshot/changed/books.csv')[0]['included'], 'no')
        self.network.assert_not_called()

    def test_snapshot_freezes_complete_books_and_lists_the_rest(self):
        rows = self.make_snapshot(incomplete=('0002',))
        self.assertEqual({r['book'] for r in rows}, {'0001_synthetic'})
        books = {b['idx']: b for b in common.read_csv(self.root / 'snapshot/test/books.csv')}
        self.assertEqual((books['0001']['included'], books['0002']['included']), ('yes', 'no'))
        self.assertTrue(books['0002']['reason'])
        self.assertEqual(common.read_json(self.root / 'snapshot/test/summary.json')['books_included'], 1)

    def test_snapshot_keeps_bytes_after_raw_changes(self):
        rows = self.make_snapshot()
        raw = self.root / 'raw' / rows[0]['source']
        raw.write_text('changed')
        self.assertEqual(common.sha256(self.root / rows[0]['path']), rows[0]['sha256'])
        record = common.read_json(self.root / 'download' / (rows[0]['book'] + '.json'))
        self.assertFalse(download.completed_download(record, raw.parent, record['share_url']))
        self.call(ocr, '--snapshot', 'test')

    # Extracted text, page order, and incomplete results

    def test_direct_office_readers_after_refactoring(self):
        word = self.root / 'sample.docx'
        xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>body</w:t></w:r><w:txbxContent><w:p><w:r><w:t>box</w:t>'
               '</w:r></w:p></w:txbxContent></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>table</w:t>'
               '</w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
        with zipfile.ZipFile(word, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        sheet = self.root / 'sample.xlsx'
        wb = ocr.openpyxl.Workbook()
        wb.active.append(['value', '=1+1'])
        wb.save(sheet)
        wb.close()
        for path, kind in ((word, 'word'), (sheet, 'sheet')):
            row = {'path': path.name, 'source': path.name, 'kind': kind, 'sha256': common.sha256(path)}
            result = ocr.process(row, self.root / 'out', ocr.Budget(0, True))
            self.assertEqual(result['status'], 'done', result)
            text = (self.root / 'out/text' / (path.name + '.txt')).read_text()
            if kind == 'word':
                self.assertEqual(text, 'body\nbox\ntable')
            else:
                self.assertIn('value', text)
                self.assertEqual(result['formulas_without_saved_value'], 1)
        self.network.assert_not_called()

    def test_conversion_without_receipt_is_set_aside_and_redone(self):
        source = self.root / 'deck.ppt'
        source.write_bytes(b'synthetic deck')
        keep = self.root / 'ocr/converted/deck.pdf'
        keep.parent.mkdir(parents=True)
        keep.write_bytes(b'left by an interrupted run')

        def soffice(command, **kwargs):
            outdir = Path(command[command.index('--outdir') + 1])
            (outdir / 'input.pdf').write_bytes(b'fresh conversion')
        with patch.object(ocr.subprocess, 'run', side_effect=soffice):
            self.assertEqual(ocr.convert(source, 'pdf', keep).read_bytes(), b'fresh conversion')
        aside = list((self.root / 'ocr/converted_set_aside').iterdir())
        self.assertEqual([p.read_bytes() for p in aside], [b'left by an interrupted run'])

    def test_parallel_ocr_keeps_page_order(self):
        def slow(image, budget):
            # Different response times must not change the source page order.
            time.sleep((3 - int(image) % 4) * 0.01)
            return image.decode(), 'h'
        with patch.object(ocr, 'transcribe', side_effect=slow), \
                concurrent.futures.ThreadPoolExecutor(4) as pool, patch.object(ocr, 'PAGE_POOL', pool):
            texts = [t for t, _, _ in ocr.transcribe_all((str(i).encode() for i in range(12)), None)]
        self.assertEqual(texts, [str(i) for i in range(12)])

    def test_pdf_render_warnings_are_recorded(self):
        pdf = self.root / 'broken.pdf'
        doc = ocr.fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), 'text')
        doc.save(pdf)
        doc.close()
        data = pdf.read_bytes().replace(b'/Font', b'/Fnt', 1)   # a missing resource MuPDF warns about
        pdf.write_bytes(data)
        notes = {}
        list(ocr.pdf_pages(pdf, notes))
        clean = {}
        good = self.root / 'good.pdf'
        doc = ocr.fitz.open()
        doc.new_page().insert_text((72, 72), 'text')
        doc.save(good)
        doc.close()
        list(ocr.pdf_pages(good, clean))
        self.assertTrue(notes.get('render_warnings') or notes.get('repaired_file_structure'), notes)
        self.assertEqual(clean, {})

    def test_pdf_open_warnings_stay_with_their_file(self):
        good, broken = self.root / 'good.pdf', self.root / 'broken.pdf'
        for path in (good, broken):
            doc = ocr.fitz.open()
            doc.new_page().insert_text((72, 72), 'text')
            doc.save(path)
            doc.close()
        data = broken.read_bytes()
        broken.write_bytes(data[:data.rindex(b'startxref')] + b'startxref\n999999\n%%EOF\n')   # broken xref
        results = {}

        def render(path):
            notes = {}
            for _ in range(5):
                list(ocr.pdf_pages(path, notes))
            results[path.name] = notes
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(render, [good, broken, good, broken]))
        self.assertEqual(results['good.pdf'], {})
        self.assertIn(0, [n for pages in results['broken.pdf'].get('render_warnings', {}).values() for n in pages])

    def test_rejected_model_response_is_kept(self):
        self.network.side_effect = lambda *a, **k: io.BytesIO(json.dumps({'model': ocr.MODEL,
            'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]}).encode())
        with self.assertRaises(ValueError):
            ocr.transcribe(b'synthetic-image', ocr.Budget(1, False))
        self.assertEqual(len(list((self.root / 'ocr/responses_rejected').glob('*.json'))), 1)
        self.assertEqual(list(ocr.RESPONSES.glob('*.json')), [])

    def test_same_page_is_requested_once(self):
        budget = ocr.Budget(1, False)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: ocr.transcribe(b'synthetic-image', budget), range(4)))
        self.assertEqual(self.network.call_count, 1)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(ocr.transcribe(b'synthetic-image', ocr.Budget(0, True)), results[0])

    def test_resume_repairs_output_and_keeps_full_summary(self):
        rows = self.make_snapshot()
        self.call(ocr, '--snapshot', 'test')
        out = self.root / 'ocr/test'
        target = out / 'text' / (rows[0]['source'] + '.txt')
        target.unlink()
        self.call(ocr, '--snapshot', 'test', '--only', '1')
        self.assertTrue(target.is_file())
        with (out / 'results.csv').open() as f:
            results = list(csv.DictReader(f))
        self.assertEqual([r['status'] for r in results], ['done', 'done'])
        self.network.assert_not_called()

    def test_budget_pause_can_resume(self):
        rows = self.make_snapshot(images=True)
        self.call(ocr, '--snapshot', 'test', '--max-cost-cny', '0')
        out = self.root / 'ocr/test'
        self.assertEqual(common.read_json(ocr.file_record(out, rows[0]))['status'], 'not_started')
        self.network.assert_not_called()
        self.call(ocr, '--snapshot', 'test')
        self.assertTrue(ocr.valid_done(rows[0], out, common.read_json(ocr.file_record(out, rows[0]))))

    # Reproducibility and evaluation

    def test_unrelated_pages_do_not_share_a_lock(self):
        self.assertIs(ocr.key_lock('a'), ocr.key_lock('a'))   # same page: one request at a time
        with ocr.key_lock('a'):
            self.assertTrue(ocr.key_lock('b').acquire(blocking=False))   # other pages never wait
            ocr.key_lock('b').release()

    def test_unmounted_data_disk_stops_with_a_message(self):
        self.assertIsNone(common.missing_disk(self.root / 'not/yet/created'))   # first run: fine
        absent = Path('/Volumes/replication-check-absent-disk/LARP_data')
        self.assertEqual(common.missing_disk(absent), Path('/Volumes/replication-check-absent-disk'))
        with patch.object(common, 'DATA', absent):
            with self.assertRaisesRegex(SystemExit, 'not mounted'):
                common.require_data_disk()

    def test_pdfs_render_outside_the_threaded_process(self):
        pdf = self.root / 'two.pdf'
        doc = ocr.fitz.open()
        doc.new_page()
        doc.new_page()
        doc.save(pdf)
        doc.close()
        row = {'path': pdf.name, 'source': pdf.name, 'kind': 'pdf', 'sha256': common.sha256(pdf)}
        self.assertEqual(ocr.process(row, self.root / 'out', ocr.Budget(0, True))['status'], 'failed')   # stops mid-file
        self.assertNotEqual(ocr.in_renderer(os.getpid), os.getpid())
        pages = list(ocr.pdf_pages(pdf))
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0], ocr.in_renderer(ocr.render_page, str(pdf), 1)[0])

    def test_request_errors_are_hashed_in_run_record(self):
        self.make_snapshot(images=True)
        errors = self.root / 'ocr/request_errors'
        for label, error, expected_count in (
            ('http', ocr.urllib.error.HTTPError('https://example.invalid', 401, 'unauthorized', {}, None), 2),
            ('timeout', TimeoutError('synthetic timeout'), 8),
        ):
            with self.subTest(error=label):
                before = set(errors.glob('*.json'))
                self.network.side_effect = error
                with patch.object(ocr.time, 'sleep'):
                    self.call(ocr, '--snapshot', 'test', '--workers', '1', '--redo-failed')
                created = set(errors.glob('*.json')) - before
                self.assertEqual(len(created), expected_count)  # two files; timeouts retry four times
                run_path = sorted((self.root / 'runs').glob('ocr_*/run.json'))[-1]
                run = common.read_json(run_path)
                self.assertEqual(run['summary']['files'], {'failed': 2})
                for path in created:
                    self.assertEqual(run['outputs'].get(str(path)), common.sha256(path))
                self.assertEqual(list(ocr.RESPONSES.glob('*.json')), [])

    def test_untracked_code_is_saved(self):
        source = self.root / 'input.txt'
        source.write_text('synthetic')
        run = common.record_run('test', {}, [source])
        source.write_text('changed')
        record = common.read_json(run)
        self.assertEqual((run.parent / 'code/common.py').read_text(), (common.HERE / 'common.py').read_text())
        self.assertEqual((run.parent / record['inputs'][str(source)]['copy']).read_text(), 'synthetic')

    def test_disk_full_stop_still_finishes_the_run_record(self):
        common.write_csv(self.root / 'books.csv', [{'idx': '0001', 'folder': '0001_a', 'title': 'a', 'share_url': 'u'}],
                         ['idx', 'folder', 'title', 'share_url'])
        with patch.object(download, 'download_book', side_effect=download.DiskFull('full')):
            self.call(download)
        run = common.read_json(next((self.root / 'runs').glob('download_*/run.json')))
        self.assertEqual(run['status'], 'finished')
        self.assertIn('full', run['summary']['stopped'])

    def test_changed_recipe_is_rejected(self):
        self.make_snapshot()
        self.call(ocr, '--snapshot', 'test')
        with patch.object(ocr, 'PARAMETERS', {**ocr.PARAMETERS, 'max_tokens': 1}):
            with self.assertRaises(SystemExit):
                self.call(ocr, '--snapshot', 'test')

    def test_changed_render_settings_are_rejected(self):
        self.make_snapshot()
        self.call(ocr, '--snapshot', 'test')
        with patch.object(ocr, 'LONG_SIDE', 1600):
            with self.assertRaises(SystemExit):
                self.call(ocr, '--snapshot', 'test')

    def test_replay_rejects_missing_original_and_missing_cache(self):
        rows = self.make_snapshot(images=True)
        self.call(ocr, '--snapshot', 'test')
        self.call(ocr, '--snapshot', 'test', '--offline')
        check = self.root / 'ocr/test_replay/replay_check.json'
        self.assertTrue(common.read_json(check)['passed'])
        (self.root / 'ocr/test/text' / (rows[0]['source'] + '.txt')).unlink()
        with self.assertRaises(SystemExit):
            self.call(ocr, '--snapshot', 'test', '--offline')
        self.assertFalse(common.read_json(check)['passed'])
        self.call(ocr, '--snapshot', 'test')
        for cache in ocr.RESPONSES.glob('*.json'):
            cache.unlink()
        with self.assertRaises(SystemExit):
            self.call(ocr, '--snapshot', 'test', '--offline')
        self.assertEqual(len(common.read_json(check)['unavailable']), 2)
        self.assertEqual(self.network.call_count, 1)

    def test_replay_passes_when_a_file_failed_in_the_original_run(self):
        self.make_snapshot(unreadable=('0002',))
        self.call(ocr, '--snapshot', 'test')
        self.call(ocr, '--snapshot', 'test', '--offline')
        check = common.read_json(self.root / 'ocr/test_replay/replay_check.json')
        self.assertTrue(check['passed'])
        self.assertEqual((check['checked_files'], check['not_done_in_original_run']), (1, ['0002_synthetic/page.txt']))

    def test_evaluation_keeps_old_truth_and_scores(self):
        self.make_snapshot(images=True)
        self.call(ocr, '--snapshot', 'test')
        self.call(evaluate, 'draw', '--snapshot', 'test', '--name', 'sample', '--n', '2', '--seed', '7')
        out = self.root / 'evaluation/sample'
        with (out / 'sample.csv').open() as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            row['reviewer'] = 'synthetic reviewer'
        with (out / 'sample.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.call(evaluate, 'score', '--name', 'sample')
        first = next((out / 'scores').glob('*/score.json'))
        first_hash = common.sha256(first)
        (out / 'corrected/0001.txt').write_text('changed synthetic truth')
        self.call(evaluate, 'score', '--name', 'sample')
        self.assertEqual(common.sha256(first), first_hash)
        self.assertEqual(len(list((out / 'scores').glob('*/score.json'))), 2)
        run = Path(common.read_json(first)['run_record'])
        saved = common.read_json(run)['inputs'][str(out / 'corrected/0001.txt')]
        self.assertEqual((run.parent / saved['copy']).read_text(), 'synthetic text')
        (out / 'model/0001.txt').write_text('tampered model')
        with self.assertRaises(SystemExit):
            self.call(evaluate, 'score', '--name', 'sample')

    def test_bootstrap_resamples_books_not_pages(self):
        one_book = [{'source': '0001_a/x.png', 'errors': e, 'chars': 10} for e in (0, 5, 10)]
        self.assertEqual(evaluate.book_bootstrap(one_book, 1, draws=200), [0.5, 0.5])   # one cluster: no spread
        two = one_book + [{'source': '0002_b/y.png', 'errors': 0, 'chars': 30}]
        low, high = evaluate.book_bootstrap(two, 1, draws=200)
        self.assertLess(low, high)

    # Tall images

    @staticmethod
    def striped(height, gap_at=()):
        """Text-like rows on white, except blank bands at the given rows."""
        im = Image.new('RGB', (100, height), 'white')
        for y in range(height):
            if not any(start <= y < start + 10 for start in gap_at):
                im.paste('black', (0, y, 50, y + 1))
        return im

    def test_tall_image_is_cut_in_a_blank_band_without_overlap(self):
        notes = {}
        tiles = ocr.tall_tiles(self.striped(500, gap_at=[190, 380]), notes)
        heights = [Image.open(io.BytesIO(t)).height for t in tiles]
        self.assertEqual(heights, [198, 190, 112])   # adding up to the full height: nothing repeats
        self.assertEqual(notes, {})

    def test_tall_image_without_blank_band_overlaps_and_is_flagged(self):
        im = self.striped(500)
        im.paste('white', (0, 190, 100, 192))   # too narrow to count as a gap between lines
        notes = {}
        heights = [Image.open(io.BytesIO(t)).height for t in ocr.tall_tiles(im, notes)]
        self.assertEqual(heights[0], 200)
        self.assertGreater(sum(heights), 500)
        self.assertTrue(notes['overlapping_tiles_need_review'])

    def test_tall_image_is_sent_whole_when_it_keeps_full_resolution(self):
        path = self.root / 'tall.png'
        self.striped(500).save(path)
        self.assertEqual(len(ocr.image_pages(path)), 1)

    # Partial losses are recorded, not hidden

    def test_failed_page_keeps_the_other_pages_and_can_be_retried(self):
        path = self.root / 'two.tif'
        frames = [Image.new('RGB', (40, 40), color) for color in ('white', 'black')]
        frames[0].save(path, save_all=True, append_images=frames[1:])
        row = {'source': 'book/two.tif', 'kind': 'image', 'path': 'two.tif', 'sha256': common.sha256(path)}
        replies = iter([('page one', 'a'), ValueError('rejected')])

        def reply(image, budget):
            answer = next(replies)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with patch.object(ocr, 'transcribe', reply):
            record = ocr.process(row, self.root / 'out', ocr.Budget(1, False))
        self.assertEqual((record['status'], record['failed_pages']), ('partial', [2]))
        self.assertEqual((self.root / 'out/text/book/two.tif.txt').read_text(), 'page one\f')
        with patch.object(ocr, 'transcribe', side_effect=ValueError('rejected')):
            self.assertEqual(ocr.process(row, self.root / 'out', ocr.Budget(1, False))['status'], 'failed')

    def test_doubtful_encodings_are_flagged_not_guessed(self):
        path = self.root / 'text.txt'
        sample = '這是一段繁體中文的劇本內容，偵探走進房間。'
        path.write_bytes(sample.encode('utf-16'))
        self.assertEqual(ocr.plain_text(path)[0], sample)
        path.write_bytes(sample.encode('big5'))
        self.assertTrue(ocr.plain_text(path)[1]['encoding_needs_review'])
        path.write_bytes('这是一段简体中文的剧本内容，侦探走进房间。'.encode('gbk'))
        self.assertFalse(ocr.plain_text(path)[1]['encoding_needs_review'])

    def test_snapshot_summary_lists_archives_it_could_not_unpack(self):
        self.make_snapshot()
        bad = self.root / 'raw/0001_synthetic/bad.zip'
        bad.write_bytes(b'not a zip')
        path = self.root / 'download/0001_synthetic.json'
        record = common.read_json(path)
        record['files'].append({'file': 'bad.zip', 'status': 'ok', 'bytes': 9, 'sha256': common.sha256(bad)})
        common.write_json(path, record)
        self.call(snapshot, '--name', 'second')
        self.assertEqual(common.read_json(self.root / 'snapshot/second/summary.json')['archives_not_unpacked'],
                         ['0001_synthetic/bad.zip'])

    def test_download_stops_when_the_cloud_account_is_lost(self):
        common.write_csv(self.root / 'books.csv', [{'idx': i, 'folder': i, 'title': 't', 'share_url': 's'} for i in ('0001', '0002')],
                         ['idx', 'folder', 'title', 'share_url'])
        failed = {'status': 'failed'}
        with patch.object(download, 'download_book', return_value=failed) as book, patch.object(download, 'logged_in', return_value=False):
            self.call(download)
        self.assertEqual(book.call_count, 1)

    def test_interrupted_snapshot_leaves_nothing_and_can_be_rerun(self):
        self.make_snapshot()
        with patch.object(snapshot, 'write_json', side_effect=OSError('disk')), self.assertRaises(OSError):
            self.call(snapshot, '--name', 'second')
        self.assertFalse((self.root / 'snapshot/second').exists())
        self.call(snapshot, '--name', 'second')
        self.assertTrue((self.root / 'snapshot/second/summary.json').exists())

    def test_cloned_input_that_differs_from_its_hash_is_rejected(self):
        source = self.root / 'source' / 'page.txt'
        source.parent.mkdir()
        source.write_text('original')

        def changed_clone(command, **kwargs):
            Path(command[-1]).write_text('changed')
            return SimpleNamespace(returncode=0)
        with patch.object(snapshot.subprocess, 'run', changed_clone), self.assertRaises(ValueError):
            snapshot.walk(source.parent, 'book', 'book', [])
        self.assertEqual(list((self.root / 'inputs').rglob('page.txt')), [])

    # OCR rounds during a download

    def test_rolling_restart_finishes_a_round_interrupted_before_ocr(self):
        self.make_snapshot()
        calls, step = self.rolling_steps()

        def interrupted(script, *args):
            if script == '04_ocr.py':
                raise SystemExit('interrupted')
            step(script, *args)
        with patch.object(rolling_ocr, 'download_running', lambda: False):
            with patch.object(rolling_ocr, 'step', interrupted), self.assertRaises(SystemExit):
                self.call(rolling_ocr, '--max-total-cny', '50')
            self.assertEqual(rolling_ocr.settled_books(), set())
            with patch.object(rolling_ocr, 'step', step):
                self.call(rolling_ocr, '--max-total-cny', '50')
        self.assertEqual(rolling_ocr.settled_books(), {'0001_synthetic', '0002_synthetic'})

    def rolling_steps(self):
        calls = []

        def step(script, *args):
            calls.append((script, args))
            self.call({'03_snapshot.py': snapshot, '04_ocr.py': ocr}[script], *args)
        return calls, step

    def test_rolling_rounds_send_only_unfinished_books_and_stop_with_download(self):
        self.make_snapshot(incomplete=('0002',))
        calls, step = self.rolling_steps()
        record = self.root / 'download' / '0002_synthetic.json'

        def second_book_finishes(seconds):
            common.write_json(record, {**common.read_json(record), 'status': 'complete'})
        running = iter([True, True, False, False])
        with patch.object(rolling_ocr, 'step', step), \
                patch.object(rolling_ocr, 'download_running', lambda: next(running)), \
                patch.object(rolling_ocr.time, 'sleep', second_book_finishes):
            self.call(rolling_ocr, '--max-total-cny', '50', '--min-new-books', '1')
        only = [args[args.index('--only') + 1:args.index('--workers')] for script, args in calls if script == '04_ocr.py']
        self.assertEqual(only, [('0001',), ('0002',)])
        self.assertEqual(rolling_ocr.rounds(), ['rolling_01', 'rolling_02'])
        self.assertEqual(rolling_ocr.settled_books(), {'0001_synthetic', '0002_synthetic'})

    def test_rolling_limit_counts_all_rounds_and_stops_new_requests(self):
        self.make_snapshot()
        calls, step = self.rolling_steps()
        common.write_json(self.root / 'runs' / 'ocr_2000' / 'run.json', {
            'arguments': {'snapshot': 'rolling_01'}, 'summary': {'new_cost_cny_estimate': 600.0}})
        common.write_json(self.root / 'runs' / 'ocr_2001' / 'run.json', {
            'arguments': {'snapshot': 'test'}, 'summary': {'new_cost_cny_estimate': 900.0}})
        self.assertEqual(rolling_ocr.spent(), 600.0)
        with patch.object(rolling_ocr, 'step', step), patch.object(rolling_ocr, 'download_running', lambda: False):
            self.call(rolling_ocr, '--max-total-cny', '600')
        self.assertEqual(calls, [])


class AssembleChecks(unittest.TestCase):
    def test_filled_pages_replace_failed_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            ocr_dir = Path(tmp)
            (ocr_dir / 'text' / 'b').mkdir(parents=True)
            (ocr_dir / 'text' / 'b' / 'x.pdf.txt').write_text('一\f\f三', encoding='utf-8')
            row = {'source': 'b/x.pdf', 'kind': 'pdf'}
            record = {'status': 'partial', 'failed_pages': [2]}
            self.assertEqual(assemble.page_text(ocr_dir, row, record),
                             ([(1, '一'), (2, '〔本页缺失：OCR 失败〕'), (3, '三')], 1, 0))
            self.assertEqual(assemble.page_text(ocr_dir, row, record, {2: '二'}),
                             ([(1, '一'), (2, '二'), (3, '三')], 0, 1))
            failed = {'status': 'failed', 'error': 'ValueError: request failed: 400'}
            self.assertEqual(assemble.page_text(ocr_dir, row, failed, {1: '甲', 2: '乙'}),
                             ([(1, '甲'), (2, '乙')], 0, 2))
            self.assertEqual(assemble.page_text(ocr_dir, row, failed), ([(None, '〔整份缺失：OCR 失败〕')], 1, 0))
            # page 3 could not be filled either: it stays missing and the file keeps 3 pages
            self.assertEqual(assemble.page_text(ocr_dir, row, failed, {1: '甲', 2: '乙', 3: None}),
                             ([(1, '甲'), (2, '乙'), (3, '〔本页缺失：OCR 失败〕')], 1, 2))

    def test_pages_sort_in_natural_order(self):
        names = ['军官_10.jpg', '军官_2.jpg', '军官_1.jpg']
        self.assertEqual(sorted(names, key=assemble.natural_key), ['军官_1.jpg', '军官_2.jpg', '军官_10.jpg'])

    def test_document_types_from_paths(self):
        cases = {('线索', '第一幕', '1.jpg'): '线索',                       # an act folder inside clues stays a clue
                 ('线索卡', '第一幕', '第二轮', '莫轩庭.pdf'): '线索',
                 ('售后资料', '打斗人物技能使用表.xlsx'): '宣传与售后',        # after-sales folder wins over 人物
                 ('组织手册', '组织者手册.pdf'): '主持人手册',
                 ('人物剧本', '军官', '军官_1.jpg'): '人物剧本',
                 ('第二幕', '张三.pdf'): '其他',                             # act names say nothing about type
                 ('剧本及线索', '核弹.pdf'): '其他',                         # a mixed folder does not decide
                 ('剧本', 'B本（黄色封面）', '1.pdf'): '人物剧本',            # bracketed notes are ignored
                 ('卡洛斯.pdf',): '其他',
                 ('人物剧本', '封面.pdf'): '宣传与售后',                     # a cover next to the file
                 ('剧本', '封面200g铜版纸覆膜-内页80g', '4赵斌.pdf'): '人物剧本',  # print spec, not a cover
                 ('三国新语', '姜维.pdf'): '其他',
                 ('开本资料', '结局', '苏南屿结局.pdf'): '人物剧本',          # a character's ending goes to that player
                 ('结局', '结局阶段小剧场（主持人端）.doc'): '主持人手册',    # read out by the host
                 ('开本资料', '结局卡'): '线索',
                 ('剧本', '诡计详解', ''): '主持人手册',                      # a trick explanation is for the host
                 ('线索', '线索详解（简版）.docx'): '线索',                    # clue words come before weak host words
                 ('剧本', '格拉多之门剧本勘误.docx'): '主持人手册',
                 ('开本资料', '勘误：10号线索卡.txt'): '线索',
                 ('人物剧本', '开本指南内页.pdf'): '主持人手册',
                 ('开本资料', '复盘相关内容', '剧本内容物清单.pdf'): '主持人手册',  # a list, not a role book
                 ('道具清单.docx',): '线索',
                 ('结局解析（图片1张）', ''): '主持人手册'}
        for parts, expected in cases.items():
            self.assertEqual(assemble.doc_type(list(parts)), expected, parts)

    def test_single_page_pdf_folders_join(self):
        records = {f'b/角色/SKM_{i}.pdf': {'pages': 1} for i in range(3)}
        records.update({'b/线索/a.pdf': {'pages': 1}, 'b/线索/b.pdf': {'pages': 1},   # only 2: kept apart
                        'b/剧本/a.pdf': {'pages': 1}, 'b/剧本/b.pdf': {'pages': 1}, 'b/剧本/c.pdf': {'pages': 9}})
        rows = [{'source': s, 'kind': 'pdf'} for s in records]
        self.assertEqual(assemble.page_pdf_folders(rows, records), {'b/角色'})

    def test_ending_reader_from_its_words(self):
        player = '你看着窗外的雨，想起了那天。你终于明白，' * 20
        host = '请主持人宣读以下内容。请所有玩家闭眼，播放BGM。主持人念：大雨落下。' * 20
        self.assertEqual(assemble.ending_reader_from_text(player), '人物剧本')
        self.assertEqual(assemble.ending_reader_from_text(host), '主持人手册')
        self.assertIsNone(assemble.ending_reader_from_text('结局'))   # too short to tell

    def test_wrapper_folders_do_not_decide_the_type(self):
        types = assemble.book_types('0014_蜕变俱乐部', [
            ('012-蜕变俱乐部（7人）欧洲烧脑机制反转', '91蜕变俱乐部', '安娜.pdf'),   # genre label 机制 is ignored
            ('012-蜕变俱乐部（7人）欧洲烧脑机制反转', '91蜕变俱乐部', '线索', '')])
        self.assertEqual(types, ['其他', '线索'])
        self.assertEqual(assemble.book_types('2772_腐萤哀鸣之刻（单手册）', [('腐萤哀鸣之刻主持人手册', '腐萤手册.pdf')]),
                         ['主持人手册'])   # an unmistakable wrapper name is used when nothing else matched
        self.assertEqual(assemble.book_types('1090_答案', [('976答案----------', '人物剧本', '墨镜男人', ''),
                                                          ('976答案----------', '')]), ['人物剧本', '其他'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', type=Path, help='optional JSON log with exact code and dependency versions')
    args = parser.parse_args()
    log = io.StringIO()
    started = common.now()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    print(log.getvalue(), end='')
    if args.record:
        common.write_json(args.record, {
            'started': started, 'finished': common.now(), 'command': sys.argv,
            'python': sys.version, 'platform': platform.platform(),
            'packages': {p: importlib.metadata.version(p) for p in ('pymupdf', 'pillow', 'pillow-heif', 'openpyxl', 'rapidfuzz')},
            'code_snapshot': {p.name: {'sha256': common.sha256(p), 'source': p.read_text(encoding='utf-8')}
                              for p in sorted(common.HERE.iterdir()) if p.suffix in ('.py', '.txt', '.md')},
            'tests': result.testsRun, 'passed': result.wasSuccessful(), 'log': log.getvalue(),
            'log_sha256': hashlib.sha256(log.getvalue().encode()).hexdigest(),
            'scope': 'Synthetic offline fixtures; no live downloads, provider requests, or real-corpus accuracy validation.'})
    raise SystemExit(0 if result.wasSuccessful() else 1)
