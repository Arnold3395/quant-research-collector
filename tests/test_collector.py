import argparse
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
import io
import http.client
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import collector as c

TEST_ROOT = (c.BASE / 'tmp' / 'tests').resolve()
assert TEST_ROOT.is_relative_to(c.BASE)
TEST_ROOT.mkdir(parents=True, exist_ok=True)


def temporary_directory():
    result = tempfile.TemporaryDirectory(dir=TEST_ROOT)
    assert Path(result.name).resolve().is_relative_to(TEST_ROOT)
    return result


SOURCE = dict(id='feed', kind='rss', url='https://example.org/feed', allowed_domains=['example.org'])
ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/2601.12345v2</id><title>Option pricing</title>
<published>2026-01-01T00:00:00Z</published><updated>2026-10-14T00:00:00Z</updated>
<summary>A revised volatility paper.</summary><author><name>Alice Smith</name></author>
<link href="https://arxiv.org/abs/2601.12345v2" rel="alternate"/>
<link href="https://arxiv.org/pdf/2601.12345v2" type="application/pdf" title="pdf"/>
<arxiv:doi>10.1000/Test</arxiv:doi><category term="q-fin.PR"/></entry></feed>'''


class FakeClient:
    def __init__(self, payload=b'%PDF-1.7\nfixture\n%%EOF', fail=False):
        self.payload, self.fail, self.calls = payload, fail, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.fail:
            raise urllib.error.URLError('offline')
        return self.payload, {}, url

    def validate(self, url):
        return c.canonical_url(url)


class ParsingTests(unittest.TestCase):
    def test_arxiv_revision_keeps_original_publication_and_new_version(self):
        paper = c.parse_feed(ATOM, SOURCE)[0]
        self.assertEqual((paper.arxiv, paper.version, paper.doi), ('2601.12345', 'v2', '10.1000/test'))
        self.assertTrue(paper.pdf_url.endswith('v2'))
        self.assertEqual(paper.published[:10], '2026-01-01')
        self.assertEqual(paper.updated[:10], '2026-10-14')

    def test_rdf_feed_and_html_entities(self):
        body = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
        <item><title>Risk &amp; Return</title><link>https://example.org/paper</link>
        <description>&lt;b&gt;Portfolio&lt;/b&gt; results</description><dc:date>2026-10-01</dc:date>
        <dc:creator>Alice</dc:creator></item></rdf:RDF>'''
        paper = c.parse_feed(body, SOURCE)[0]
        self.assertEqual(paper.abstract, 'Portfolio results')
        self.assertEqual(paper.authors, ['Alice'])
        self.assertEqual(paper.published, '2026-10-01')

    def test_rss_enclosure(self):
        body = b'<rss><channel><item><title>Portfolio</title><link>https://example.org/p</link><enclosure type="application/pdf" url="https://example.org/p.pdf"/></item></channel></rss>'
        self.assertEqual(c.parse_feed(body, SOURCE)[0].pdf_url, 'https://example.org/p.pdf')

    def test_changed_listing_is_failure_not_empty_success(self):
        with self.assertRaisesRegex(RuntimeError, 'No entries'):
            c.fetch_source(FakeClient(b'<rss><channel/></rss>'), SOURCE, None)

    def test_listing_rejects_navigation_and_duplicate_links(self):
        source = dict(SOURCE, kind='html', link_pattern=r'/research/.+\.pdf$')
        body = '<a href="/research/">Nav</a><a href="/research/x.pdf"><b>期权报告</b></a><a href="/research/x.pdf">Again</a>'
        papers = c.parse_listing(body.encode(), source)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, '期权报告')

    def test_rules_do_not_match_short_terms_inside_other_words(self):
        matcher = c.Matcher(dict(topics={'CTA': ['CTA'], '期权': ['期权']}, minimum_score=2))
        self.assertFalse(matcher.match(c.Paper('s', '1', 'Octagon and octane', 'https://example.org/1'), SOURCE))
        paper = c.Paper('s', '2', 'CTA 与期权研究', 'https://example.org/2')
        self.assertTrue(matcher.match(paper, SOURCE))
        self.assertIn('期权', paper.topics)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.lib = c.Library(self.temp.name)
        self.paper = c.Paper('feed', 'one', 'Portfolio & volatility', 'https://example.org/one',
                             authors=['Alice'], doi='10.1000/one', pdf_url='https://example.org/one.pdf')

    def tearDown(self):
        self.lib.db.close()
        self.temp.cleanup()

    def test_repeated_ingestion_is_idempotent_and_revision_is_retained(self):
        first, new, version = self.lib.upsert(self.paper)
        self.assertTrue(new and version)
        self.assertEqual(self.lib.upsert(self.paper), (first, False, False))
        _, new, version = self.lib.upsert(replace(self.paper, updated='2026-10-15', abstract='Revised results'))
        self.assertFalse(new)
        self.assertTrue(version)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM documents').fetchone()[0], 1)
        self.assertEqual(self.lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 2)

    def test_doi_deduplicates_across_sources_but_keeps_each_record(self):
        self.lib.upsert(self.paper)
        _, new, record = self.lib.upsert(replace(self.paper, source='other', source_id='different', url='https://example.org/other'))
        self.assertFalse(new)
        self.assertTrue(record)

    def test_conflicting_dois_are_not_merged_by_title(self):
        self.lib.upsert(self.paper)
        _, new, _ = self.lib.upsert(replace(self.paper, source_id='two', url='https://example.org/two', doi='10.1000/two'))
        self.assertTrue(new)

    def test_html_login_page_is_not_saved_as_pdf_and_failure_is_persisted(self):
        rid, _, _ = self.lib.upsert(self.paper)
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        result = self.lib.download(FakeClient(b'<html>Sign in</html>'), row, SOURCE, {})
        self.assertEqual(result, 'download_failed')
        state = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        self.assertEqual(state['attempts'], 1)
        self.assertIn('did not return a PDF', state['error'])
        self.assertFalse(list(Path(self.temp.name).rglob('*.pdf')))

    def test_pdf_is_content_addressed_and_shared_between_records(self):
        one, _, _ = self.lib.upsert(self.paper)
        two, _, _ = self.lib.upsert(replace(self.paper, source='other', source_id='other'))
        for rid, expected in ((one, 'downloaded'), (two, 'reused_pdf')):
            row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
            client = FakeClient()
            self.assertEqual(self.lib.download(client, row, SOURCE, {}), expected)
            self.assertEqual(len(client.calls), 1 if rid == one else 0)
        self.assertEqual(len(list(Path(self.temp.name).rglob('*.pdf'))), 1)
        self.assertFalse(list(Path(self.temp.name).rglob('*.part')))

    def test_export_escapes_active_html(self):
        self.lib.upsert(replace(self.paper, title='<script>alert(1)</script>', abstract='<img onerror=alert(1)>'))
        c.export_library(self.lib)
        page = (Path(self.temp.name) / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('&lt;script&gt;', page)

    def test_offline_report_preserves_latest_run_status_and_archive(self):
        self.lib.upsert(self.paper)
        info = dict(run_id='existing-run', status='ok', sources=[])
        c.export_library(self.lib, info)
        archive = self.lib.root / 'reports' / 'existing-run.md'
        before_archive = archive.read_bytes()
        before_status = (self.lib.root / 'last_run.json').read_bytes()
        config = self.lib.root / 'test-config.toml'
        config.write_text('sources = []\n[collector]\ndata_dir = "data"\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(c.main(['report', '--config', str(config), '--data-dir', str(self.lib.root), '--days', '30']), 0)
        self.assertEqual(archive.read_bytes(), before_archive)
        self.assertEqual((self.lib.root / 'last_run.json').read_bytes(), before_status)
        self.assertIn('existing-run', (self.lib.root / 'index.html').read_text(encoding='utf-8'))

    def test_retry_backlog_survives_failed_download(self):
        rid, _, _ = self.lib.upsert(self.paper)
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        self.lib.download(FakeClient(fail=True), row, SOURCE, {})
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        self.assertEqual(self.lib.download(FakeClient(), row, SOURCE, {}), 'downloaded')
        self.assertEqual(self.lib.db.execute('SELECT attempts FROM records WHERE id=?', (rid,)).fetchone()[0], 2)

    def test_missing_local_pdf_is_queued_and_can_be_recovered(self):
        rid, _, _ = self.lib.upsert(self.paper)
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        self.lib.download(FakeClient(), row, SOURCE, {})
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        path = (self.lib.root / row['pdf_path']).resolve()
        self.assertTrue(path.is_relative_to(self.lib.root.resolve()))
        path.unlink()
        self.assertEqual(self.lib.reconcile_downloads(), 1)
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        self.assertEqual((row['pdf_status'], row['attempts'], row['last_attempt']), ('pending', 0, ''))
        self.assertEqual(self.lib.download(FakeClient(), row, SOURCE, {}), 'downloaded')
        self.assertTrue(path.is_file())

    def test_run_budget_deferral_does_not_consume_a_download_retry(self):
        rid, _, _ = self.lib.upsert(self.paper)
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        client = FakeClient()
        with patch.object(client, 'get', side_effect=c.RunBudgetExceeded('time budget')):
            self.assertEqual(self.lib.download(client, row, SOURCE, {}), 'deferred_budget')
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        self.assertEqual((row['pdf_status'], row['attempts'], row['last_attempt']), ('pending', 0, ''))

    def test_interrupted_download_is_immediately_retryable_after_restart(self):
        rid, _, _ = self.lib.upsert(self.paper)
        self.lib.db.execute("UPDATE records SET attempts=1,last_attempt=? WHERE id=?", (c.stamp(), rid))
        self.lib.db.commit()
        self.assertEqual(self.lib.reconcile_downloads(), 1)
        row = self.lib.db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        self.assertEqual((row['pdf_status'], row['attempts'], row['last_attempt']), ('pending', 0, ''))
        self.assertEqual(self.lib.download(FakeClient(), row, SOURCE, {}), 'downloaded')


class PolicyAndRunTests(unittest.TestCase):
    def test_source_selection_accepts_spaces_but_rejects_unknown_ids(self):
        config = dict(sources=[dict(SOURCE, id='one'), dict(SOURCE, id='two', enabled=False)])
        self.assertEqual([s['id'] for s in c.select_sources(config, None)], ['one'])
        self.assertEqual([s['id'] for s in c.select_sources(config, ' one, two ')], ['one', 'two'])
        for selection in ('typo', 'one,typo', ' , '):
            with self.assertRaises(ValueError):
                c.select_sources(config, selection)

    def test_doctor_rejects_unknown_sources_before_network_access(self):
        args = argparse.Namespace(sources='typo')
        config = dict(collector={}, sources=[SOURCE])
        with patch.object(c, 'Client') as client:
            with self.assertRaisesRegex(ValueError, 'Unknown source'):
                c.doctor(args, config)
            client.assert_not_called()

    def test_short_http_body_is_not_accepted_as_a_complete_download(self):
        client = c.Client(dict(http_attempts=1, request_interval_seconds=0), [SOURCE])
        response = io.BytesIO(b'%PDF-1.7\ntruncated')
        response.headers = {'Content-Length': '1000'}
        response.url = 'https://example.org/paper.pdf'
        with patch.object(client, 'check_robots'), patch.object(client.opener, 'open', return_value=response):
            with self.assertRaises(http.client.IncompleteRead):
                client.get(response.url)

    def test_slow_stream_checks_deadline_between_reads_without_retrying(self):
        clock = [0.1]
        class SlowResponse:
            headers = {}
            url = 'https://example.org/slow.pdf'
            reads = 0
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): raise AssertionError('Blocking read(n) should not be used')
            def read1(self, size):
                self.reads += 1
                clock[0] += 0.6
                return b'part'
        response = SlowResponse()
        client = c.Client(dict(http_attempts=2, request_interval_seconds=0), [SOURCE], deadline=1.0)
        with patch.object(client, 'check_robots'), patch.object(client.opener, 'open', return_value=response) as opener, patch.object(c.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(c.RunBudgetExceeded):
                client.get(response.url)
        self.assertEqual(response.reads, 2)
        self.assertEqual(opener.call_count, 1)

    def test_host_allowlist_blocks_redirect_targets(self):
        client = c.Client({}, [SOURCE])
        for url in ('http://127.0.0.1/p.pdf', 'https://example.org.evil.test/a', 'file:///C:/secret'):
            with self.assertRaises((c.PolicyError, ValueError)):
                client.validate(url)
        self.assertEqual(client.validate('https://example.org/p.pdf'), 'https://example.org/p.pdf')

    def test_pdf_resolver_ignores_footer_and_does_not_guess_ambiguous_pdfs(self):
        paper = c.Paper('s', '1', 'Portfolio', 'https://example.org/paper')
        client = FakeClient(b'<a href="/footer-form-crs.pdf">Terms</a><a href="/one.pdf">PDF</a><a href="/two.pdf">PDF</a>')
        self.assertEqual(c.resolve_pdf(client, paper, SOURCE), '')

    def test_failed_source_does_not_advance_its_watermark_while_other_source_succeeds(self):
        with temporary_directory() as directory:
            old = c.Library(directory)
            old.db.execute("INSERT INTO runs(id,started,status) VALUES('stale','2000-01-01T00:00:00+00:00','running')")
            old.db.commit()
            old.db.close()
            settings = dict(data_dir=directory, max_run_minutes=2, initial_days=14)
            sources = [dict(SOURCE, id='good'), dict(SOURCE, id='bad')]
            config = dict(collector=settings, sources=sources, filter=dict(topics={'Portfolio': ['portfolio']}, minimum_score=1))
            args = argparse.Namespace(data_dir=directory, sources=None, days=None, metadata_only=True, max_downloads=None)
            paper = c.Paper('good', 'one', 'Portfolio construction', 'https://example.org/one', updated=c.stamp())
            def fetch(client, source, since):
                if source['id'] == 'bad':
                    raise RuntimeError('Feed unavailable')
                return [paper], False
            with patch.object(c, 'Client'), patch.object(c, 'fetch_source', side_effect=fetch), redirect_stdout(io.StringIO()):
                self.assertEqual(c.collect(args, config, Path(directory)), 2)
            lib = c.Library(directory)
            try:
                self.assertIsNotNone(lib.success('good'))
                self.assertIsNone(lib.success('bad'))
                self.assertEqual(lib.db.execute("SELECT status FROM runs WHERE id='stale'").fetchone()[0], 'interrupted')
                row = lib.db.execute('SELECT pdf_status,attempts FROM records').fetchone()
                self.assertEqual(tuple(row), ('pending', 0))
                state = json.loads((Path(directory) / 'last_run.json').read_text(encoding='utf-8'))
                self.assertEqual(state['status'], 'partial')
            finally:
                lib.db.close()
                logging.shutdown()


if __name__ == '__main__':
    unittest.main()
