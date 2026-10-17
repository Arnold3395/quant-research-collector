import argparse
from contextlib import redirect_stdout
from datetime import timedelta
import io
import json
import logging
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.parse

import adapters as a
import collector as c
import maintenance as m
from test_collector import temporary_directory, SOURCE


SINCE = c.parse_date('2026-10-01T12:00:00Z')
END = '2026-10-16T10:00:00+00:00'


class Client:
    def __init__(self, *payloads):
        self.payloads, self.calls, self.metrics = list(payloads), [], {}
    def get(self, url, **kwargs):
        self.calls.append(url)
        if not self.payloads:
            raise AssertionError('Unexpected network call: ' + url)
        value = self.payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, bytes) else json.dumps(value).encode(), {}, url


def crossref_payload(dois=('10.1000/one',), cursor='NEXT'):
    return dict(status='ok', message={'items': [dict(DOI=doi, title=['Portfolio optimization'],
        published={'date-parts': [[2001]]}, deposited={'date-time': '2020-01-01T00:00:00Z'},
        indexed={'date-time': '2026-10-15T00:00:00Z'}, author=[dict(given='Alice', family='Smith')],
        license=[{'URL': 'https://creativecommons.org/licenses/by/4.0/'}], type='journal-article',
        link=[{'URL': 'https://paywall.test/p.pdf', 'content-type': 'application/pdf'}]) for doi in dois],
        'next-cursor': cursor})


def datacite_payload(next_url=None):
    return dict(data=[dict(id='10.1000/one', attributes=dict(doi='10.1000/one',
        titles=[{'title': 'Portfolio optimization'}], creators=[{'name': 'Alice Smith'}],
        descriptions=[{'descriptionType': 'Abstract', 'description': '<p>Risk model</p>'}],
        types={'resourceTypeGeneral': 'Preprint'}, rightsList=[{'rightsUri': 'https://creativecommons.org/licenses/by/4.0/'}],
        dates=[{'dateType': 'Issued', 'date': '2001'}], updated='2026-10-15T00:00:00Z',
        url='https://repo.test/paper', contentUrl=['https://repo.test/paper.pdf']))], links={'next': next_url})


def oai(records='', token='', error=''):
    if error:
        body = '<error code="' + error + '">No results</error>'
    else:
        body = '<ListRecords>' + records + ('<resumptionToken>' + token + '</resumptionToken>' if token else '') + '</ListRecords>'
    return ('<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">' + body + '</OAI-PMH>').encode()


def record(identifier='oai:test:1', deleted=False):
    header = '<header' + (' status="deleted"' if deleted else '') + '><identifier>' + identifier + '</identifier><datestamp>2026-10-15T00:00:00Z</datestamp></header>'
    meta = '' if deleted else '<metadata><dc:dc><dc:title>Portfolio optimization</dc:title><dc:identifier>https://repo.test/handle/1</dc:identifier><dc:identifier>doi:10.1000/one</dc:identifier><dc:creator>Alice Smith</dc:creator><dc:date>2001</dc:date><dc:description>Risk models</dc:description></dc:dc></metadata>'
    return '<record>' + header + meta + '</record>'


class AdapterTests(unittest.TestCase):
    def test_registry_and_config_validation_cover_supported_kinds(self):
        self.assertEqual(set(a.REGISTRY), {'rss', 'html', 'arxiv', 'crossref', 'datacite', 'oai_pmh'})
        for kind, extra in [('crossref', {}), ('datacite', {}), ('oai_pmh', {'metadata_prefix': 'unsupported'})]:
            with self.assertRaises(ValueError):
                m.validate_config(dict(collector={}, sources=[dict(SOURCE, kind=kind, **extra)]))

    def test_crossref_preserves_original_date_and_does_not_claim_link_is_free_pdf(self):
        source = dict(SOURCE, kind='crossref', issn='1469-7696', _until=END)
        batch = c.fetch_source(Client(crossref_payload()), source, SINCE)
        paper = batch.papers[0]
        self.assertEqual((paper.published, paper.updated, paper.doi), ('2001', '2020-01-01T00:00:00Z', '10.1000/one'))
        self.assertEqual(paper.pdf_url, '')
        self.assertEqual(paper.license_url, 'https://creativecommons.org/licenses/by/4.0/')
        self.assertFalse(batch.truncated)

    def test_crossref_cursor_changes_keep_identical_query_parameters_and_window(self):
        source = dict(SOURCE, kind='crossref', issn='1469-7696', page_size=1, max_pages=2, _until=END)
        client = Client(crossref_payload(cursor='next+/%='), crossref_payload(('10.1000/two',), cursor='next2'))
        result = c.fetch_source(client, source, SINCE)
        params = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) for url in client.calls]
        self.assertEqual(params[0]['filter'], params[1]['filter'])
        self.assertEqual(params[1]['cursor'], ['next+/%='])
        self.assertIn('until-index-date:2026-10-16T10:00:00', params[1]['filter'][0])
        self.assertNotIn('Z', params[1]['filter'][0])
        self.assertTrue(result.truncated)
        self.assertEqual(result.cursor, 'next2')

    def test_crossref_repeated_cursor_is_an_error_not_false_completion(self):
        source = dict(SOURCE, kind='crossref', query='portfolio', page_size=1, max_pages=3, _until=END)
        client = Client(crossref_payload(cursor='same'), crossref_payload(('10.1000/two',), cursor='same'))
        with self.assertRaisesRegex(ValueError, 'repeated'):
            c.fetch_source(client, source, SINCE)

    def test_datacite_parses_dates_authors_license_and_content_link(self):
        source = dict(SOURCE, kind='datacite', query='portfolio', _until=END)
        client = Client(datacite_payload())
        paper = c.fetch_source(client, source, SINCE).papers[0]
        self.assertEqual((paper.authors, paper.abstract, paper.resource_type), (['Alice Smith'], 'Risk model', 'Preprint'))
        self.assertEqual(paper.pdf_url, 'https://repo.test/paper.pdf')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[0]).query)
        self.assertIn('updated:[2026-10-01T12:00:00Z TO 2026-10-16T10:00:00Z]', query['query'][0])
        self.assertEqual(query['page[cursor]'], ['1'])

    def test_datacite_never_follows_foreign_pagination_links(self):
        source = dict(SOURCE, kind='datacite', query='portfolio', _until=END)
        with self.assertRaisesRegex(ValueError, 'leaves'):
            c.fetch_source(Client(datacite_payload('https://evil.test/dois?page%5Bcursor%5D=x')), source, SINCE)

    def test_datacite_extracts_cursor_but_rebuilds_original_query(self):
        source = dict(SOURCE, kind='datacite', query='portfolio', _until=END, max_pages=1)
        next_url = SOURCE['url'] + '?page%5Bcursor%5D=next%2Btoken&query=changed'
        result = c.fetch_source(Client(datacite_payload(next_url)), source, SINCE)
        self.assertTrue(result.truncated)
        self.assertEqual(result.cursor, 'next+token')

    def test_oai_next_request_uses_only_resumption_token_and_verb(self):
        source = dict(SOURCE, kind='oai_pmh', set_spec='open_access', max_pages=2, _until=END)
        client = Client(oai(record(), 'next+/='), oai(record('oai:test:2')))
        batch = c.fetch_source(client, source, SINCE)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[1]).query)
        self.assertEqual(params, {'verb': ['ListRecords'], 'resumptionToken': ['next+/=']})
        self.assertEqual(len(batch.papers), 2)
        self.assertEqual(batch.papers[0].published, '2001')
        self.assertEqual(batch.papers[0].doi, '10.1000/one')

    def test_oai_no_records_match_is_success_but_protocol_error_is_not(self):
        source = dict(SOURCE, kind='oai_pmh', _until=END)
        self.assertEqual(c.fetch_source(Client(oai(error='noRecordsMatch')), source, SINCE).papers, [])
        with self.assertRaisesRegex(ValueError, 'badResumptionToken'):
            c.fetch_source(Client(oai(error='badResumptionToken')), source, SINCE)
        with self.assertRaisesRegex(ValueError, 'Unexpected OAI'):
            c.fetch_source(Client(b'<html>challenge</html>'), source, SINCE)

    def test_oai_day_granularity_and_deletion_notice(self):
        source = dict(SOURCE, kind='oai_pmh', date_granularity='days', _until=END)
        client = Client(oai(record(deleted=True)))
        batch = c.fetch_source(client, source, SINCE)
        self.assertEqual(batch.deletions, [dict(source_id='oai:test:1', datestamp='2026-10-15T00:00:00Z')])
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[0]).query)
        self.assertEqual(params['from'], ['2026-10-01'])
        self.assertEqual(params['until'], ['2026-10-16'])

    def test_source_catalog_is_offline_and_includes_disabled_sources(self):
        with patch.object(c, 'Client') as network, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(c.main(['sources']), 0)
        network.assert_not_called()
        items = json.loads(output.getvalue())
        self.assertTrue(any(item['kind'] == 'oai_pmh' and item['resumable'] for item in items))
        self.assertTrue(any(not item['enabled'] for item in items))

    def test_fragments_merge_validate_and_expanded_json_loads_without_original_files(self):
        with temporary_directory() as directory:
            root = Path(directory)
            (root / 'sources.d').mkdir()
            config = root / 'config.toml'
            config.write_text('source_files=["sources.d/*.toml"]\n[collector]\n[filter.topics]\nRisk=["risk"]\n', encoding='utf-8')
            fragment = root / 'sources.d' / 'extra.toml'
            fragment.write_text('[[sources]]\nid="one"\nkind="rss"\nurl="https://example.org/feed"\nallowed_domains=["example.org"]\n', encoding='utf-8')
            combined, _ = c.load_config(config)
            self.assertEqual(combined['sources'][0]['id'], 'one')
            expanded = root / 'expanded.json'
            expanded.write_text(json.dumps(combined), encoding='utf-8')
            fragment.unlink()
            self.assertEqual(c.load_config(expanded)[0], combined)
            with self.assertRaisesRegex(ValueError, 'No source files match'):
                c.load_config(config)

    def test_fragments_reject_duplicate_ids_and_non_source_sections(self):
        with temporary_directory() as directory:
            root = Path(directory)
            config = root / 'config.toml'
            config.write_text('source_files=["*.extra.toml"]\n[collector]\n', encoding='utf-8')
            body = '[[sources]]\nid="same"\nkind="rss"\nurl="https://example.org/feed"\nallowed_domains=["example.org"]\n'
            (root / 'one.extra.toml').write_text(body, encoding='utf-8')
            (root / 'two.extra.toml').write_text(body, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Duplicate source ids'):
                c.load_config(config)
            (root / 'two.extra.toml').write_text('[collector]\ntimeout_seconds=1\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'only contain'):
                c.load_config(config)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.name)
        self.source = dict(SOURCE, kind='oai_pmh', max_pages=1)
        self.config = dict(collector=dict(initial_days=14, max_run_minutes=2), sources=[self.source],
                           filter=dict(topics={'Portfolio': ['portfolio']}, minimum_score=1))
        self.args = argparse.Namespace(data_dir=str(self.root), sources=None, days=None, metadata_only=True,
                                       max_downloads=None, force_sources=False)

    def tearDown(self):
        logging.shutdown()
        self.temp.cleanup()

    def run_batch(self, client):
        with patch.object(c, 'Client', return_value=client), redirect_stdout(io.StringIO()):
            return c.collect(self.args, self.config, self.root)

    def test_checkpoint_survives_restart_then_finalizes_original_window(self):
        self.assertEqual(self.run_batch(Client(oai(record(), 'page2'))), 2)
        lib = c.Library(self.root)
        checkpoint = dict(lib.db.execute('SELECT * FROM harvest_checkpoints').fetchone())
        self.assertIsNone(lib.success('feed'))
        self.assertEqual(lib.db.execute('SELECT failures FROM source_health').fetchone()[0], 0)
        lib.db.close()
        client = Client(oai(record('oai:test:2')))
        self.assertEqual(self.run_batch(client), 0)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[0]).query)['resumptionToken'], ['page2'])
        lib = c.Library(self.root)
        try:
            self.assertEqual(c.stamp(lib.success('feed')), checkpoint['window_until'])
            self.assertEqual(lib.db.execute('SELECT count(*) FROM harvest_checkpoints').fetchone()[0], 0)
            self.assertEqual(lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 2)
        finally:
            lib.db.close()

    def test_backup_restores_a_nonempty_checkpoint_and_can_continue(self):
        self.run_batch(Client(oai(record(), 'page2')))
        config_path = self.root / 'full-config.json'
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        lib = c.Library(self.root)
        archive = self.root / 'snapshot.zip'
        try:
            m.backup(lib, archive, config_path, c)
        finally:
            lib.db.close()
        restored = self.root / 'restored'
        m.restore(archive, restored, c)
        expanded, _ = c.load_config(restored / 'config.expanded.json')
        self.assertEqual(expanded, self.config)
        self.args.data_dir = str(restored)
        client = Client(oai(record('oai:test:2')))
        self.assertEqual(self.run_batch(client), 0)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[0]).query)['resumptionToken'], ['page2'])

    def test_changed_configuration_discards_old_cursor(self):
        self.run_batch(Client(oai(record(), 'page2')))
        self.config['filter']['topics']['Portfolio'].append('risk')
        client = Client(oai(record()))
        self.assertEqual(self.run_batch(client), 0)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[0]).query)
        self.assertNotIn('resumptionToken', params)
        self.assertIn('from', params)

    def test_failed_resumption_keeps_cursor_and_watermark(self):
        self.run_batch(Client(oai(record(), 'page2')))
        self.assertEqual(self.run_batch(Client(RuntimeError('outage'))), 2)
        lib = c.Library(self.root)
        try:
            self.assertEqual(lib.db.execute('SELECT cursor FROM harvest_checkpoints').fetchone()[0], 'page2')
            self.assertIsNone(lib.success('feed'))
        finally:
            lib.db.close()

    def test_explicitly_expired_oai_token_restarts_same_window_once(self):
        self.run_batch(Client(oai(record(), 'page2')))
        lib = c.Library(self.root)
        previous = dict(lib.db.execute('SELECT * FROM harvest_checkpoints').fetchone())
        lib.db.close()
        client = Client(oai(error='badResumptionToken'), oai(record()))
        self.assertEqual(self.run_batch(client), 0)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(client.calls[1]).query)
        self.assertNotIn('resumptionToken', params)
        self.assertEqual(params['until'], [a.utc_query(previous['window_until'], c)])
        info = json.loads((self.root / 'last_run.json').read_text())
        self.assertTrue(info['sources'][0]['checkpoint_restarted'])
        self.assertEqual(info['new_records'], 0)

    def test_server_index_window_does_not_drop_old_publication_or_deposit_dates(self):
        self.source.update(kind='crossref', issn='1469-7696')
        self.assertEqual(self.run_batch(Client(crossref_payload())), 0)
        lib = c.Library(self.root)
        try:
            self.assertEqual(lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 1)
        finally:
            lib.db.close()

    def test_metadata_only_channel_never_enters_pdf_queue(self):
        self.source.update(kind='crossref', issn='1469-7696', download=False)
        self.args.metadata_only = False
        client = Client(crossref_payload())
        self.assertEqual(self.run_batch(client), 0)
        self.assertEqual(len(client.calls), 1)
        info = json.loads((self.root / 'last_run.json').read_text())
        self.assertEqual((info['metadata_only_records'], info['pending_downloads']), (1, 0))
        lib = c.Library(self.root)
        try:
            row = lib.db.execute('SELECT pdf_status,attempts FROM records').fetchone()
            self.assertEqual(tuple(row), ('metadata_only', 0))
        finally:
            lib.db.close()

    def test_deletion_marks_existing_records_without_erasing_history(self):
        self.assertEqual(self.run_batch(Client(oai(record()))), 0)
        self.assertEqual(self.run_batch(Client(oai(record(deleted=True)))), 0)
        lib = c.Library(self.root)
        try:
            self.assertEqual(lib.db.execute('SELECT count(*) FROM records').fetchone()[0], 1)
            self.assertTrue(json.loads(lib.db.execute('SELECT metadata FROM records').fetchone()[0])['source_deleted'])
            self.assertEqual(lib.db.execute('SELECT count(*) FROM source_notices').fetchone()[0], 1)
        finally:
            lib.db.close()


if __name__ == '__main__':
    unittest.main()
