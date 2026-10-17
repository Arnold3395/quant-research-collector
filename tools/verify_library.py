"""Offline integrity audit; no network, AI, or third-party packages."""
import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
import sys


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.local = []

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get('href', '')
        if tag == 'a' and href.startswith('pdf/'):
            self.local.append(href)


def verify(root):
    root = Path(root).resolve()
    errors = []
    db = sqlite3.connect((root / 'library.sqlite3').as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        integrity = [r[0] for r in db.execute('PRAGMA integrity_check')]
        if integrity != ['ok']:
            errors.append('SQLite integrity: ' + repr(integrity))
        rows = db.execute('SELECT * FROM records').fetchall()
        pdfs = {r['pdf_path']: r['sha256'] for r in rows if r['pdf_status'] == 'downloaded'}
        for relative, expected in pdfs.items():
            path = (root / relative).resolve()
            if not relative or not path.is_relative_to(root):
                errors.append('Invalid PDF path: ' + relative)
                continue
            try:
                digest = hashlib.sha256()
                with path.open('rb') as handle:
                    first = handle.read(1024)
                    if not first.lstrip().startswith(b'%PDF-'):
                        errors.append('Invalid PDF header: ' + relative)
                    digest.update(first)
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(block)
                if digest.hexdigest() != expected:
                    errors.append('PDF checksum mismatch: ' + relative)
            except OSError as error:
                errors.append(str(error))
        catalog = [json.loads(line) for line in (root / 'catalog.jsonl').read_text(encoding='utf-8').splitlines() if line]
        catalog_by_id = {r['id']: r for r in catalog}
        if len(catalog_by_id) != len(catalog) or set(catalog_by_id) != {r['id'] for r in rows}:
            errors.append('JSONL IDs/count differ from SQLite (possibly a run is still active)')
        for row in rows:
            item = catalog_by_id.get(row['id'])
            if item and any(item[k] != row[k] for k in ('document_id', 'source', 'pdf_status', 'pdf_path', 'sha256')):
                errors.append('JSONL record differs from SQLite: ' + str(row['id']))
        links = Links()
        links.feed((root / 'index.html').read_text(encoding='utf-8'))
        for href in links.local:
            if not (root / href).is_file():
                errors.append('Broken local HTML link: ' + href)
        last = db.execute('SELECT id,status FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
        status = json.loads((root / 'last_run.json').read_text(encoding='utf-8'))
        if last is None or last['status'] == 'running' or last['id'] != status['run_id']:
            errors.append('Latest run/export is not complete')
        if last and last['status'] != status['status']:
            errors.append('Run status differs between SQLite and last_run.json')
        return dict(status='ok' if not errors else 'failed',
                    documents=db.execute('SELECT count(*) FROM documents').fetchone()[0], records=len(rows),
                    pdf_files=len(pdfs), local_pdf_links=len(links.local),
                    download_states=dict(db.execute('SELECT pdf_status,count(*) FROM records GROUP BY pdf_status').fetchall()),
                    latest_run_id=last['id'] if last else None,
                    latest_run_status=last['status'] if last else None, errors=errors)
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default=str(Path(__file__).resolve().parents[1] / 'data'))
    parser.add_argument('--output', help='Optional JSON audit report path')
    args = parser.parse_args()
    try:
        report = verify(args.data_dir)
    except Exception as error:
        report = dict(status='failed', errors=[str(error)])
    content = json.dumps(report, ensure_ascii=False, indent=2)
    print(content)
    if args.output:
        Path(args.output).write_text(content, encoding='utf-8')
    sys.exit(0 if report['status'] == 'ok' else 1)
