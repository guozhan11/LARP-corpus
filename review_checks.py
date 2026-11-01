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
from unittest.mock import patch

import common
import download
import snapshot
import ocr
import evaluate
import make_book_list
from PIL import Image


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
        for module in (common, download, snapshot, ocr, evaluate, make_book_list):
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
                   for i in range(1, 853)]
        scope = self.root / 'scope.json'
        common.write_json(scope, {'rows': entries})
        manifest = self.root / 'manifest.csv'
        common.write_csv(manifest, [{**e['selected'], 'url_with_pwd': 'https://pan.baidu.com/s/synthetic'}
                                   for e in entries], ['idx', 'folder', 'url_with_pwd'])
        self.call(make_book_list, '--scope', str(scope), '--manifest', str(manifest))
        books = self.root / 'books.csv'
        self.assertEqual(len(common.read_csv(books)), 852)
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
            texts = [t for t, _ in ocr.transcribe_all((str(i).encode() for i in range(12)), None)]
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
