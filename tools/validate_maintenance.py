"""Offline backup/restore/recovery drill on copies. Never alters main-library records/PDFs."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import collector


def command(*arguments, expected=0):
    result = subprocess.run([sys.executable, str(BASE / 'collector.py'), *map(str, arguments)],
                            cwd=BASE, capture_output=True, text=True, encoding='utf-8', timeout=120)
    if result.returncode != expected:
        raise RuntimeError(f'{arguments[0]} exited {result.returncode}: {result.stdout}\n{result.stderr}')
    return json.loads(result.stdout)


def table_rows(root):
    with closing(sqlite3.connect(root / 'library.sqlite3')) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: db.execute('SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY rowid').fetchall()
                for table in tables}


def file_hashes(root):
    with closing(sqlite3.connect(root / 'library.sqlite3')) as db:
        paths = [r[0] for r in db.execute("SELECT DISTINCT pdf_path FROM records WHERE pdf_status='downloaded'")]
    result = {}
    for name in paths:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('Invalid library PDF path')
        with path.open('rb') as handle:
            result[name] = hashlib.file_digest(handle, 'sha256').hexdigest()
    return result


def main():
    root = BASE / 'data'
    with closing(sqlite3.connect((root / 'library.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA user_version').fetchone()[0] != collector.SCHEMA_VERSION:
            raise ValueError('Upgrade the main library first; this drill does not migrate it')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    work = BASE / 'tmp' / 'maintenance-validation' / stamp
    work.mkdir(parents=True)
    before = table_rows(root)
    hashes = file_hashes(root)
    result = dict(started=datetime.now(timezone.utc).isoformat(), directory=str(work))
    result['main_integrity'] = command('verify')
    result['main_status'] = command('status')
    archive = work / 'library.zip'
    restored = work / 'restored'
    result['backup'] = command('backup', '--output', archive)
    result['restore'] = command('restore', '--archive', archive, '--data-dir', restored)
    assert table_rows(restored) == before, 'Restored database tables differ'
    assert file_hashes(restored) == hashes, 'Restored PDFs differ'
    result['exact_roundtrip'] = True
    result['reclassification'] = command('reclassify', '--data-dir', restored)
    assert file_hashes(restored) == hashes
    with closing(sqlite3.connect(restored / 'library.sqlite3')) as db:
        pending = db.execute("SELECT id FROM records WHERE pdf_status!='downloaded' ORDER BY id LIMIT 1").fetchone()
    if pending:
        result['scoped_retry'] = command('retry', '--data-dir', restored, '--record-id', pending[0])
        assert result['scoped_retry']['queued_records'] == [pending[0]]
    # Deliberately damage only a restored COPY, retaining its PDF header.
    if hashes:
        name = next(iter(hashes))
        damaged = (restored / name).resolve()
        assert damaged.is_relative_to(work.resolve())
        damaged.write_bytes(b'%PDF-1.7\nintentional recovery drill corruption\n%%EOF')
        result['detected_corruption'] = command('verify', '--data-dir', restored, expected=2)
        result['repair'] = command('verify', '--repair', '--data-dir', restored, expected=2)
        assert result['repair']['repaired_records'] >= 1
        result['after_repair'] = command('verify', '--data-dir', restored)
        assert result['after_repair']['status'] == 'ok'
    # Main tables and file bytes must remain exactly as before this drill.
    assert table_rows(root) == before, 'Main database rows changed during offline drill'
    assert file_hashes(root) == hashes, 'Main PDFs changed during offline drill'
    result.update(status='ok', main_library_unchanged=True, main_pdf_files=len(hashes),
                  finished=datetime.now(timezone.utc).isoformat())
    (work / 'validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'maintenance_validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(status=result['status'], work=str(work), pdf_files=len(hashes),
                          exact_roundtrip=True, main_library_unchanged=True), indent=2))


if __name__ == '__main__':
    main()
