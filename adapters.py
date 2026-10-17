"""Source adapters. All I/O goes through the collector's policy-aware Client."""
from dataclasses import dataclass, field
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET


@dataclass
class Harvest:
    papers: list
    truncated: bool = False
    cursor: str = ''
    since: str = ''
    until: str = ''
    deletions: list = field(default_factory=list)
    pages: int = 0
    server_filtered: bool = True
    error: object = None

    def __iter__(self):
        # Compatible with the existing fetch_source -> (papers, truncated) contract.
        return iter((self.papers, self.truncated))


@dataclass(frozen=True)
class Adapter:
    fetch: object
    options: frozenset
    coverage: str
    incremental_axis: str
    resumable: bool = False


REGISTRY = {}


class CheckpointExpired(ValueError):
    """The server explicitly rejected a saved resumption token."""


def register(kind, *, options=(), coverage='', incremental_axis='', resumable=False):
    def decorator(function):
        if kind in REGISTRY:
            raise ValueError('Duplicate adapter kind: ' + kind)
        REGISTRY[kind] = Adapter(function, frozenset(options), coverage, incremental_axis, resumable)
        return function
    return decorator


def endpoint(source, params):
    # Static endpoint query parameters are intentionally unsupported: cursor URLs
    # must not accidentally accumulate duplicate filters or escape this endpoint.
    return source['url'] + '?' + urllib.parse.urlencode(params)


def window(source, since, api):
    checkpoint = source.get('_checkpoint') or {}
    start = checkpoint.get('window_since') or (api.stamp(since) if since else '')
    return start, checkpoint.get('window_until') or source.get('_until') or api.stamp(), checkpoint.get('cursor', '')


def utc_query(value, api):
    return api.parse_date(value).strftime('%Y-%m-%dT%H:%M:%SZ')


def limits(source, override):
    return source.get('page_size', 100), override or source.get('max_pages', 5)


def first(value):
    return value[0] if isinstance(value, list) and value else value or ''


def publication_date(value):
    parts = (value or {}).get('date-parts', [[]])[0]
    return '-'.join(str(p) if index == 0 else f'{p:02d}' for index, p in enumerate(parts))


def canonical_or_empty(value, api):
    try:
        return api.canonical_url(value)
    except (ValueError, AttributeError):
        return ''


def map_crossref(item, source, api):
    doi = api.doi_id(item.get('DOI', ''))
    title = api.text_only(first(item.get('title', [])))
    if not doi or not title:
        raise ValueError('Crossref item missing DOI/title')
    authors = [a.get('name') or ' '.join(filter(None, (a.get('given'), a.get('family')))) for a in item.get('author', [])]
    license_url = first(item.get('license', []))
    return api.Paper(source['id'], doi, title, 'https://doi.org/' + doi,
        abstract=api.text_only(item.get('abstract', '')), authors=[a for a in authors if a], doi=doi,
        published=publication_date(item.get('published') or item.get('issued')),
        updated=(item.get('deposited') or {}).get('date-time', ''),
        categories=[str(x) for x in item.get('subject', [])],
        license_url=license_url.get('URL', '') if isinstance(license_url, dict) else '',
        resource_type=item.get('type', ''))


def map_datacite(item, source, api):
    a = item['attributes']
    doi = api.doi_id(a.get('doi') or item['id'])
    title = api.text_only(first(a.get('titles', [] )).get('title', '')) if a.get('titles') else ''
    if not doi or not title:
        raise ValueError('DataCite item missing DOI/title')
    published = next((d['date'] for d in a.get('dates', []) if d.get('dateType') == 'Issued'), '')
    descriptions = [d.get('description', '') for d in a.get('descriptions', []) if d.get('descriptionType') == 'Abstract']
    pdfs = a.get('contentUrl') or []
    if isinstance(pdfs, str):
        pdfs = [pdfs]
    pdfs = sorted({canonical_or_empty(p, api) for p in pdfs if isinstance(p, str) and re.search(r'\.pdf(?:[?#]|$)', p, re.I)} - {''})
    return api.Paper(source['id'], doi, title,
        canonical_or_empty(a.get('url', ''), api) or 'https://doi.org/' + doi,
        abstract=api.text_only(' '.join(descriptions)), authors=[x['name'] for x in a.get('creators', []) if x.get('name')],
        published=published or str(a.get('publicationYear') or ''), updated=a.get('updated', ''), doi=doi,
        pdf_url=pdfs[0] if len(pdfs) == 1 else '', version=str(a.get('version') or ''),
        categories=[x['subject'] for x in a.get('subjects', []) if x.get('subject')],
        license_url=next((x['rightsUri'] for x in a.get('rightsList', []) if x.get('rightsUri')), ''),
        resource_type=(a.get('types') or {}).get('resourceTypeGeneral', ''))


def map_oai_pmh(payload, source, api):
    entry = ET.fromstring(payload)
    header = entry.find('o:header', OAI)
    if header is None:
        raise ValueError('OAI-PMH header missing')
    identifier = header.findtext('o:identifier', '', OAI)
    updated = header.findtext('o:datestamp', '', OAI)
    if not identifier or not api.parse_date(updated):
        raise ValueError('OAI-PMH identifier/datestamp missing')
    if header.get('status') == 'deleted':
        return dict(source_id=identifier, datestamp=updated)
    metadata = entry.find('o:metadata', OAI)
    if metadata is None:
        raise ValueError('OAI-PMH metadata missing')
    def values(key):
        return [api.text_only(''.join(n.itertext())) for n in metadata.findall('.//dc:' + key, OAI)]
    title = first(values('title'))
    identifiers = values('identifier')
    urls = [canonical_or_empty(u, api) for u in identifiers if u.startswith(('https://', 'http://'))]
    urls = [u for u in urls if u]
    if not title or not urls:
        raise ValueError('OAI DC item missing title/HTTP identifier')
    landing = next((u for u in urls if '/handle/' in u or 'hdl.handle.net/' in u), urls[0])
    pdfs = sorted({u for u in urls if re.search(r'\.pdf(?:[?#]|$)', u, re.I)})
    doi = next((api.doi_id(u) for u in identifiers if re.match(r'(?:https?://(?:dx\.)?doi\.org/|doi:|10\.\d{4,9}/)', u, re.I)), '')
    dates = values('date')
    return api.Paper(source['id'], identifier, title, landing,
        abstract=' '.join(values('description')), authors=values('creator'),
        published=dates[0] if dates else '', updated=updated, doi=doi,
        pdf_url=pdfs[0] if len(pdfs) == 1 else '', categories=values('subject'),
        license_url=next((u for u in values('rights') if u.startswith(('http://', 'https://'))), ''),
        resource_type=first(values('type')))


def map_guard(kind, payload, source, api):
    try:
        return globals()['map_' + kind](payload, source, api)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        handler = source.get('_item_error_handler')
        if handler is None:
            raise
        handler(kind, payload, error)
        return None


def replay_item(kind, payload, source, api):
    if kind == 'feed':
        result = api.parse_feed(payload.encode(), source)
        if not result:
            raise ValueError('Feed item still lacks required fields')
        return result
    mapped = globals()['map_' + kind](payload, source, api)
    return [mapped]


@register('crossref', options=('query', 'issn', 'work_type'), coverage='DOI metadata; not a full-text entitlement',
          incremental_axis='Crossref index time (includes redeposits/reindexing)', resumable=True)
def crossref(client, source, since, max_pages, api):
    start, end, cursor = window(source, since, api)
    size, count = limits(source, max_pages)
    # Crossref accepts UTC timestamps without a trailing Z (unlike OAI/Solr).
    filters = ['until-index-date:' + utc_query(end, api).removesuffix('Z')]
    if start:
        filters.append('from-index-date:' + utc_query(start, api).removesuffix('Z'))
    if source.get('issn'):
        filters.append('issn:' + source['issn'])
    if source.get('work_type'):
        filters.append('type:' + source['work_type'])
    papers, seen, tokens = [], set(), set()
    cursor = cursor or '*'
    for page in range(count):
        params = dict(filter=','.join(filters), rows=size, cursor=cursor, sort='indexed', order='asc')
        if source.get('query'):
            params['query.bibliographic'] = source['query']
        try:
            body, _, _ = client.get(endpoint(source, params))
        except Exception as error:
            if page:
                return Harvest(papers, True, cursor, start, end, pages=page, error=error)
            raise
        payload = json.loads(body)
        message = payload.get('message', {})
        if payload.get('status') != 'ok' or not isinstance(message.get('items'), list):
            raise ValueError('Unexpected Crossref response schema')
        items = message['items']
        for item in items:
            paper = map_guard('crossref', item, source, api)
            if paper is not None and paper.source_id not in seen:
                papers.append(paper)
                seen.add(paper.source_id)
        next_cursor = message.get('next-cursor', '')
        if len(items) < size or not items:
            return Harvest(papers, since=start, until=end, pages=page + 1)
        if not next_cursor or next_cursor == cursor or next_cursor in tokens:
            raise ValueError('Crossref cursor missing or repeated before exhaustion')
        tokens.add(cursor)
        cursor = next_cursor
    return Harvest(papers, True, cursor, start, end, pages=count)


@register('datacite', options=('query', 'resource_types'), coverage='Repository DOI metadata; linked files are not guaranteed accessible',
          incremental_axis='DataCite metadata updated time', resumable=True)
def datacite(client, source, since, max_pages, api):
    start, end, cursor = window(source, since, api)
    size, count = limits(source, max_pages)
    query = '(' + source['query'] + ') AND updated:[' + (utc_query(start, api) if start else '*') + ' TO ' + utc_query(end, api) + ']'
    papers, seen, tokens = [], set(), set()
    cursor = cursor or '1'
    for page in range(count):
        params = {'query': query, 'page[size]': size, 'page[cursor]': cursor}
        if source.get('resource_types'):
            params['resource-type-id'] = ','.join(source['resource_types'])
        try:
            body, _, _ = client.get(endpoint(source, params))
        except Exception as error:
            if page:
                return Harvest(papers, True, cursor, start, end, pages=page, error=error)
            raise
        payload = json.loads(body)
        if not isinstance(payload.get('data'), list) or not isinstance(payload.get('links'), dict):
            raise ValueError('Unexpected DataCite response schema')
        for item in payload['data']:
            paper = map_guard('datacite', item, source, api)
            if paper is not None and paper.source_id not in seen:
                papers.append(paper)
                seen.add(paper.source_id)
        next_url = payload['links'].get('next')
        if not next_url:
            return Harvest(papers, since=start, until=end, pages=page + 1)
        parts = urllib.parse.urlsplit(next_url)
        original = urllib.parse.urlsplit(source['url'])
        if (parts.scheme, parts.netloc, parts.path) != (original.scheme, original.netloc, original.path):
            raise ValueError('DataCite next link leaves its configured endpoint')
        values = urllib.parse.parse_qs(parts.query).get('page[cursor]', [])
        if len(values) != 1 or values[0] == cursor or values[0] in tokens:
            raise ValueError('DataCite next cursor missing or repeated')
        tokens.add(cursor)
        cursor = values[0]
    return Harvest(papers, True, cursor, start, end, pages=count)


OAI = {'o': 'http://www.openarchives.org/OAI/2.0/', 'dc': 'http://purl.org/dc/elements/1.1/'}


@register('oai_pmh', options=('metadata_prefix', 'set_spec', 'date_granularity'),
          coverage='Repository OAI-PMH changes, including deletion notices; PDF availability varies',
          incremental_axis='OAI header datestamp', resumable=True)
def oai_pmh(client, source, since, max_pages, api):
    start, end, cursor = window(source, since, api)
    _, count = limits(source, max_pages)
    papers, deletions, tokens = [], [], set()
    for page in range(count):
        params = dict(verb='ListRecords')
        if cursor:
            params['resumptionToken'] = cursor
        else:
            params['metadataPrefix'] = source.get('metadata_prefix', 'oai_dc')
            day = source.get('date_granularity', 'seconds') == 'days'
            params['until'] = utc_query(end, api)[:10] if day else utc_query(end, api)
            if start:
                params['from'] = utc_query(start, api)[:10] if day else utc_query(start, api)
            if source.get('set_spec'):
                params['set'] = source['set_spec']
        try:
            body, _, _ = client.get(endpoint(source, params))
        except Exception as error:
            if page:
                return Harvest(papers, True, cursor, start, end, deletions, page, error=error)
            raise
        root = ET.fromstring(body)
        if root.tag != '{http://www.openarchives.org/OAI/2.0/}OAI-PMH':
            raise ValueError('Unexpected OAI-PMH response (possibly a login/challenge page)')
        errors = root.findall('o:error', OAI)
        if errors:
            if len(errors) == 1 and errors[0].get('code') == 'noRecordsMatch':
                return Harvest(papers, since=start, until=end, deletions=deletions, pages=page + 1)
            if any(e.get('code') == 'badResumptionToken' for e in errors):
                raise CheckpointExpired('OAI-PMH badResumptionToken; restart the saved window')
            raise ValueError('OAI-PMH: ' + '; '.join((e.get('code', '') + ': ' + (e.text or '')) for e in errors))
        container = root.find('o:ListRecords', OAI)
        if container is None:
            raise ValueError('OAI-PMH ListRecords missing')
        if not container.findall('o:record', OAI):
            raise ValueError('Empty OAI ListRecords without noRecordsMatch')
        for entry in container.findall('o:record', OAI):
            mapped = map_guard('oai_pmh', ET.tostring(entry, encoding='unicode'), source, api)
            if isinstance(mapped, dict):
                deletions.append(mapped)
            elif mapped is not None:
                papers.append(mapped)
        next_cursor = (container.findtext('o:resumptionToken', '', OAI) or '').strip()
        if not next_cursor:
            return Harvest(papers, since=start, until=end, deletions=deletions, pages=page + 1)
        if next_cursor == cursor or next_cursor in tokens:
            raise ValueError('OAI-PMH resumption token repeated')
        tokens.add(cursor)
        cursor = next_cursor
    return Harvest(papers, True, cursor, start, end, deletions, count)


for _kind, _coverage, _axis in (
        ('rss', 'Current feed window only; no full historical guarantee', 'Feed updated/publication date when provided'),
        ('html', 'Current static listing only; no full historical guarantee', 'New discovery / listing content changes'),
        ('arxiv', 'Official arXiv API query with bounded pages', 'arXiv lastUpdatedDate')):
    def _builtin(client, source, since, max_pages, api):
        return api.fetch_builtin_source(client, source, since, max_pages)
    register(_kind, options=('query', 'link_pattern', 'encoding'), coverage=_coverage, incremental_axis=_axis, resumable=(_kind == 'arxiv'))(_builtin)


def fetch(client, source, since, max_pages, api):
    if source['kind'] not in REGISTRY:
        raise ValueError('Unknown source kind: ' + source['kind'])
    return REGISTRY[source['kind']].fetch(client, source, since, max_pages, api)
