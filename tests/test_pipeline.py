import argparse
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import timedelta
import gzip
import io
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
import urllib.parse
import urllib.error

import adapters
import collector as c
import maintenance as m
import pipeline as p
from test_collector import temporary_directory, SOURCE, ATOM


class Client:
    def __init__(self, *payloads):
        self.payloads, self.calls = list(payloads), []
        self.metrics = {}

    def get(self, url, **kwargs):
        self.calls.append(url)
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return payload, {}, url

    def validate(self, url):
        return c.canonical_url(url)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.name)
        self.source = dict(SOURCE)
        self.config = dict(collector=dict(max_run_minutes=2), sources=[self.source],
                           filter=dict(topics={'组合': ['portfolio']}, minimum_score=2))
        self.config_path = self.root / 'config.json'
        self.lib = c.Library(self.root)
        self.paper = c.Paper('feed', 'one', 'Portfolio research', 'https://example.org/one',
                             abstract='Complete abstract', authors=['Alice 王'], published='2026-01-03',
                             updated=c.stamp(), pdf_url='https://example.org/one.pdf')

    def tearDown(self):
        self.lib.db.close()
        self.temp.cleanup()

    def cli(self, command, *args, client=None):
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False), encoding='utf-8')
        buffer = io.StringIO()
        with patch.object(c, 'Client', return_value=client or Client()), redirect_stdout(buffer):
            code = c.main([command, '--config', str(self.config_path), '--data-dir', str(self.root), *map(str, args)])
        return code, buffer.getvalue()

    def collect(self, papers=None, client=None, metadata_only=True, side_effect=None):
        args = argparse.Namespace(command='run', data_dir=str(self.root), sources=None, days=None,
                                  metadata_only=metadata_only, max_downloads=None, force_sources=False)
        with patch.object(c, 'Client', return_value=client or Client()), \
             patch.object(c, 'fetch_source', side_effect=side_effect, return_value=(papers or [], False)), \
             redirect_stdout(io.StringIO()):
            return c.collect(args, self.config, self.root)

    def test_rejected_candidate_can_be_promoted_offline_without_duplicate_versions(self):
        rejected = replace(self.paper, title='Option pricing')
        self.assertEqual(self.collect([rejected]), 0)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 0)
        row = self.lib.db.execute('SELECT * FROM candidates').fetchone()
        self.assertEqual(row['reason'], 'below_threshold')
        self.config['filter']['topics']['期权'] = ['option pricing']
        with patch.object(c, 'fetch_source', side_effect=AssertionError('must stay offline')):
            code, text = self.cli('rescreen')
        self.assertEqual((code, json.loads(text)['new_records']), (0, 1))
        self.assertEqual(json.loads(self.cli('rescreen')[1])['new_records'], 0)
        history = json.loads(self.lib.db.execute('SELECT decisions FROM candidates').fetchone()[0])
        self.assertEqual([h['accepted'] for h in history], [False, True])
        self.config['filter']['exclude_terms'] = ['option']
        self.cli('rescreen')
        self.assertFalse(json.loads(self.lib.db.execute('SELECT metadata FROM records').fetchone()[0])['rule_match'])
        self.assertEqual(self.lib.db.execute('SELECT reason FROM candidates').fetchone()[0], 'excluded_title')

    def test_candidate_retention_and_logical_quota_do_not_delete_archived_records(self):
        self.collect([self.paper])
        self.lib.db.execute("UPDATE candidates SET last_seen='2000-01-01T00:00:00+00:00'")
        p.prune(self.lib, {}, c)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 1)
        p.remember(self.lib, replace(self.paper, abstract='x' * 3000), self.source, self.config, c)
        p.prune(self.lib, dict(candidate_max_mb=0.001), c)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)

    def test_disabled_candidate_capture_and_optional_raw_capture(self):
        self.config['collector']['retain_candidates'] = False
        self.collect([self.paper])
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)
        p.capture(self.lib, 'feed', SOURCE['url'], b'hello', {}, c)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM raw_responses').fetchone()[0], 0)
        for _ in range(2):
            p.capture(self.lib, 'feed', SOURCE['url'], b'hello', dict(retain_raw_responses=True), c)
        rows = self.lib.db.execute('SELECT * FROM raw_responses').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(gzip.decompress(rows[0]['body']), b'hello')

    def test_conflicting_identity_is_quarantined_without_stopping_valid_following_item(self):
        self.lib.upsert(replace(self.paper, doi='10.1/a'))
        self.lib.upsert(replace(self.paper, source_id='two', url='https://example.org/two', doi='10.1/b'))
        conflict = replace(self.paper, doi='10.1/b')
        following = replace(self.paper, source_id='three', url='https://example.org/three')
        self.assertEqual(self.collect([conflict, following]), 2)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 3)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM item_errors').fetchone()[0], 1)
        info = json.loads((self.root / 'last_run.json').read_text(encoding='utf-8'))
        self.assertEqual(info['sources'][0]['status'], 'needs_review')
        self.assertTrue(info['sources'][0]['watermark_advanced'])
        self.assertEqual(self.lib.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_crossref_bad_item_is_preserved_and_valid_item_continues(self):
        self.source.update(kind='crossref', query='portfolio')
        body = json.dumps(dict(status='ok', message=dict(items=[{'DOI': '10.1/bad'},
                 {'DOI': '10.1/good', 'title': ['Portfolio']}]))).encode()
        code, output = self.cli('run', '--metadata-only', client=Client(body))
        self.assertEqual(code, 2)
        info = json.loads(output)
        self.assertEqual((info['new_records'], info['sources'][0]['quarantined']), (1, 1))
        item = self.lib.db.execute('SELECT * FROM item_errors').fetchone()
        self.assertEqual(json.loads(item['payload'])['DOI'], '10.1/bad')
        with patch.object(adapters, 'map_crossref', return_value=replace(self.paper, source_id='fixed')):
            code, text = self.cli('replay', '--item-id', item['id'])
        self.assertEqual((code, json.loads(text)['items'][0]['status']), (0, 'resolved'))
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 2)

    def test_bad_feed_entry_does_not_hide_following_valid_entry(self):
        body = b'<rss><channel><item><title>bad</title></item><item><title>Portfolio</title><link>https://example.org/p</link></item></channel></rss>'
        code, _ = self.cli('run', '--metadata-only', client=Client(body))
        self.assertEqual(code, 2)
        self.assertEqual(self.lib.db.execute('SELECT stage FROM item_errors').fetchone()[0], 'feed')
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 1)

    def test_invalid_field_type_is_isolated_before_it_can_break_catalog_export(self):
        invalid = replace(self.paper, source_id='bad', authors=None)
        self.assertEqual(self.collect([invalid, self.paper]), 2)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 1)
        self.assertIn('authors', self.lib.db.execute('SELECT error FROM item_errors').fetchone()[0])
        self.assertIn('Portfolio research', (self.root / 'index.html').read_text(encoding='utf-8'))

    def test_replay_failure_keeps_item_unresolved_and_increments_attempts(self):
        p.quarantine(self.lib, 'feed', 'crossref', {}, 'missing fields', 'r', None, c)
        code, output = self.cli('replay', '--sources', 'feed')
        self.assertEqual((code, json.loads(output)['items'][0]['status']), (2, 'failed'))
        row = self.lib.db.execute('SELECT resolved_at,attempts FROM item_errors').fetchone()
        self.assertEqual(tuple(row), ('', 1))

    def test_manual_resolution_requires_a_reason_and_preserves_an_explicit_task_gap(self):
        self.lib.db.execute('INSERT INTO backfill_tasks(id,job_id,source,window_since,window_until,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            (1, 'job', 'feed', '2026-01-01', '2026-01-02', 'needs_review', c.stamp(), c.stamp()))
        p.quarantine(self.lib, 'feed', 'crossref', {}, 'missing title', 'r', 1, c)
        with self.assertRaisesRegex(ValueError, 'nonempty --reason'):
            self.cli('resolve', '--item-id', 1)
        code, output = self.cli('resolve', '--item-id', 1, '--reason', 'Reviewed original, accepted missing title')
        self.assertEqual((code, json.loads(output)['recovered_records']), (0, 0))
        row = self.lib.db.execute('SELECT * FROM item_errors').fetchone()
        self.assertTrue(row['resolved_at'])
        self.assertEqual(json.loads(row['payload']), {})
        task = self.lib.db.execute('SELECT status,coverage FROM backfill_tasks').fetchone()
        self.assertEqual(tuple(task), ('limited', 'accepted_item_gaps'))
        self.assertTrue(list((self.root / 'maintenance_history').glob('*resolve.json')))

    def test_download_continues_even_when_metadata_endpoint_fails(self):
        rid = self.lib.upsert(self.paper)[0]
        client = Client(b'%PDF-1.7\nfixture\n%%EOF')
        self.assertEqual(self.collect(client=client, metadata_only=False, side_effect=RuntimeError('metadata outage')), 2)
        self.assertEqual(self.lib.db.execute('SELECT pdf_status FROM records WHERE id=?', (rid,)).fetchone()[0], 'downloaded')
        self.assertEqual(client.calls, [self.paper.pdf_url])

    def test_download_command_never_fetches_metadata(self):
        self.lib.upsert(self.paper)
        with patch.object(c, 'fetch_source', side_effect=AssertionError('metadata requested')):
            code, output = self.cli('download', client=Client(b'%PDF-1.7\nfixture'))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['sources'], [])
        self.assertIsNone(self.lib.success('feed'))

    def test_retry_after_is_persisted_and_blocks_a_new_client_before_another_request(self):
        client = c.Client(dict(http_attempts=1, request_interval_seconds=0), [self.source])
        client.retry_observer = lambda host, until: p.state_value(self.lib, 'host_retry:' + host, until)
        error = urllib.error.HTTPError(SOURCE['url'], 429, 'Wait', {'Retry-After': '172800'}, None)
        with patch.object(client.opener, 'open', side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                client.get(SOURCE['url'], robots=False)
        later_client = c.Client(dict(http_attempts=1, request_interval_seconds=0), [self.source])
        later_client.host_waits = p.host_waits(self.lib, c)
        self.assertIn('example.org', later_client.host_waits)
        with patch.object(later_client.opener, 'open') as request:
            with self.assertRaises(urllib.error.HTTPError):
                later_client.get('https://example.org/another.pdf', robots=False)
            request.assert_not_called()

    def test_host_wait_skips_only_that_host_without_consuming_queued_record_attempts(self):
        first_id = self.lib.upsert(self.paper)[0]
        other = dict(SOURCE, id='other', url='https://other.org/feed', allowed_domains=['other.org'])
        self.config['sources'].append(other)
        self.lib.upsert(replace(self.paper, source='other', source_id='other', url='https://other.org/p', pdf_url='https://other.org/p.pdf'))
        p.state_value(self.lib, 'host_retry:example.org', c.stamp(c.now()+timedelta(days=2)))
        client = Client(b'%PDF-1.7 fixture')
        p.downloads(self.lib, client, self.config['sources'], {}, 10, dict(downloads={}), c)
        self.assertEqual(client.calls, ['https://other.org/p.pdf'])
        self.assertEqual(self.lib.db.execute('SELECT attempts FROM records WHERE id=?', (first_id,)).fetchone()[0], 0)

    def test_round_robin_and_per_source_download_cap_survive_next_invocation(self):
        self.config['sources'].append(dict(SOURCE, id='second'))
        for sid in ('feed', 'second'):
            for index in range(3):
                self.lib.upsert(replace(self.paper, source=sid, source_id=sid+str(index), url=f'https://example.org/{sid}{index}', pdf_url=f'https://example.org/{sid}{index}.pdf'))
        client, info = Client(*([b'%PDF-fixture'] * 3)), dict(downloads={})
        p.downloads(self.lib, client, self.config['sources'], {}, 3, info, c)
        self.assertEqual([url.rsplit('/', 1)[-1] for url in client.calls], ['feed0.pdf', 'second0.pdf', 'feed1.pdf'])
        client = Client(*([b'%PDF-fixture'] * 5))
        p.downloads(self.lib, client, self.config['sources'], dict(download_max_per_source=1), 5, dict(downloads={}), c)
        self.assertEqual([url.rsplit('/', 1)[-1] for url in client.calls], ['second1.pdf', 'feed2.pdf'])

    def test_discovery_rotates_after_previous_source_and_reserves_download_budget(self):
        self.config['sources'].append(dict(SOURCE, id='second'))
        self.config['collector']['source_max_seconds'] = 20
        p.state_value(self.lib, 'last_discovery_source', 'feed')
        seen = []
        def fetch(client, source, since):
            seen.append((source['id'], client.deadline))
            return [], False
        with patch.object(c.time, 'monotonic', return_value=100):
            self.assertEqual(self.collect(metadata_only=False, side_effect=fetch), 0)
        self.assertEqual(seen, [('second', 120), ('feed', 120)])

    def test_backfill_fixed_ranges_resume_without_advancing_normal_watermark(self):
        self.source.update(kind='crossref', query='portfolio')
        calls = []
        def fetch(client, source, since):
            calls.append((c.stamp(since), source['_until']))
            return adapters.Harvest([], since=c.stamp(since), until=source['_until'])
        with patch.object(c, 'fetch_source', side_effect=fetch):
            code, output = self.cli('backfill', '--from-date', '2026-01-01', '--until-date', '2026-01-15', '--chunk-days', 7, '--max-tasks', 1)
            job = json.loads(output)['job_id']
            self.assertEqual(code, 2)
            code, output = self.cli('backfill', '--job-id', job)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)['all_api_windows_done'])
        self.assertEqual(calls, [('2026-01-01T00:00:00+00:00', '2026-01-08T00:00:00+00:00'),
                                 ('2026-01-08T00:00:00+00:00', '2026-01-15T00:00:00+00:00')])
        self.assertIsNone(self.lib.success('feed'))

    def test_backfill_rss_never_claims_complete_historical_coverage(self):
        with patch.object(c, 'fetch_source', return_value=([], False)):
            code, output = self.cli('backfill', '--from-date', '2026-01-01', '--until-date', '2026-01-02')
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(result['scan_finished'])
        self.assertFalse(result['all_api_windows_done'])
        self.assertEqual(result['tasks'][0]['status'], 'limited')

    def test_interrupted_backfill_task_can_resume(self):
        self.source.update(kind='crossref', query='portfolio')
        with patch.object(c, 'fetch_source', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cli('backfill', '--from-date', '2026-01-01', '--until-date', '2026-01-02')
        task = self.lib.db.execute('SELECT * FROM backfill_tasks').fetchone()
        self.assertEqual(task['status'], 'running')
        with patch.object(c, 'fetch_source', side_effect=lambda client, source, since: adapters.Harvest([], since=c.stamp(since), until=source['_until'])):
            code, _ = self.cli('backfill', '--job-id', task['job_id'])
        self.assertEqual(code, 0)

    def test_arxiv_checkpoint_resumes_same_window_at_next_offset(self):
        self.source.update(kind='arxiv', query='cat:q-fin.PR', page_size=1, max_pages=1)
        payload = ATOM.replace(b'<entry>', b'<totalResults xmlns="http://a9.com/-/spec/opensearch/1.1/">2</totalResults><entry>', 1)
        code, _ = self.cli('run', '--metadata-only', client=Client(payload))
        self.assertEqual(code, 2)
        checkpoint = self.lib.db.execute('SELECT * FROM harvest_checkpoints').fetchone()
        self.assertEqual(checkpoint['cursor'], '1')
        client = Client(payload.replace(b'2601.12345', b'2601.12346'))
        code, _ = self.cli('run', '--metadata-only', client=client)
        self.assertEqual(code, 0)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[0]).query)
        self.assertEqual(query['start'], ['1'])
        self.assertIn(c.parse_date(checkpoint['window_until']).strftime('%Y%m%d%H%M'), query['search_query'][0])
        self.assertEqual(c.stamp(self.lib.success('feed')), checkpoint['window_until'])

    def test_partial_page_progress_survives_network_budget_interruption(self):
        self.source.update(kind='crossref', query='portfolio', page_size=1, max_pages=2)
        first = json.dumps(dict(status='ok', message={'items': [{'DOI': '10.1/good', 'title': ['Portfolio']}], 'next-cursor': 'second'})).encode()
        code, output = self.cli('run', '--metadata-only', client=Client(first, c.RunBudgetExceeded('phase exhausted')))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)['sources'][0]['status'], 'deferred_budget')
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 1)
        checkpoint = self.lib.db.execute('SELECT * FROM harvest_checkpoints').fetchone()
        self.assertEqual(checkpoint['cursor'], 'second')
        self.assertIsNone(self.lib.success('feed'))
        client = Client(json.dumps(dict(status='ok', message={'items': []})).encode())
        code, _ = self.cli('run', '--metadata-only', client=client)
        self.assertEqual(code, 0)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[0]).query)
        self.assertEqual(query['cursor'], ['second'])
        self.assertEqual(c.stamp(self.lib.success('feed')), checkpoint['window_until'])

    def test_first_page_outage_pins_initial_window_instead_of_sliding_it_forward(self):
        self.source.update(kind='crossref', query='portfolio')
        self.assertEqual(self.cli('run', '--metadata-only', client=Client(RuntimeError('offline')))[0], 2)
        checkpoint = self.lib.db.execute('SELECT * FROM harvest_checkpoints').fetchone()
        self.assertEqual(checkpoint['cursor'], '')
        later = c.now() + timedelta(days=2)
        client = Client(json.dumps(dict(status='ok', message={'items': []})).encode())
        with patch.object(c, 'now', return_value=later):
            self.assertEqual(self.cli('run', '--metadata-only', '--force-sources', client=client)[0], 0)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[0]).query)
        self.assertIn(c.parse_date(checkpoint['window_since']).strftime('%Y-%m-%dT%H:%M:%S'), query['filter'][0])
        self.assertEqual(c.stamp(self.lib.success('feed')), checkpoint['window_until'])

    def test_all_bad_arxiv_page_does_not_prevent_reading_following_page(self):
        self.source.update(kind='arxiv', query='cat:q-fin.PR', page_size=1, max_pages=2)
        total = b'<totalResults xmlns="http://a9.com/-/spec/opensearch/1.1/">2</totalResults>'
        bad = b'<feed xmlns="http://www.w3.org/2005/Atom">' + total + b'<entry><title>Missing URL</title></entry></feed>'
        good = ATOM.replace(b'<entry>', total + b'<entry>', 1)
        client = Client(bad, good)
        code, output = self.cli('run', '--metadata-only', client=client)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)['sources'][0]['status'], 'needs_review')
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 1)

    def test_rescreen_does_not_unpause_deleted_source_records(self):
        self.collect([self.paper])
        p.apply_notice(self.lib, 'feed', dict(source_id='one', datestamp=c.stamp()), c)
        self.lib.db.commit()
        self.cli('rescreen')
        metadata = json.loads(self.lib.db.execute('SELECT metadata FROM records').fetchone()[0])
        self.assertTrue(metadata['source_deleted'])
        self.assertEqual(p.metrics(self.lib, self.config, [self.source], c)['ready_downloads'], 0)

    def test_dates_outside_feed_window_are_retained_for_explicit_rescreen(self):
        self.assertEqual(self.collect([replace(self.paper, updated='2000-01-01')]), 0)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 0)
        self.assertEqual(self.lib.db.execute('SELECT reason FROM candidates').fetchone()[0], 'outside_window')
        self.assertEqual(json.loads(self.cli('rescreen')[1])['new_records'], 1)

    def test_citation_year_accepts_feed_dates_and_never_writes_weekday_as_year(self):
        self.assertEqual(p.citation_year('Fri, 16 Oct 2026 10:00:00 GMT'), '2026')
        self.assertEqual(p.citation_year('2020'), '2020')
        self.assertEqual(p.citation_year('unknown'), '')

    def test_shared_quarantine_tracks_every_affected_backfill_task(self):
        for task in (1, 2):
            self.lib.db.execute('INSERT INTO backfill_tasks(id,job_id,source,window_since,window_until,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                (task, 'job', 'feed', str(task), str(task+1), 'needs_review', c.stamp(), c.stamp()))
            p.quarantine(self.lib, 'feed', 'paper', asdict(self.paper), 'temporary mapping bug', 'r', task, c)
        self.assertTrue(p.unresolved(self.lib, 'feed', 1))
        self.assertTrue(p.unresolved(self.lib, 'feed', 2))
        self.cli('replay', '--sources', 'feed')
        self.assertEqual([r[0] for r in self.lib.db.execute('SELECT status FROM backfill_tasks')], ['done', 'done'])

    def test_metrics_need_repeated_degradation_not_a_single_zero_result(self):
        digest = m.source_hash(self.config, self.source)
        for index, count in enumerate((10, 10, 10, 0)):
            state = dict(source='feed', status='ok', fetched=count, accepted=count)
            p.record_metrics(self.lib, state, [self.paper] * count, str(index), 'normal', digest, time.monotonic(), c)
        self.assertEqual(p.metrics(self.lib, self.config, [self.source], c)['alerts'], [])
        p.record_metrics(self.lib, dict(source='feed', status='ok', fetched=0, accepted=0), [], 'last', 'normal', digest, time.monotonic(), c)
        self.assertEqual(p.metrics(self.lib, self.config, [self.source], c)['alerts'][0]['kind'], 'volume_drop')
        self.config['filter']['minimum_score'] = 3
        self.assertEqual(p.metrics(self.lib, self.config, [self.source], c)['alerts'], [])

    def test_metrics_detect_missing_field_regression_but_not_always_sparse_feed(self):
        digest = m.source_hash(self.config, self.source)
        sparse = replace(self.paper, abstract='', authors=[])
        for index in range(5):
            papers = [self.paper if index < 3 else sparse] * 5
            p.record_metrics(self.lib, dict(source='feed', status='ok', fetched=5, accepted=5), papers, str(index), 'normal', digest, time.monotonic(), c)
        alerts = p.metrics(self.lib, self.config, [self.source], c)['alerts']
        self.assertEqual({a['kind'] for a in alerts}, {'missing_abstract', 'missing_authors'})

    def test_aging_feed_entries_do_not_cause_false_acceptance_regression(self):
        digest = m.source_hash(self.config, self.source)
        for index in range(5):
            count = 10 if index < 3 else 0
            state = dict(source='feed', status='ok', fetched=10, evaluated=count, accepted=count)
            p.record_metrics(self.lib, state, [self.paper] * 10, str(index), 'normal', digest, time.monotonic(), c)
        self.assertEqual(p.metrics(self.lib, self.config, [self.source], c)['alerts'], [])

    def test_full_library_search_filters_dates_authors_doi_and_versions(self):
        self.config['sources'].append(dict(SOURCE, id='second', enabled=False))
        self.lib.upsert(replace(self.paper, doi='10.1/shared', topics=['组合']))
        self.lib.upsert(replace(self.paper, source='second', source_id='two', url='https://example.org/two', doi='10.1/shared', topics=['组合']))
        code, text = self.cli('search', '--query', 'PORTFOLIO', '--author', '王', '--topic', '组合', '--doi', 'https://doi.org/10.1/shared', '--from-date', '2026-01-01', '--until-date', '2026-02-01')
        self.assertEqual((code, json.loads(text)['total']), (0, 1))
        self.assertEqual(json.loads(text)['records'][0]['source'], 'second')
        self.assertEqual(json.loads(self.cli('search', '--all-versions')[1])['total'], 2)
        self.assertEqual(json.loads(self.cli('search', '--until-date', '2026-01-03')[1])['total'], 0)
        self.assertEqual(json.loads(self.cli('search', '--sources', 'feed')[1])['records'][0]['source'], 'feed')

    def test_export_formats_escape_special_characters_and_preserve_unicode(self):
        self.lib.upsert(replace(self.paper, title='=Portfolio & {期权}', abstract='Text\nER  -\nTY  - INJECT'))
        for kind, suffix in (('csv', 'csv'), ('bibtex', 'bib'), ('ris', 'ris'), ('json', 'json')):
            target = self.root / ('export.' + suffix)
            code, output = self.cli('export', '--format', kind, '--output', target)
            self.assertEqual((code, json.loads(output)['exported']), (0, 1))
            text = target.read_text(encoding='utf-8')
            self.assertIn('期权', text)
            if kind == 'csv':
                self.assertIn("'=Portfolio", text)
            elif kind == 'bibtex':
                self.assertIn(r'\& \{期权\}', text)
            elif kind == 'ris':
                self.assertEqual(text.count('\nER  -\n'), 1)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                self.cli('export', '--format', kind, '--output', target)

    def test_schema3_migration_and_backup_keep_pipeline_tables_and_record_ids(self):
        rid = self.lib.upsert(self.paper)[0]
        self.lib.db.execute('PRAGMA user_version=3')
        self.lib.db.commit()
        self.lib.db.close()
        self.lib = c.Library(self.root)
        self.assertEqual(self.lib.upsert(self.paper), (rid, False, False))
        self.assertTrue(list((self.root / 'backups').glob('pre-migration-*.sqlite3')))
        p.remember(self.lib, self.paper, self.source, self.config, c)
        self.lib.db.execute('INSERT INTO backfill_tasks(id,job_id,source,window_since,window_until,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            (1, 'job', 'feed', '2026-01-01', '2026-01-02', 'needs_review', c.stamp(), c.stamp()))
        p.quarantine(self.lib, 'feed', 'paper', asdict(self.paper), 'example', 'run', 1, c)
        p.capture(self.lib, 'feed', SOURCE['url'], b'raw fixture', dict(retain_raw_responses=True), c)
        p.record_metrics(self.lib, dict(source='feed', status='ok', fetched=1, accepted=1), [self.paper], 'r', 'normal', 'hash', time.monotonic(), c)
        p.state_value(self.lib, 'last_download_source', 'feed')
        self.lib.db.execute('INSERT INTO checkpoint_tracking VALUES(?,?,?,?)', ('feed', 'normal', c.stamp(), c.stamp()))
        self.lib.db.commit()
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        archive = self.root / 'roundtrip.zip'
        m.backup(self.lib, archive, self.config_path, c)
        target = self.root / 'restored'
        m.restore(archive, target, c)
        other = c.Library(target)
        try:
            for table in ('records', 'candidates', 'item_errors', 'error_tasks', 'raw_responses', 'source_metrics',
                          'backfill_tasks', 'pipeline_state', 'checkpoint_tracking'):
                self.assertEqual([tuple(r) for r in self.lib.db.execute('SELECT * FROM '+table)], [tuple(r) for r in other.db.execute('SELECT * FROM '+table)])
        finally:
            other.db.close()

    def test_invalid_new_config_and_cli_ranges_fail_before_network(self):
        for setting, value in (('retain_candidates', 'yes'), ('discovery_budget_fraction', 2), ('download_max_per_source', -1)):
            config = dict(self.config, collector={setting: value})
            with self.assertRaises(ValueError):
                m.validate_config(config)
        for args in (('--from-date', 'nonsense'), ('--from-date', '2026-02-01', '--until-date', '2026-01-01')):
            with self.assertRaises(SystemExit):
                self.cli('search', *args)


if __name__ == '__main__':
    unittest.main()
