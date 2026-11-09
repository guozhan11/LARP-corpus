"""Join the per-file OCR text into whole documents, book by book.

Each PDF, Word, slide, sheet or text file is one document. Images in the same
folder are one document, in natural filename order (军官_2.jpg before 军官_10.jpg),
and so are the PDFs of a folder holding at least 3 PDFs that are all one page
(a role book scanned page by page: SKM_3671911230721.pdf …).
Every page starts with a marker naming its source file and page, so any line
can be traced back to the original. Pages without text say why. Pages the main
model could not read but step 04c filled (another model) are used, and counted
in documents.csv (filled_pages); ocr/<name>_fill/pages.csv says which model.

Each document gets a type from its path inside the book: 宣传与售后 if a folder
on the path is an after-sales or promotion folder (售后/宣传); otherwise the first
level, from the file name up, whose name matches: 宣传与售后, then strong host
words, then 线索, then weak host words (阶段, 结局, 诡计详解, 勘误, 导演 …), then 人物剧本
(not for a list: 剧本内容物清单). A level
matching both 剧本 and 线索 (剧本及线索) or nothing is skipped. A cover or
character art (封面/封皮/立绘) in the file's own name or the folder it sits in is
宣传与售后; higher up, or in a print-spec folder (封面200g铜版纸…), it is ignored.
An ending goes by who reads it: a character's ending (李湛结局, 人物结局) is
人物剧本; one the host reads out (结局小剧场（主持人端）) is 主持人手册; a bare 结局
falls through to the folders above (结局 there counts as weak host). Unless its name
says host, clue or promotion (附录-线索列表 in a folder 资料（线索列表，结局，附录） stays
a clue; bracket notes do not make an ending folder), an ending's own words come next, when they are clear: 你 throughout (≥10 per 1,000 characters,
few host cues) means a player reads it; host cues (主持人, 请玩家, 宣读, BGM:
≥3 per 1,000) with little 你 (<5) mean the host reads it out. Bracketed notes are ignored (B本（黄色封面）). Act names (第一幕) are not
used: role, clue and host material are all divided by act. Otherwise 其他.
Wrapper folders say nothing about any one document and are left out: the folders
every file of the book sits in. Folders named after the book keep only what is
left after removing the title, brackets, digits, advertisements and genre labels
such as 机制/情感/主持人 (痛仰线索 → 线索; 绝笔（6人中式机制本） → nothing). Only
when nothing else matched are the wrappers' own names used, cleaned the same way
(腐萤哀鸣之刻主持人手册 → 主持人手册; 976答案----- → nothing).
The type is a guess from names only; documents.csv lists it for review.

python 06_assemble.py --ocr batch1 [--books 0001 0035]   # --books replaces only those books in the tables
"""
import argparse
import re
import shutil
from collections import defaultdict
from pathlib import Path, PurePosixPath
from common import DATA, read_csv, read_json, record_run, finish_run, write_csv

TYPES = [   # checked in this order within each path part; the first match decides
    ('宣传与售后', r'海报|宣传|售后|测评|logo|LOGO|邀请函|易拉宝|链接|广告|二维码'),
    ('主持人手册', r'主持|组织|DM|dm|Dm|复盘|开本手册|真相|答案'),
    ('线索', r'线索|搜证|证物|卡(?!洛|门|尔|特|萨|罗|莉|琳|西|斯|拉|米|伦|夫|丽)|道具|地图|附件|亲笔|信件|名牌|照片|配件|结婚证'),   # 卡 but not names such as 卡洛斯
    ('主持人手册', r'流程|开场|结局|演绎|机制|阶段|诡计|详解|解析|勘误|开本指南|导演|引导手册'),   # weaker host words: 阶段线索卡, 线索详解 are clues
    ('人物剧本', r'剧本|人物|角色|本子'),
]
GENRE = r'机制|演绎|推理|情感|欢乐|恐怖|硬核|本格|阵营|沉浸|开放|还原|反转|烧脑|变格|无需(DM|dm)?(主持人?)?'   # labels in book-named folders
TEXT_KINDS = {'pdf', 'word', 'slides', 'sheet', 'text', 'image'}


CN_DIGITS = {c: i for i, c in enumerate('零一二三四五六七八九')}


def cn_number(t):
    """一 → 1, 十一 → 11, 二十三 → 23 (page names such as 第十一页)."""
    if '十' not in t:
        return int(''.join(str(CN_DIGITS[c]) for c in t))
    tens, _, ones = t.partition('十')
    return (CN_DIGITS[tens] if tens else 1) * 10 + (CN_DIGITS[ones] if ones else 0)


def natural_key(name):
    key = []
    for t in re.split(r'(\d+|[一二三四五六七八九十]+)', name):
        if t.isdigit():
            key.append((0, int(t), ''))
        elif t and re.fullmatch(r'[一二三四五六七八九十]+', t) and t.count('十') <= 1:
            key.append((0, cn_number(t), ''))
        else:
            key.append((1, 0, t.lower()))
    return key


AFTER_SALES = r'售后|宣传'
COVER = r'封面|封皮|立绘'
PRINT_SPEC = r'\d+ ?[克g]|铜版|铜板|双胶|内页|骑马钉'   # 封面200g铜版纸覆膜-内页80g… is a print spec   # a folder of these holds only promotion or after-sales material


def stem(part):
    """A path part without extension or bracketed notes (B本（黄色封面）→ B本)."""
    part = re.sub(r'\.[A-Za-z0-9]{1,5}$', '', part)
    return re.sub(r'[（(【\[][^）)】\]]*[）)】\]]', '', part)


def ending_reader(name):
    """An ending (结局) belongs to whoever reads it: 李湛结局 is handed to that player,
    结局阶段小剧场（主持人端） is read out by the host. None when the name does not say."""
    if '结局' not in name or re.search(r'卡|布局', name):
        return None   # 结局卡 is a clue card
    if re.search(r'主持|DM|dm|组织|念白|朗读|演绎|小剧场|复盘|真相|开法|建议|说明|指引|流程|解析|详解', name):
        return '主持人手册'
    generic = r'终极|最终|大|真|隐藏|开放|好|坏|普通|完美|阵营|全员|总|合集|所有|全部|分支|多|各|游戏|剧本|故事|版本|本|篇|卡|第.+[幕章]'
    before, _, after = re.sub(r'\.[A-Za-z0-9]{1,5}|《[^》]*》', '', name).partition('结局')   # 《神乐汤》 is the book
    clean = lambda t: re.sub(r'[A-Za-z\d\s_\-—·.、:：（）()【】]+|的$|^本|纯图版|已压缩', '', t)
    for side in (clean(before), clean(after)):   # 李湛结局, 结局本-彭如愿, 隐藏结局夏小池
        side = re.sub('^(' + generic + ')+', '', side)
        if 2 <= len(side) <= 8 and not re.fullmatch('(' + generic + ')+', side):
            return '人物剧本'
    return None


HOST_CUES = r'主持人|DM|dm|请.{0,4}玩家|各位玩家|所有玩家|玩家们|大家|宣读|朗读|旁白|播放|BGM|bgm'


def ending_reader_from_text(text):
    """Who an ending is written for, from its words: 你 throughout means a player reads it;
    cues to the host (主持人, 请玩家…, 宣读, BGM) with little 你 mean the host reads it out."""
    n = len(text) / 1000
    if n < 0.2:
        return None   # too short to tell
    you, host = text.count('你') / n, len(re.findall(HOST_CUES, text)) / n
    if host >= 3 and you < 5:
        return '主持人手册'
    if you >= 10 and host < 1.5:
        return '人物剧本'
    return None


def doc_type(parts):
    """parts: path parts inside the book, file name last."""
    if any(re.search(AFTER_SALES, stem(p)) for p in parts[:-1]):
        return TYPES[0][0]
    near = [stem(p) for p in parts if p][-2:]   # the file's own name and the folder it sits in
    if any(re.search(COVER, p) and not re.search(PRINT_SPEC, p) for p in near):
        return TYPES[0][0]   # covers and character art (在人间封面, 人物立绘), not print specs
    ending = ending_reader(near[-1]) if near else None
    if ending:
        return ending
    for part in reversed(parts):
        hits = [name for name, pattern in TYPES if re.search(pattern, stem(part))]
        if '清单' in part and '人物剧本' in hits:
            hits.remove('人物剧本')   # 剧本内容物清单 is a list of the box's contents
        if '人物剧本' in hits and '线索' in hits:
            continue   # a mixed folder (剧本及线索, 剧本+线索) does not decide; look further up
        if hits:
            return hits[0]
    return '其他'


def book_title(book):
    return re.sub(r'[（(【《].*', '', book.split('_', 1)[-1]).strip()


def clean_wrapper(part, title):
    """A wrapper folder's name without the title, brackets, digits or a reseller's advert."""
    part = re.split(r'-{3,}|—{2,}', part)[0]                  # 1040人间星火---------新上市剧本获取…
    part = re.sub(r'[（(【《\[][^）)】》\]]*[）)】》\]]', '', part)  # （8人欢乐机制本） 【无水印】 《…》
    return re.sub(r'\d+', '', part.replace(title, '')).strip(' -_')


def book_types(book, docs_parts):
    """Type per document: inside the book's wrapper folders first, the wrappers' names last."""
    folders = [p[:-1] for p in docs_parts]
    common = 0
    for level in zip(*folders):
        if len(set(level)) > 1:
            break
        common += 1
    title = book_title(book)
    types = []
    for parts in docs_parts:
        # wrappers: folders shared by the whole book. Folders named after the book keep
        # only what follows the title, without genre labels (痛仰线索 → 线索, 绝笔（6人中式机制本） → '')
        wrap = list(range(min(common, len(parts) - 1)))
        inner = []
        for i, p in enumerate(parts):
            if i in wrap:
                continue
            if i < len(parts) - 1 and len(title) >= 2 and title in p:
                p = re.split(r'-{3,}|—{2,}', p.replace(title, ''))[0]   # keep bracket contents: 【孽障·剧本】 → 剧本
                p = re.sub(GENRE + r'|\d+|[（）()【】《》\[\]]', '', p)
            inner.append(p)
        t = doc_type(inner)
        if t == '其他':   # last, the wrappers' own names, without title, adverts or genre labels
            names = [re.sub(GENRE, '', clean_wrapper(parts[i], title)) for i in wrap]
            names = [n for n in names if n.strip(' -_')]
            if names:
                t = doc_type(names + [''])
        types.append(t)
    return types


def safe(part):
    return part.replace('!', '／')   # files from inside an archive: archive.zip!inner/path


def page_text(ocr_dir, row, record, fills=None):
    """List of (marker suffix, text) per page, how many pages are missing, how many filled.

    `fills` maps page (or tile) number to step 04c's text for this file, None where
    04c could not fill it either; 04c lists every page that had no text."""
    fills = fills or {}
    path = ocr_dir / 'text' / (row['source'] + '.txt')
    status = (record or {}).get('status')
    if status == 'failed' and fills:   # every page failed in step 04; 04c rendered them all
        pages, failed = [''] * max(fills), set(range(1, max(fills) + 1))
    elif status not in ('done', 'partial') or not path.exists():
        why = 'OCR 失败' if record else '未做 OCR'
        return [(None, f'〔整份缺失：{why}〕')], 1, 0
    else:
        pages = path.read_text(encoding='utf-8').split('\f')
        failed = {int(n) for n in re.findall(r'\d+', str(record.get('failed_pages') or ''))}
    fills = {i: t for i, t in fills.items() if t is not None and i in failed}
    lost = len(failed) - len(fills)
    if row['kind'] == 'image':   # a tall image is read in tiles; one page, with any lost tile marked
        tiles = [fills.get(i, '〔本块缺失：OCR 失败〕') if i in failed else t for i, t in enumerate(pages, 1)]
        return [(None, '\n'.join(t.strip() for t in tiles).strip())], lost, len(fills)
    out = []
    for i, t in enumerate(pages, 1):
        out.append((i, (fills.get(i, '〔本页缺失：OCR 失败〕') if i in failed else t).strip()))
    return out, lost, len(fills)


PAGE_PDFS = 3   # a folder of at least this many PDFs, every one a single page, is one document


def page_pdf_folders(rows, records):
    """Folders whose PDFs are single scanned pages (SKM_3671911230721.pdf …): joined like images."""
    pdfs = defaultdict(list)
    for row in rows:
        if row['kind'] == 'pdf':
            pdfs[str(PurePosixPath(row['source']).parent)].append(row)
    return {folder for folder, members in pdfs.items() if len(members) >= PAGE_PDFS
            and all((records.get(r['source']) or {}).get('pages') == 1 for r in members)}


def assemble_book(book, rows, ocr_dir, records, out_dir, fills=None):
    groups = defaultdict(list)   # (folder, file or None) -> rows
    paged = page_pdf_folders(rows, records)
    for row in rows:
        rel = PurePosixPath(row['source']).relative_to(book)
        if row['kind'] == 'image' or (row['kind'] == 'pdf' and str(PurePosixPath(row['source']).parent) in paged):
            groups[(str(rel.parent), None)].append(row)
        else:
            groups[(str(rel.parent), rel.name)].append(row)
    docs, all_parts = [], []
    for (folder, name), members in sorted(groups.items(), key=lambda g: (natural_key(g[0][0]), natural_key(g[0][1] or ''))):
        members.sort(key=lambda r: natural_key(PurePosixPath(r['source']).name))
        folder_parts = [] if folder == '.' else list(PurePosixPath(folder).parts)
        label = folder_parts[-1] if folder_parts else book
        if name:
            parts, target = folder_parts + [name], folder_parts + [name + '.txt']
        else:
            parts = folder_parts + ['']   # an image folder: its folders, no file name
            kinds = {r['kind'] for r in members}
            count = f'图片{len(members)}张' if kinds == {'image'} else f'{len(members)}页'
            target = folder_parts[:-1] + [f'{label}（{count}）.txt']
        blocks, pages, missing, filled = [], 0, 0, 0
        for row in members:
            fname = PurePosixPath(row['source']).name
            got, lost, mended = page_text(ocr_dir, row, records.get(row['source']), (fills or {}).get(row['source']))
            missing += lost
            filled += mended
            for page, text in got:
                pages += 1
                where = fname if page is None or len(got) == 1 else f'{fname} 第{page}页'
                blocks.append(f'=== {where} ===\n{text}'.rstrip())
        path = out_dir / book / Path(*[safe(p) for p in target])
        if path.exists():   # e.g. a file literally named 张三（图片3张）.txt beside that image folder
            raise SystemExit(f'two documents would be written to {path}')
        path.parent.mkdir(parents=True, exist_ok=True)
        body = '\n\n'.join(blocks) + '\n'
        path.write_text(body, encoding='utf-8')
        docs.append({'book': book, 'document': str(path.relative_to(out_dir / book)),
                     'files': len(members), 'pages': pages,
                     'missing_pages': missing, 'filled_pages': filled, 'chars': len(body),
                     'kind': members[0]['kind'] if len({r['kind'] for r in members}) == 1 else 'mixed'})
        all_parts.append(tuple(parts))
        d = docs[-1]
        own = stem(name or label)
        # Endings: by stems, so 资料（线索列表，结局，附录） is not an ending folder. A name that
        # says host (演绎手册), clue (线索列表), promotion, or 卡/布局 stands over the text.
        endings = [stem(q) for q in parts if '结局' in stem(q)]
        if (endings and not any(re.search(r'卡|布局', q) for q in endings + [own])
                and ending_reader(own) != '主持人手册'
                and not any(re.search(pattern, own) for _, pattern in TYPES[:3])):
            text = re.sub(r'^=== .* ===$', '', body, flags=re.M)
            d['reader_from_text'] = ending_reader_from_text(text) or ''
    for d, t in zip(docs, book_types(book, all_parts)):
        d['type'] = d.get('reader_from_text') or t   # an ending's own words outrank its name
    return docs


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ocr', required=True, help='name of the OCR run under ocr/ (and its snapshot)')
    p.add_argument('--books', nargs='*', help='only books whose folder starts with these numbers (for trying it out)')
    a = p.parse_args()
    ocr_dir, snap = DATA / 'ocr' / a.ocr, DATA / 'snapshot' / a.ocr
    out_dir = DATA / 'assembled' / a.ocr
    fill_csv = DATA / 'ocr' / (a.ocr + '_fill') / 'pages.csv'
    run = record_run('06_assemble', vars(a), [snap / 'files.csv', *([fill_csv] if fill_csv.exists() else [])])
    records = {}
    for f in (ocr_dir / 'files').glob('*.json'):
        r = read_json(f)
        records[r['source']] = r
    fills = defaultdict(dict)   # source -> page -> text, from step 04c when it was run
    fill_dir = DATA / 'ocr' / (a.ocr + '_fill')
    if (fill_dir / 'pages.csv').exists():
        for r in read_csv(fill_dir / 'pages.csv'):
            fills[r['source']][int(r['page'])] = (
                (fill_dir / 'text' / f"{r['source']}.{r['page']}.txt").read_text(encoding='utf-8') if r['model'] else None)
    by_book = defaultdict(list)
    for row in read_csv(snap / 'files.csv'):
        if row['kind'] in TEXT_KINDS and (not a.books or row['book'].split('_')[0] in a.books):
            by_book[row['book']].append(row)
    if out_dir.exists() and not a.books:
        shutil.rmtree(out_dir)   # a full run rebuilds everything from the OCR text
    docs, books = [], []
    for book in sorted(by_book):
        if a.books and (out_dir / book).exists():
            shutil.rmtree(out_dir / book)
        got = assemble_book(book, by_book[book], ocr_dir, records, out_dir, fills)
        docs += got
        count = defaultdict(int)
        for d in got:
            count[d['type']] += 1
        books.append({'book': book, 'documents': len(got), **{t: count[t] for t, _ in TYPES}, '其他': count['其他'],
                      'pages': sum(d['pages'] for d in got),
                      'missing_pages': sum(d['missing_pages'] for d in got),
                      'filled_pages': sum(d['filled_pages'] for d in got),
                      'chars': sum(d['chars'] for d in got)})
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.books:   # a partial run replaces only its own books in the full tables
        done = set(by_book)
        if (out_dir / 'documents.csv').exists():
            docs = [d for d in read_csv(out_dir / 'documents.csv') if d['book'] not in done] + docs
            books = [b for b in read_csv(out_dir / 'books.csv') if b['book'] not in done] + books
        docs.sort(key=lambda d: d['book'])   # stable: a book's documents keep their order
        books.sort(key=lambda b: b['book'])
    write_csv(out_dir / 'documents.csv', docs, ['book', 'document', 'type', 'reader_from_text', 'kind', 'files', 'pages', 'missing_pages', 'filled_pages', 'chars'])
    write_csv(out_dir / 'books.csv', books, ['book', 'documents', *[t for t, _ in TYPES], '其他', 'pages', 'missing_pages', 'filled_pages', 'chars'])
    finish_run(run, [out_dir / 'documents.csv', out_dir / 'books.csv',
                     *sorted(out_dir / b / d for b, d in ((d['book'], d['document']) for d in docs))],
               books=len(books), documents=len(docs))
    print(f'{len(books)} books, {len(docs)} documents -> {out_dir}')


if __name__ == '__main__':
    main()
