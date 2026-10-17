import argparse
from contextlib import closing, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import io
import json
import logging
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import collector as c
import maintenance as m
from test_collector import SOURCE, FakeClient, temporary_directory


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.name)
        self.config = dict(collector=dict(max_run_minutes=1, initial_days=14, overlap_days=3),
                           filter=dict(topics={'Risk': ['risk']}, minimum_score=1), sources=[SOURCE])
        self.args = argparse.Namespace(data_dir=str(self.root), sources=None, days=None,
                                       metadata_only=True, max_downloads=None, force_sources=False)
        self.paper = c.Paper('feed', 'one', 'Risk model', 'https://example.org/one', updated=c.stamp())

    def tearDown(self):
        logging.shutdown()
        self.temp.cleanup()

    def run_collection(self, payload=None, error=None):
        fake = FakeClient()
        fake.metrics = {}
        with patch.object(c, 'Client', return_value=fake), patch.object(c, 'fetch_source',
                return_value=([payload or self.paper], False), side_effect=error) as fetch, redirect_stdout(io.StringIO()):
            code = c.collect(self.args, self.config, self.root)
        return code, fetch

    def test_watermark_overlap_and_configuration_change(self):
        self.assertEqual(self.run_collection()[0], 0)
        with closing(sqlite3.connect(self.root / 'library.sqlite3')) as db:
            # Distinguish incremental 3-day overlap from initial 14-day window.
            db.execute('UPDATE source_state SET last_success=?', (c.stamp(c.now() - timedelta(days=1)),))
            db.commit()
        code, call = self.run_collection()
        self.assertEqual(code, 0)
        self.assertAlmostEqual((c.now() - call.call_args.args[2]).total_seconds() / 86400, 4, places=3)
        self.config['filter']['topics']['Risk'].append('liquidity')
        code, call = self.run_collection()
        self.assertAlmostEqual((c.now() - call.call_args.args[2]).total_seconds() / 86400, 14, places=3)
        info = json.loads((self.root / 'last_run.json').read_text())
        self.assertTrue(info['sources'][0]['configuration_changed'])
        self.assertEqual((info['new_documents'], info['new_records']), (0, 0))
        self.assertEqual(len(list((self.root / 'config_history').glob('*.json'))), 2)

    def test_failed_source_cools_down_and_force_can_retry(self):
        self.assertEqual(self.run_collection(error=RuntimeError('outage'))[0], 2)
        code, call = self.run_collection()
        self.assertEqual(code, 2)
        call.assert_not_called()
        self.args.force_sources = True
        self.assertEqual(self.run_collection()[0], 0)
        lib = c.Library(self.root)
        try:
            health = lib.db.execute('SELECT * FROM source_health').fetchone()
            self.assertEqual((health['failures'], health['next_retry']), (0, ''))
            self.assertEqual(m.status(lib, self.config, [SOURCE], c)['status'], 'ok')
        finally:
            lib.db.close()

    def test_failed_changed_configuration_does_not_lose_backfill(self):
        self.run_collection()
        self.config['filter']['topics']['Risk'].append('portfolio')
        self.run_collection(error=RuntimeError('outage'))
        self.assertEqual(self.run_collection()[0], 2)
        self.args.force_sources = True
        code, call = self.run_collection()
        self.assertEqual(code, 0)
        self.assertAlmostEqual((c.now() - call.call_args.args[2]).total_seconds() / 86400, 14, places=3)

    def test_downloaded_item_is_never_requested_on_repeated_collection(self):
        self.args.metadata_only = False
        self.args.max_downloads = 5
        paper = replace(self.paper, pdf_url='https://example.org/a.pdf')
        with patch.object(c, 'Client', side_effect=lambda *a: FakeClient()), patch.object(c, 'fetch_source', return_value=([paper], False)), redirect_stdout(io.StringIO()):
            self.assertEqual(c.collect(self.args, self.config, self.root), 0)
        fake = FakeClient(fail=True)
        with patch.object(c, 'Client', return_value=fake), patch.object(c, 'fetch_source', return_value=([paper], False)), redirect_stdout(io.StringIO()):
            self.assertEqual(c.collect(self.args, self.config, self.root), 0)
        self.assertEqual(fake.calls, [])
        info = json.loads((self.root / 'last_run.json').read_text())
        self.assertEqual((info['new_records'], info['downloads']), (0, {}))

    def test_interrupt_has_final_run_status_immediately(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_collection(error=KeyboardInterrupt())
        lib = c.Library(self.root)
        try:
            self.assertEqual(lib.db.execute('SELECT status FROM runs').fetchone()[0], 'interrupted')
            self.assertIsNone(lib.success('feed'))
        finally:
            lib.db.close()

    def test_truncated_scan_keeps_the_existing_watermark(self):
        self.run_collection()
        lib = c.Library(self.root)
        before = lib.success('feed')
        lib.db.close()
        with patch.object(c, 'Client', return_value=FakeClient()), patch.object(c, 'fetch_source', return_value=([self.paper], True)), redirect_stdout(io.StringIO()):
            self.assertEqual(c.collect(self.args, self.config, self.root), 2)
        lib = c.Library(self.root)
        try:
            self.assertEqual(lib.success('feed'), before)
        finally:
            lib.db.close()

    def test_log_rotation_limits_retained_log_files(self):
        self.config['collector'].update(log_max_mb=0.0001, log_backups=1)
        self.run_collection()
        self.run_collection()
        self.assertTrue((self.root / 'collector.log.1').is_file())
        self.assertFalse((self.root / 'collector.log.2').exists())

    def test_failed_pdf_retry_wait_grows_with_attempt_count(self):
        self.run_collection()
        lib = c.Library(self.root)
        lib.db.execute("UPDATE records SET pdf_status='download_failed',attempts=2,last_attempt=?", (c.stamp(c.now() - timedelta(hours=30)),))
        lib.db.commit()
        lib.db.close()
        self.args.metadata_only, self.args.max_downloads = False, 5
        fake = FakeClient(fail=True)
        with patch.object(c, 'Client', return_value=fake), patch.object(c, 'fetch_source', return_value=([self.paper], False)), redirect_stdout(io.StringIO()):
            self.assertEqual(c.collect(self.args, self.config, self.root), 0)
        self.assertEqual(fake.calls, [])

    def test_discovery_budget_deferral_does_not_trigger_source_failure_cooldown(self):
        self.assertEqual(self.run_collection(error=c.RunBudgetExceeded('budget'))[0], 2)
        lib = c.Library(self.root)
        try:
            self.assertIsNone(lib.success('feed'))
            self.assertEqual(lib.db.execute('SELECT count(*) FROM source_health').fetchone()[0], 0)
        finally:
            lib.db.close()

    def test_manual_short_backfill_does_not_jump_over_incremental_gap(self):
        self.run_collection()
        lib = c.Library(self.root)
        old = c.stamp(c.now() - timedelta(days=60))
        lib.db.execute('UPDATE source_state SET last_success=?', (old,))
        lib.db.commit()
        lib.db.close()
        self.args.days = 1
        self.assertEqual(self.run_collection()[0], 0)
        lib = c.Library(self.root)
        try:
            self.assertEqual(c.stamp(lib.success('feed')), old)
        finally:
            lib.db.close()
        self.args.days = None
        self.config['filter']['topics']['Risk'].append('portfolio')
        code, call = self.run_collection()
        self.assertAlmostEqual((c.now() - call.call_args.args[2]).total_seconds() / 86400, 63, places=3)

    def test_manual_scan_does_not_hide_configuration_change_from_next_normal_run(self):
        self.run_collection()
        self.config['filter']['topics']['Risk'].append('portfolio')
        self.args.days = 1
        self.run_collection()
        self.args.days = None
        code, call = self.run_collection()
        self.assertAlmostEqual((c.now() - call.call_args.args[2]).total_seconds() / 86400, 14, places=3)

    def test_source_honors_long_retry_after_without_blocking_the_process(self):
        error = urllib.error.HTTPError(SOURCE['url'], 429, 'Too Many Requests', {'Retry-After': '172800'}, None)
        self.assertEqual(self.run_collection(error=error)[0], 2)
        lib = c.Library(self.root)
        try:
            row = lib.db.execute('SELECT * FROM source_health').fetchone()
            self.assertGreater((c.parse_date(row['next_retry']) - c.now()).total_seconds(), 172790)
        finally:
            lib.db.close()


class CacheTests(unittest.TestCase):
    def test_retry_after_accepts_http_dates_and_checks_wrapped_robot_errors(self):
        expected = c.now() + timedelta(hours=2)
        from email.utils import format_datetime
        error = urllib.error.HTTPError(SOURCE['url'], 503, 'Unavailable', {'Retry-After': format_datetime(expected, usegmt=True)}, None)
        wrapped = c.PolicyError('robots unavailable')
        wrapped.__cause__ = error
        self.assertLess(abs((c.error_retry_after(wrapped) - expected).total_seconds()), 1)

    def test_conditional_304_reuses_verified_persistent_response(self):
        with temporary_directory() as root:
            client = c.Client(dict(http_attempts=1, request_interval_seconds=0), [SOURCE])
            client.cache_root = Path(root)
            response = io.BytesIO(b'<rss>known bytes</rss>')
            response.headers = {'ETag': '"v1"', 'Last-Modified': 'Wed, 14 Oct 2026 00:00:00 GMT'}
            response.url = SOURCE['url']
            with patch.object(client, 'check_robots'), patch.object(client.opener, 'open', return_value=response):
                first = client.get(SOURCE['url'], conditional=True)
            # A new Client simulates the next process/run.
            client = c.Client(dict(http_attempts=1, request_interval_seconds=0), [SOURCE])
            client.cache_root = Path(root)
            error = urllib.error.HTTPError(SOURCE['url'], 304, 'Not Modified', {}, None)
            with patch.object(client, 'check_robots'), patch.object(client.opener, 'open', side_effect=error) as request:
                self.assertEqual(client.get(SOURCE['url'], conditional=True), first)
            headers = dict(request.call_args.args[0].header_items())
            self.assertEqual(headers['If-none-match'], '"v1"')
            self.assertEqual(client.metrics['bytes_received'], 0)
            self.assertEqual(client.metrics['not_modified'], 1)

    def test_corrupt_cache_is_not_used_or_sent_as_conditional(self):
        with temporary_directory() as root:
            path = Path(root) / (hashlib.sha256(SOURCE['url'].encode()).hexdigest() + '.json')
            path.write_text('{broken', encoding='utf-8')
            client = c.Client(dict(http_attempts=1, request_interval_seconds=0), [SOURCE])
            client.cache_root = Path(root)
            response = io.BytesIO(b'new')
            response.headers, response.url = {}, SOURCE['url']
            with patch.object(client, 'check_robots'), patch.object(client.opener, 'open', return_value=response) as request:
                self.assertEqual(client.get(SOURCE['url'], conditional=True)[0], b'new')
            self.assertNotIn('If-none-match', dict(request.call_args.args[0].header_items()))


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.name)
        self.lib = c.Library(self.root / 'library')
        self.paper = c.Paper('feed', 'one', 'Risk', 'https://example.org/p', pdf_url='https://example.org/p.pdf', version='v1')
        self.rid = self.lib.upsert(self.paper)[0]
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (self.rid,)).fetchone()
        self.lib.download(FakeClient(), row, SOURCE, {})
        self.config_path = self.root / 'config.toml'
        self.config_path.write_text('sources=[]\n[collector]\n', encoding='utf-8')

    def tearDown(self):
        self.lib.db.close()
        self.temp.cleanup()

    def test_metadata_revision_reuses_same_pdf_without_transfer(self):
        # A known identical revision marker allows reuse after metadata enrichment.
        rid = self.lib.upsert(replace(self.paper, version='v1', abstract='More complete abstract'))[0]
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        fake = FakeClient(fail=True)
        self.assertEqual(self.lib.download(fake, row, SOURCE, {}), 'reused_pdf')
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM download_events').fetchone()[0], 2)

    def test_new_version_at_same_url_is_downloaded(self):
        rid = self.lib.upsert(replace(self.paper, version='v2', updated=c.stamp()))[0]
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        fake = FakeClient(b'%PDF-1.7\nnew version\n%%EOF')
        self.assertEqual(self.lib.download(fake, row, SOURCE, {}), 'downloaded')
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(list(self.lib.root.rglob('*.pdf'))), 2)

    def test_checksum_corruption_is_requeued_even_with_valid_pdf_header(self):
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (self.rid,)).fetchone()
        path = self.lib.root / row['pdf_path']
        path.write_bytes(path.read_bytes().replace(b'fixture', b'changed'))
        self.assertEqual(m.integrity(self.lib)['status'], 'failed')
        self.assertEqual(self.lib.reconcile_downloads(), 1)
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (self.rid,)).fetchone()
        self.assertEqual((row['pdf_status'], row['pdf_path']), ('pending', ''))

    def test_backup_restore_roundtrip_and_no_overwrite(self):
        archive = self.root / 'backup.zip'
        report = m.backup(self.lib, archive, self.config_path, c)
        self.assertEqual(report['status'], 'ok')
        restored = self.root / 'restored'
        self.assertEqual(m.restore(archive, restored, c)['pdf_files'], 1)
        lib = c.Library(restored)
        try:
            self.assertEqual(m.integrity(lib)['status'], 'ok')
            self.assertEqual(lib.db.execute('SELECT metadata FROM records').fetchone()[0],
                             self.lib.db.execute('SELECT metadata FROM records').fetchone()[0])
        finally:
            lib.db.close()
        with self.assertRaisesRegex(ValueError, 'must not exist'):
            m.restore(archive, restored, c)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            m.backup(self.lib, archive, self.config_path, c)

    def test_restore_rejects_corruption_before_installing_library(self):
        archive = self.root / 'backup.zip'
        m.backup(self.lib, archive, self.config_path, c)
        corrupt = self.root / 'corrupt.zip'
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(corrupt, 'w') as output:
            for name in source.namelist():
                payload = source.read(name)
                if name.endswith('.pdf'):
                    payload = payload.replace(b'fixture', b'changed')
                output.writestr(name, payload)
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            m.restore(corrupt, self.root / 'bad-restore', c)
        self.assertFalse((self.root / 'bad-restore').exists())

    def test_restore_rejects_traversal(self):
        path = self.root / 'evil.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('../escape.txt', 'bad')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            m.restore(path, self.root / 'restore', c)

    def test_integrity_repair_preserves_record_and_queues_download(self):
        row = self.lib.db.execute('SELECT * FROM records').fetchone()
        (self.lib.root / row['pdf_path']).unlink()
        result = m.integrity(self.lib, repair=True)
        self.assertEqual(result['repaired_records'], 1)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 1)
        self.assertEqual(self.lib.db.execute('SELECT pdf_status FROM records').fetchone()[0], 'pending')

    def test_legacy_schema_migration_preserves_ids_downloads_and_backup(self):
        self.lib.db.execute('PRAGMA user_version=0')
        # Reproduce the original fingerprint algorithm from the previous release.
        old = hashlib.sha256(json.dumps([self.paper.title, self.paper.abstract, self.paper.updated,
                         self.paper.version, self.paper.pdf_url], ensure_ascii=False).encode()).hexdigest()[:24]
        self.lib.db.execute('UPDATE records SET fingerprint=?', (old,))
        self.lib.db.commit()
        self.lib.db.close()
        self.lib = c.Library(self.root / 'library')
        self.assertEqual(self.lib.upsert(self.paper), (self.rid, False, False))
        self.assertEqual(self.lib.db.execute('SELECT pdf_status FROM records').fetchone()[0], 'downloaded')
        self.assertEqual(len(list((self.lib.root / 'backups').glob('pre-migration-*.sqlite3'))), 1)

    def test_newer_schema_fails_instead_of_mutating_library(self):
        self.lib.db.execute('PRAGMA user_version=999')
        self.lib.db.commit()
        with self.assertRaisesRegex(ValueError, 'newer'):
            c.Library(self.lib.root)

    def test_author_and_identifier_changes_are_retained_as_revisions(self):
        rid = self.lib.upsert(replace(self.paper, authors=['New Author']))[0]
        self.assertNotEqual(rid, self.rid)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM documents').fetchone()[0], 1)

    def configure_cli(self):
        self.config_path.write_text('''[collector]
[filter]
minimum_score=1
[filter.topics]
Risk=["risk"]
[[sources]]
id="feed"
kind="rss"
url="https://example.org/feed"
allowed_domains=["example.org"]
''', encoding='utf-8')

    def cli(self, command, *flags):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(c, 'Client') as client:
            code = c.main([command, '--config', str(self.config_path), '--data-dir', str(self.lib.root), *flags])
            client.assert_not_called()
        return code, json.loads(output.getvalue())

    def test_scoped_retry_only_resets_selected_failed_record(self):
        self.configure_cli()
        second = self.lib.upsert(replace(self.paper, source_id='two', url='https://example.org/two'))[0]
        third = self.lib.upsert(replace(self.paper, source_id='three', url='https://example.org/three'))[0]
        self.lib.db.execute("UPDATE records SET pdf_status='download_failed',attempts=8,last_attempt=? WHERE id IN (?,?)", (c.stamp(), second, third))
        self.lib.db.commit()
        code, result = self.cli('retry', '--record-id', str(second))
        self.assertEqual((code, result['queued_records']), (0, [second]))
        states = {r['id']: (r['pdf_status'], r['attempts']) for r in self.lib.db.execute('SELECT * FROM records')}
        self.assertEqual(states[self.rid], ('downloaded', 1))
        self.assertEqual(states[second], ('pending', 0))
        self.assertEqual(states[third], ('download_failed', 8))
        with self.assertRaisesRegex(ValueError, 'requires'):
            self.cli('retry')
        self.assertTrue(list((self.lib.root / 'maintenance_history').glob('*-retry.json')))
        catalog = [json.loads(line) for line in (self.lib.root / 'catalog.jsonl').read_text().splitlines()]
        self.assertEqual(next(r['pdf_status'] for r in catalog if r['id'] == second), 'pending')

    def test_reclassification_is_offline_preserves_records_pdfs_and_dedup(self):
        self.configure_cli()
        self.config_path.write_text(self.config_path.read_text().replace('["risk"]', '["missing keyword"]'), encoding='utf-8')
        code, result = self.cli('reclassify')
        self.assertEqual((code, result['unmatched']), (0, 1))
        row = self.lib.db.execute('SELECT * FROM records').fetchone()
        self.assertFalse(json.loads(row['metadata'])['rule_match'])
        self.assertEqual(row['pdf_status'], 'downloaded')
        self.assertEqual(self.lib.upsert(self.paper), (self.rid, False, False))
        self.assertEqual(len(list(self.lib.root.rglob('*.pdf'))), 1)

    def test_verify_repair_cli_is_offline_and_exports_consistent_state(self):
        self.configure_cli()
        row = self.lib.db.execute('SELECT * FROM records').fetchone()
        (self.lib.root / row['pdf_path']).unlink()
        code, result = self.cli('verify', '--repair')
        self.assertEqual((code, result['repaired_records']), (2, 1))
        self.assertEqual(self.cli('verify')[0], 0)
        catalog = json.loads((self.lib.root / 'catalog.jsonl').read_text().splitlines()[0])
        self.assertEqual((catalog['pdf_status'], catalog['pdf_path']), ('pending', ''))

    def test_process_lock_rejects_a_second_writer(self):
        script = 'from pathlib import Path; import collector; collector.run_lock(Path(__import__("sys").argv[1])).__enter__()'
        with c.run_lock(self.lib.root):
            result = subprocess.run([sys.executable, '-c', script, str(self.lib.root)], cwd=c.BASE,
                                    capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Another collector process', result.stderr)

    def test_mutable_url_without_revision_markers_is_not_reused_after_changes(self):
        # The existing PDF's version differs; the incoming unversioned item must fetch.
        changed = replace(self.paper, version='', abstract='Changed content')
        rid = self.lib.upsert(changed)[0]
        self.lib.db.execute('UPDATE records SET metadata=? WHERE id=?',
                            (json.dumps(c.asdict(replace(self.paper, version=''))), self.rid))
        self.lib.db.commit()
        fake = FakeClient()
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        self.assertEqual(self.lib.download(fake, row, SOURCE, {}), 'downloaded')
        self.assertEqual(len(fake.calls), 1)

    def test_pdf_long_retry_after_is_persisted(self):
        rid = self.lib.upsert(replace(self.paper, version='v2'))[0]
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        error = urllib.error.HTTPError(self.paper.pdf_url, 429, 'Rate limit', {'Retry-After': '172800'}, None)
        with patch.object(FakeClient, 'get', side_effect=error):
            self.assertEqual(self.lib.download(FakeClient(), row, SOURCE, {}), 'download_failed')
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        self.assertGreater((c.parse_date(row['next_retry']) - c.now()).total_seconds(), 172790)


class ConfigTests(unittest.TestCase):
    def test_invalid_budget_and_source_settings_fail_before_network(self):
        for key, value in [('http_attempts', 0), ('max_run_minutes', -1), ('timeout_seconds', 'fast'), ('log_backups', 1.5)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.validate_config(dict(collector={key: value}, sources=[]))
        for source in [dict(SOURCE, kind='unknown'), dict(SOURCE, page_size=0), dict(SOURCE, allowed_domains=['evil.test'])]:
            with self.assertRaises(ValueError):
                m.validate_config(dict(collector={}, sources=[source]))


if __name__ == '__main__':
    unittest.main()
