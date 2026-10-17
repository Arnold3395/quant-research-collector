#!/usr/bin/env python3
"""Rule-based financial research collector. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import html
from html.parser import HTMLParser
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET

UTC = timezone.utc
LOG = logging.getLogger('collector')
BASE = Path(__file__).resolve().parent
SCHEMA_VERSION = 4
ATOM = {'a': 'http://www.w3.org/2005/Atom', 'ar': 'http://arxiv.org/schemas/atom',
        'o': 'http://a9.com/-/spec/opensearch/1.1/'}


def now():
    return datetime.now(UTC)


def stamp(value=None):
    return (value or now()).astimezone(UTC).isoformat(timespec='seconds')


def text_only(value):
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', value or '')).split())


def normalized(value):
    return re.sub(r'[^\w\u3400-\u9fff]', '', unicodedata.normalize('NFKC', value).casefold())


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if result is None:
        return None
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def retry_after(headers):
    value = headers.get('Retry-After', '') if headers else ''
    if value.isdigit():
        return now() + timedelta(seconds=min(int(value), 31536000))
    return parse_date(value)


def error_retry_after(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, urllib.error.HTTPError):
            return retry_after(error.headers)
        error = error.__cause__
    return None


def canonical_url(value):
    parts = urllib.parse.urlsplit(html.unescape(value.strip()))
    scheme, host = parts.scheme.lower(), (parts.hostname or '').lower()
    if scheme not in ('http', 'https') or not host or parts.username or parts.password:
        raise ValueError('Only public HTTP(S) URLs without credentials are supported')
    if host in ('arxiv.org', 'export.arxiv.org', 'www.arxiv.org'):
        scheme = 'https'
    if parts.port and parts.port not in (80, 443):
        raise ValueError('Nonstandard HTTP ports are not supported')
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                                   if not k.lower().startswith('utm_')])
    return urllib.parse.urlunsplit((scheme, host, parts.path or '/', query, ''))


def arxiv_id(value):
    match = re.search(r'(?:arxiv\.org/(?:abs|pdf)/|arxiv:)([a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(v\d+)?', value, re.I)
    return (match.group(1), match.group(2) or '') if match else ('', '')


def doi_id(value):
    return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value.strip(), flags=re.I).lower()


@dataclass
class Paper:
    source: str
    source_id: str
    title: str
    url: str
    abstract: str = ''
    authors: list[str] = field(default_factory=list)
    published: str = ''
    updated: str = ''
    pdf_url: str = ''
    doi: str = ''
    arxiv: str = ''
    version: str = ''
    categories: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    score: int = 0
    rule_match: bool = True
    license_url: str = ''
    resource_type: str = ''
    source_deleted: bool = False

    def fingerprint(self):
        content = [self.title, self.abstract, self.updated, self.version, self.pdf_url,
                   self.authors, self.published, self.doi, self.arxiv, self.categories,
                   self.license_url, self.resource_type]
        return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()[:24]


class Page(HTMLParser):
    """Collect links, citation metadata and text without executing JavaScript."""
    def __init__(self, content=''):
        super().__init__(convert_charrefs=True)
        self.links, self.meta, self.parts = [], {}, []
        self.anchor = None
        self.ignored = 0
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.ignored += 1
        if tag == 'meta':
            key = (attrs.get('name') or attrs.get('property') or '').lower()
            if key:
                self.meta.setdefault(key, []).append(attrs.get('content', ''))
        if tag == 'a':
            self.anchor = [attrs.get('href', ''), [], attrs.get('title') or attrs.get('aria-label', '')]

    def handle_data(self, data):
        if self.ignored:
            return
        self.parts.append(data)
        if self.anchor is not None:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.ignored = max(0, self.ignored - 1)
        if tag == 'a' and self.anchor is not None:
            url, chunks, fallback = self.anchor
            self.links.append((url, text_only(' '.join(chunks)) or fallback))
            self.anchor = None

    def first(self, *keys):
        for key in keys:
            values = self.meta.get(key, [])
            if values and values[0]:
                return text_only(values[0])
        return ''


def xml_child(node, names):
    for name in names:
        for child in node:
            if child.tag.rsplit('}', 1)[-1].lower() == name.lower():
                return ''.join(child.itertext()).strip()
    return ''


def parse_feed(body, source):
    root = ET.fromstring(body)
    papers = []
    if root.tag == '{http://www.w3.org/2005/Atom}feed':
        entries = root.findall('a:entry', ATOM)
    else:
        entries = [n for n in root.iter() if n.tag.rsplit('}', 1)[-1] == 'item']
    for entry in entries:
        try:
            title = text_only(xml_child(entry, ['title']))
            url = xml_child(entry, ['link'])
            pdf, authors, categories = '', [], []
            if root.tag.endswith('}feed'):
                url = ''
                for link in entry.findall('a:link', ATOM):
                    href = link.get('href', '')
                    if link.get('type') == 'application/pdf' or link.get('title') == 'pdf':
                        pdf = href
                    elif link.get('rel', 'alternate') == 'alternate':
                        url = href
                url = url or xml_child(entry, ['id'])
                authors = [text_only(a.findtext('a:name', '', ATOM)) for a in entry.findall('a:author', ATOM)]
                categories = [n.get('term', '') for n in entry.findall('a:category', ATOM)]
            else:
                author = text_only(xml_child(entry, ['creator', 'author']))
                authors = [author] if author else []
                for child in entry:
                    if child.tag.rsplit('}', 1)[-1] in ('enclosure', 'content') and child.get('type') == 'application/pdf':
                        pdf = child.get('url', '')
            if not title or not url:
                if source.get('_item_error_handler'):
                    raise ValueError('Feed item missing title or URL')
                continue
            url = canonical_url(urllib.parse.urljoin(source['url'], url))
            aid, version = arxiv_id(url)
            if aid:
                pdf = pdf or 'https://arxiv.org/pdf/' + aid + version
            if urllib.parse.urlsplit(url).path.lower().endswith('.pdf'):
                pdf = url
            published = xml_child(entry, ['published', 'pubDate', 'date'])
            updated = xml_child(entry, ['updated', 'modified']) or published
            # Some feeds supply only an updated timestamp; never fabricate a publication date.
            papers.append(Paper(source['id'], xml_child(entry, ['id', 'guid']) or url, title, url,
                                abstract=text_only(xml_child(entry, ['summary', 'description', 'encoded', 'content'])),
                                authors=authors, published=published, updated=updated, pdf_url=pdf,
                                doi=doi_id(xml_child(entry, ['doi'])), arxiv=aid, version=version,
                                categories=categories))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            handler = source.get('_item_error_handler')
            if handler is None:
                raise
            wrapper = ET.Element(root.tag)
            wrapper.append(entry)
            handler('feed', ET.tostring(wrapper, encoding='unicode'), error)
    return papers


def parse_listing(body, source):
    page = Page(body.decode(source.get('encoding', 'utf-8'), 'replace'))
    pattern = re.compile(source['link_pattern'], re.I)
    papers, seen = [], set()
    for href, title in page.links:
        try:
            url = canonical_url(urllib.parse.urljoin(source['url'], href))
        except ValueError:
            continue
        if not pattern.search(url) or not title.strip() or url in seen:
            continue
        seen.add(url)
        papers.append(Paper(source['id'], url, title.strip(), url,
                            pdf_url=url if urllib.parse.urlsplit(url).path.lower().endswith('.pdf') else ''))
    return papers


class Matcher:
    def __init__(self, config):
        self.topics = config['topics']
        self.exclude = config.get('exclude_terms', [])
        self.minimum = config.get('minimum_score', 2)

    @staticmethod
    def contains(text, term):
        term = unicodedata.normalize('NFKC', term).casefold()
        if re.fullmatch(r'[a-z0-9 ._/-]+', term):
            return re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text) is not None
        return term in text

    def match(self, paper, source):
        paper.topics, paper.matched_terms, paper.score, paper.rule_match = [], [], 0, False
        title = unicodedata.normalize('NFKC', paper.title).casefold()
        abstract = unicodedata.normalize('NFKC', paper.abstract).casefold()
        if any(self.contains(title, word) for word in self.exclude):
            return False
        topics, terms, score = [], [], 0
        for topic, keywords in self.topics.items():
            matches = [word for word in keywords if self.contains(title, word) or self.contains(abstract, word)]
            if matches:
                topics.append(topic)
                terms.extend(matches)
                score += sum(3 if self.contains(title, word) else 1 for word in matches)
        finance_category = any(c.startswith('q-fin.') for c in paper.categories)
        if finance_category:
            topics.append('量化金融分类')
            score += 3
        paper.topics, paper.matched_terms, paper.score = sorted(set(topics)), sorted(set(terms)), score
        paper.rule_match = score >= source.get('minimum_score', self.minimum)
        return paper.rule_match


class PolicyError(RuntimeError):
    pass


class RunBudgetExceeded(TimeoutError):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, client):
        self.client = client

    def redirect_request(self, request, response, code, message, headers, newurl):
        newurl = canonical_url(urllib.parse.urljoin(request.full_url, newurl))
        self.client.validate(newurl)
        if not getattr(request, '_is_robots', False):
            self.client.check_robots(newurl)
        self.client.throttle(newurl)
        new = super().redirect_request(request, response, code, message, headers, newurl)
        if new is not None:
            new._is_robots = getattr(request, '_is_robots', False)
        return new


class Client:
    def __init__(self, settings, sources, deadline=None):
        self.settings = settings
        self.allowed = set(d.lower() for s in sources for d in s['allowed_domains'])
        self.last, self.robots = {}, {}
        self.deadline = deadline
        context = ssl.create_default_context(cafile=settings.get('ca_file') or None)
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), SafeRedirect(self))
        self.agent = settings.get('user_agent', 'QuantResearchCollector/1.0 (personal research)')
        self.cache_root = None
        self.host_waits = {}
        self.retry_observer = None
        self.metrics = dict(requests=0, bytes_received=0, not_modified=0)

    def validate(self, url):
        url = canonical_url(url)
        host = urllib.parse.urlsplit(url).hostname
        if not any(host == d or host.endswith('.' + d) for d in self.allowed):
            raise PolicyError('Host is outside configured allowed_domains: ' + host)
        # Domain allowlist is the trust boundary. Do not reject a named public
        # endpoint based on local DNS: transparent proxies often use fake-IP DNS.
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise PolicyError('Nonpublic IP literal is not allowed: ' + host)
        return url

    def throttle(self, url):
        self.throttle_deadline()
        host = urllib.parse.urlsplit(url).hostname
        until = parse_date(self.host_waits.get(host, ''))
        if until and until > now():
            raise urllib.error.HTTPError(url, 429, 'Host waiting for Retry-After', {'Retry-After': stamp(until)}, None)
        key = 'arxiv.org' if host.endswith('arxiv.org') else host
        delay = max(float(self.settings.get('request_interval_seconds', 1.0)), 3.2 if key == 'arxiv.org' else 0)
        wait = delay - (time.monotonic() - self.last.get(key, 0))
        if wait > 0:
            time.sleep(wait)
        self.last[key] = time.monotonic()

    def check_robots(self, url):
        parts = urllib.parse.urlsplit(url)
        # Explicit official API: use its published API rules, not web-page robots rules.
        if parts.hostname == 'export.arxiv.org' and parts.path == '/api/query':
            return
        origin = f'{parts.scheme}://{parts.netloc}'
        if origin not in self.robots:
            robot = urllib.robotparser.RobotFileParser()
            try:
                body, _, _ = self.get(origin + '/robots.txt', max_bytes=1_000_000, robots=False)
                robot.parse(body.decode('utf-8', 'replace').splitlines())
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    robot.parse([])
                else:
                    raise PolicyError(f'robots.txt returned HTTP {error.code}: {origin}') from error
            except RunBudgetExceeded:
                raise
            except Exception as error:
                raise PolicyError(f'Cannot verify robots.txt: {origin}: {error}') from error
            self.robots[origin] = robot
        robot = self.robots[origin]
        if not robot.can_fetch(self.agent, url):
            raise PolicyError('robots.txt disallows: ' + url)
        delay = robot.crawl_delay(self.agent) or robot.crawl_delay('*')
        if delay:
            if delay > 60:
                raise PolicyError('robots.txt crawl-delay exceeds this collector budget: ' + url)
            key = parts.hostname
            wait = delay - (time.monotonic() - self.last.get(key, 0))
            if wait > 0:
                time.sleep(wait)

    def get(self, url, max_bytes=5_000_000, robots=True, conditional=False):
        url = self.validate(url)
        if robots:
            self.check_robots(url)
        cache, cache_path = None, None
        if conditional and self.cache_root:
            cache_path = Path(self.cache_root) / (hashlib.sha256(url.encode()).hexdigest() + '.json')
            try:
                import base64
                cache = json.loads(cache_path.read_text(encoding='utf-8'))
                if not isinstance(cache, dict) or not isinstance(cache.get('headers'), dict):
                    raise ValueError('Invalid HTTP cache structure')
                body = base64.b64decode(cache['body'], validate=True)
                if cache['url'] != url or len(body) > max_bytes or hashlib.sha256(body).hexdigest() != cache['sha256']:
                    raise ValueError('Invalid HTTP cache')
                self.validate(cache['final_url'])
            except (OSError, ValueError, KeyError, TypeError, PolicyError):
                cache = None
        tries = int(self.settings.get('http_attempts', 2))
        for attempt in range(tries):
            self.throttle(url)
            headers = {'User-Agent': self.agent, 'Accept-Encoding': 'identity'}
            if cache:
                for header in ('ETag', 'Last-Modified'):
                    value = next((v for k, v in cache['headers'].items() if k.lower() == header.lower()), '')
                    if value:
                        headers['If-None-Match' if header == 'ETag' else 'If-Modified-Since'] = value
            request = urllib.request.Request(url, headers=headers)
            request._is_robots = not robots
            try:
                self.metrics['requests'] += 1
                with self.opener.open(request, timeout=float(self.settings.get('timeout_seconds', 25))) as response:
                    length = response.headers.get('Content-Length')
                    if length and int(length) > max_bytes:
                        raise ValueError('Response exceeds byte limit')
                    chunks, size = [], 0
                    while True:
                        self.throttle_deadline()
                        # read1 returns after one buffered/socket read, so a slowly
                        # trickling response cannot hide the run deadline inside read(n).
                        read = getattr(response, 'read1', response.read)
                        block = read(min(65536, max_bytes + 1 - size))
                        if not block:
                            break
                        chunks.append(block)
                        size += len(block)
                        self.metrics['bytes_received'] += len(block)
                        if size > max_bytes:
                            raise ValueError('Response exceeds byte limit')
                    if length and size != int(length):
                        raise http.client.IncompleteRead(b'', max(0, int(length) - size))
                    result = b''.join(chunks), dict(response.headers), response.url
                    if cache_path:
                        import base64
                        atomic_text(cache_path, json.dumps(dict(url=url, final_url=response.url,
                                    headers=dict(response.headers), sha256=hashlib.sha256(result[0]).hexdigest(),
                                    body=base64.b64encode(result[0]).decode()), ensure_ascii=False))
                    observer = getattr(self, 'raw_observer', None)
                    if observer:
                        observer(result[2], result[0])
                    return result
            except RunBudgetExceeded:
                raise
            except urllib.error.HTTPError as error:
                until = retry_after(error.headers)
                if error.code in (429, 503) and until and until > now():
                    host = urllib.parse.urlsplit(error.url).hostname
                    self.host_waits[host] = stamp(until)
                    if self.retry_observer:
                        self.retry_observer(host, stamp(until))
                if error.code == 304 and cache:
                    # Recheck access rules even when content comes from the local cache.
                    if cache['final_url'] != url and robots:
                        self.check_robots(cache['final_url'])
                    self.metrics['not_modified'] += 1
                    error.close()
                    observer = getattr(self, 'raw_observer', None)
                    if observer:
                        observer(cache['final_url'], body)
                    return body, cache['headers'], cache['final_url']
                if error.code not in (429, 500, 502, 503, 504) or attempt + 1 >= tries:
                    raise
                until = retry_after(error.headers)
                wait = max(0, (until - now()).total_seconds()) if until else 2 ** (attempt + 1)
                if wait > 60:
                    raise
                error.close()
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
                if attempt + 1 >= tries:
                    raise
                time.sleep(2 ** (attempt + 1))
        raise RuntimeError('No HTTP attempts configured')

    def throttle_deadline(self):
        if self.deadline and time.monotonic() >= self.deadline:
            raise RunBudgetExceeded('Configured run time limit reached')


def fetch_builtin_source(client, source, since, max_pages=None):
    if source['kind'] == 'arxiv':
        import adapters
        start, end, cursor = adapters.window(source, since, sys.modules[__name__])
        query = source['query']
        if since:
            query += f' AND lastUpdatedDate:[{parse_date(start).strftime("%Y%m%d%H%M")} TO {parse_date(end).strftime("%Y%m%d%H%M")}]'
        size = source.get('page_size', 100)
        limit = max_pages or source.get('max_pages', 10)
        papers, seen = [], set()
        offset = int(cursor or 0)
        for index in range(limit):
            params = urllib.parse.urlencode(dict(search_query=query, start=offset + index * size, max_results=size,
                                                 sortBy='lastUpdatedDate', sortOrder='descending'))
            try:
                body, _, _ = client.get(source['url'] + '?' + params)
            except Exception as error:
                if index:
                    return adapters.Harvest(papers, True, str(offset + index * size), start, end, pages=index, error=error)
                raise
            batch = parse_feed(body, source)
            root = ET.fromstring(body)
            total_text = root.findtext('o:totalResults', '', ATOM)
            if not total_text.isdigit():
                raise ValueError('arXiv response missing valid totalResults')
            total = int(total_text)
            if any(p.source_id.endswith('/errors') for p in batch):
                raise RuntimeError('arXiv API error response')
            for paper in batch:
                if paper.source_id not in seen:
                    papers.append(paper)
                    seen.add(paper.source_id)
            if not root.findall('a:entry', ATOM) or offset + (index + 1) * size >= total:
                return adapters.Harvest(papers, since=start, until=end, pages=index + 1)
        return adapters.Harvest(papers, True, str(offset + limit * size), start, end, pages=limit)
    body, _, _ = client.get(source['url'], conditional=True)
    if source['kind'] == 'rss':
        papers = parse_feed(body, source)
    elif source['kind'] == 'html':
        papers = parse_listing(body, source)
    else:
        raise ValueError('Unknown source kind: ' + source['kind'])
    if not papers:
        raise RuntimeError('No entries parsed; endpoint may have changed (not treated as zero new papers)')
    return papers, False


def fetch_source(client, source, since, max_pages=None):
    import adapters
    return adapters.fetch(client, source, since, max_pages, sys.modules[__name__])


def resolve_pdf(client, paper, source):
    if paper.pdf_url:
        return paper.pdf_url
    if not source.get('resolve_pdf', True):
        return ''
    body, headers, final_url = client.get(paper.url)
    if body.lstrip().startswith(b'%PDF-'):
        return final_url
    page = Page(body.decode('utf-8', 'replace'))
    for key in ('citation_pdf_url', 'eprints.document_url'):
        for value in page.meta.get(key, []):
            candidate = urllib.parse.urljoin(final_url, value)
            client.validate(candidate)
            return candidate
    candidates = []
    for href, label in page.links:
        if not re.search(r'\.pdf(?:[?#]|$)', href, re.I):
            continue
        if re.search(r'footer|form.crs|privacy|disclaimer|terms|employer|accessibility', href + ' ' + label, re.I):
            continue
        candidate = urllib.parse.urljoin(final_url, href)
        try:
            client.validate(candidate)
        except (ValueError, PolicyError):
            continue
        score = 0
        if re.search(r'download|full.text|paper|report|下载|全文|报告', label, re.I):
            score += 4
        if re.search(r'appendix|supplement|slides|附录|演示', href + ' ' + label, re.I):
            score -= 5
        if '/publ/work' in candidate or '/econres/' in candidate:
            score += 3
        candidates.append((score, candidate))
    if candidates:
        candidates.sort(key=lambda pair: (-pair[0], pair[1]))
        # Ambiguous pages are left as metadata instead of downloading an unrelated PDF.
        best = [u for score, u in candidates if score == candidates[0][0]]
        if len(set(best)) == 1:
            return best[0]
    return ''


SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, norm_title TEXT NOT NULL,
 first_author TEXT NOT NULL, created_at TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identifiers (
 kind TEXT NOT NULL, value TEXT NOT NULL, document_id INTEGER NOT NULL REFERENCES documents(id),
 PRIMARY KEY(kind,value)
);
CREATE TABLE IF NOT EXISTS records (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
 source TEXT NOT NULL, source_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 metadata TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 pdf_status TEXT NOT NULL DEFAULT 'pending', pdf_path TEXT NOT NULL DEFAULT '',
 pdf_url TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT NOT NULL DEFAULT '',
 next_retry TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', UNIQUE(source,source_id,fingerprint)
);
CREATE TABLE IF NOT EXISTS source_state (source TEXT PRIMARY KEY, last_success TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started TEXT NOT NULL, finished TEXT, status TEXT, details TEXT);
CREATE INDEX IF NOT EXISTS records_doc ON records(document_id);
CREATE INDEX IF NOT EXISTS documents_title ON documents(norm_title,first_author);
CREATE TABLE IF NOT EXISTS source_health (
 source TEXT PRIMARY KEY, config_hash TEXT NOT NULL DEFAULT '', last_attempt TEXT NOT NULL DEFAULT '',
 last_status TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
 next_retry TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', attempt_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS download_events (
 id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL REFERENCES records(id),
 timestamp TEXT NOT NULL, result TEXT NOT NULL, details TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS harvest_checkpoints (
 source TEXT NOT NULL, mode TEXT NOT NULL, config_hash TEXT NOT NULL,
 window_since TEXT NOT NULL, window_until TEXT NOT NULL, cursor TEXT NOT NULL,
 PRIMARY KEY(source,mode)
);
CREATE TABLE IF NOT EXISTS source_notices (
 source TEXT NOT NULL, source_id TEXT NOT NULL, datestamp TEXT NOT NULL,
 status TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(source,source_id,datestamp,status)
);
CREATE INDEX IF NOT EXISTS records_queue ON records(pdf_status,attempts,first_seen);
CREATE INDEX IF NOT EXISTS records_pdf_url ON records(pdf_url,pdf_status);
'''


class Library:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'library.sqlite3', timeout=30)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            self.db.close()
            raise ValueError('Database schema is newer than this collector; upgrade the code')
        existing = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='records'").fetchone()
        if existing and version < SCHEMA_VERSION:
            backup = self.root / 'backups' / ('pre-migration-' + now().strftime('%Y%m%dT%H%M%S%f') + '.sqlite3')
            backup.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(backup)) as target:
                self.db.backup(target)
        from pipeline import SCHEMA as PIPELINE_SCHEMA
        self.db.executescript(SCHEMA + PIPELINE_SCHEMA)
        if 'evaluated' not in {r[1] for r in self.db.execute('PRAGMA table_info(source_metrics)')}:
            self.db.execute('ALTER TABLE source_metrics ADD COLUMN evaluated INTEGER NOT NULL DEFAULT 0')
            self.db.execute('UPDATE source_metrics SET evaluated=fetched')
            self.db.commit()
        if 'next_retry' not in {r[1] for r in self.db.execute('PRAGMA table_info(records)')}:
            self.db.execute("ALTER TABLE records ADD COLUMN next_retry TEXT NOT NULL DEFAULT ''")
        if 'attempt_hash' not in {r[1] for r in self.db.execute('PRAGMA table_info(source_health)')}:
            self.db.execute("ALTER TABLE source_health ADD COLUMN attempt_hash TEXT NOT NULL DEFAULT ''")
        if version < SCHEMA_VERSION:
            with self.db:
                if version < 3:
                    for row in self.db.execute('SELECT id,metadata FROM records').fetchall():
                        self.db.execute('UPDATE records SET fingerprint=? WHERE id=?',
                                        (Paper(**json.loads(row['metadata'])).fingerprint(), row['id']))
                self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def upsert(self, paper, commit=True):
        identifiers = [('url', canonical_url(paper.url)), ('source:' + paper.source, paper.source_id)]
        if paper.doi:
            identifiers.append(('doi', doi_id(paper.doi)))
        if paper.arxiv:
            identifiers.append(('arxiv', paper.arxiv))
        ids = {row[0] for kind, value in identifiers
               for row in self.db.execute('SELECT document_id FROM identifiers WHERE kind=? AND value=?', (kind, value))}
        if len(ids) > 1:
            raise ValueError('Conflicting identifiers; manual review required: ' + paper.title)
        first_author = normalized(paper.authors[0]) if paper.authors else ''
        doc_id = next(iter(ids), None)
        if doc_id is None and first_author:
            rows = self.db.execute('SELECT id FROM documents WHERE norm_title=? AND first_author=?',
                                   (normalized(paper.title), first_author)).fetchall()
            if len(rows) == 1:
                candidate = rows[0][0]
                incoming = {kind: value for kind, value in identifiers if kind in ('doi', 'arxiv')}
                known = self.db.execute("SELECT kind,value FROM identifiers WHERE document_id=? AND kind IN ('doi','arxiv')", (candidate,)).fetchall()
                if not any(kind in incoming and incoming[kind] != value for kind, value in known):
                    doc_id = candidate
        new_doc = doc_id is None
        timestamp = stamp()
        if new_doc:
            doc_id = self.db.execute('INSERT INTO documents(title,norm_title,first_author,created_at,last_seen) VALUES(?,?,?,?,?)',
                                      (paper.title, normalized(paper.title), first_author, timestamp, timestamp)).lastrowid
        else:
            self.db.execute('UPDATE documents SET last_seen=? WHERE id=?', (timestamp, doc_id))
        for kind, value in identifiers:
            self.db.execute('INSERT OR IGNORE INTO identifiers VALUES(?,?,?)', (kind, value, doc_id))
        metadata = json.dumps(asdict(paper), ensure_ascii=False)
        result = self.db.execute('INSERT OR IGNORE INTO records(document_id,source,source_id,fingerprint,metadata,first_seen,last_seen,pdf_url) VALUES(?,?,?,?,?,?,?,?)',
                                (doc_id, paper.source, paper.source_id, paper.fingerprint(), metadata, timestamp, timestamp, paper.pdf_url))
        new_record = result.rowcount == 1
        row = self.db.execute('SELECT id FROM records WHERE source=? AND source_id=? AND fingerprint=?',
                              (paper.source, paper.source_id, paper.fingerprint())).fetchone()
        self.db.execute('UPDATE records SET last_seen=?,metadata=? WHERE id=?', (timestamp, metadata, row[0]))
        if commit:
            self.db.commit()
        return row[0], new_doc, new_record

    def success(self, source):
        row = self.db.execute('SELECT last_success FROM source_state WHERE source=?', (source,)).fetchone()
        return parse_date(row[0]) if row else None

    def reconcile_downloads(self):
        """Put missing/unreadable local PDFs back in the persistent download queue."""
        # A pending record with an attempt timestamp was interrupted mid-download.
        # The caller owns the process lock, so no download can still be writing it.
        resumed = self.db.execute("UPDATE records SET attempts=max(attempts-1,0),last_attempt='',error='Previous download interrupted; queued for recovery' WHERE pdf_status='pending' AND last_attempt!=''").rowcount
        missing = []
        paths = self.db.execute("SELECT DISTINCT pdf_path,sha256 FROM records WHERE pdf_status='downloaded'").fetchall()
        for row in paths:
            path = (self.root / row[0]).resolve()
            try:
                if not row[0] or not path.is_relative_to(self.root.resolve()):
                    raise ValueError('Invalid stored PDF path')
                with path.open('rb') as handle:
                    if not handle.read(1024).lstrip().startswith(b'%PDF-'):
                        raise ValueError('Local file is not a PDF')
                    handle.seek(0)
                    if hashlib.file_digest(handle, 'sha256').hexdigest() != row['sha256']:
                        raise ValueError('Local PDF checksum mismatch')
            except (OSError, ValueError):
                missing.append(row[0])
        repaired = resumed
        for path in missing:
            result = self.db.execute("UPDATE records SET pdf_status='pending',pdf_path='',sha256='',attempts=0,last_attempt='',next_retry='',error='Local PDF missing, corrupt or unreadable; queued for recovery' WHERE pdf_status='downloaded' AND pdf_path=?", (path,))
            repaired += result.rowcount
        self.db.commit()
        return repaired

    def download(self, client, row, source, settings):
        # Re-read state: another record in this run may already have supplied this file.
        row = self.db.execute('SELECT * FROM records WHERE id=?', (row['id'],)).fetchone()
        if row['pdf_status'] == 'downloaded':
            return 'already_downloaded'
        paper = Paper(**json.loads(row['metadata']))
        def record_event(result):
            error = self.db.execute('SELECT error FROM records WHERE id=?', (row['id'],)).fetchone()[0]
            self.db.execute('INSERT INTO download_events(record_id,timestamp,result,details) VALUES(?,?,?,?)',
                            (row['id'], stamp(), result, error))
            self.db.commit()
            return result
        attempt = row['attempts'] + 1
        self.db.execute('UPDATE records SET attempts=?,last_attempt=? WHERE id=?', (attempt, stamp(), row['id']))
        self.db.commit()
        try:
            pdf = row['pdf_url'] or resolve_pdf(client, paper, source)
            if not pdf:
                self.db.execute("UPDATE records SET pdf_status='no_public_pdf',error='' WHERE id=?", (row['id'],))
                self.db.commit()
                return record_event('no_public_pdf')
            pdf = canonical_url(pdf)
            self.db.execute('UPDATE records SET pdf_url=? WHERE id=?', (pdf, row['id']))
            self.db.commit()
            donors = self.db.execute("SELECT * FROM records WHERE document_id=? AND pdf_url=? AND pdf_status='downloaded' AND id!=?",
                                     (row['document_id'], pdf, row['id'])).fetchall()
            for donor in donors:
                old = Paper(**json.loads(donor['metadata']))
                if (old.version, old.updated) != (paper.version, paper.updated):
                    continue
                if not (paper.version or paper.updated) and old.fingerprint() != paper.fingerprint():
                    # A mutable URL with changed metadata and no revision marker
                    # cannot prove that the remote PDF is still the same version.
                    continue
                local = (self.root / donor['pdf_path']).resolve()
                if not local.is_relative_to(self.root.resolve()):
                    continue
                try:
                    valid = hashlib.sha256(local.read_bytes()).hexdigest() == donor['sha256']
                except OSError:
                    valid = False
                if valid:
                    self.db.execute("UPDATE records SET pdf_status='downloaded',pdf_path=?,sha256=?,next_retry='',error='' WHERE id=?",
                                    (donor['pdf_path'], donor['sha256'], row['id']))
                    return record_event('reused_pdf')
            body, _, _ = client.get(pdf, max_bytes=int(settings.get('max_pdf_mb', 40)) * 1024 * 1024)
            if not body.lstrip().startswith(b'%PDF-'):
                raise ValueError('URL did not return a PDF (login page or changed link)')
            digest = hashlib.sha256(body).hexdigest()
            relative = Path('pdf') / digest[:2] / (digest + '.pdf')
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                temp = path.with_suffix('.part')
                temp.write_bytes(body)
                temp.replace(path)
            self.db.execute("UPDATE records SET pdf_status='downloaded',pdf_path=?,sha256=?,next_retry='',error='' WHERE id=?",
                            (relative.as_posix(), digest, row['id']))
            self.db.commit()
            return record_event('downloaded')
        except RunBudgetExceeded:
            self.db.execute("UPDATE records SET pdf_status='pending',attempts=?,last_attempt='',error='Deferred: run time budget exhausted' WHERE id=?", (row['attempts'], row['id']))
            self.db.commit()
            return record_event('deferred_budget')
        except Exception as error:
            status = 'policy_blocked' if isinstance(error, PolicyError) else 'download_failed'
            until = error_retry_after(error)
            self.db.execute('UPDATE records SET pdf_status=?,error=?,next_retry=? WHERE id=?',
                            (status, str(error)[:1500], stamp(until) if until else '', row['id']))
            self.db.commit()
            LOG.warning('PDF %s: %s: %s', status, paper.title, error)
            return record_event(status)


@contextmanager
def run_lock(root):
    path = Path(root) / '.collector.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another collector process is already running') from None
    try:
        yield
    finally:
        handle.seek(0)
        if sys.platform == 'win32':
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.part')
    temp.write_text(content, encoding='utf-8')
    temp.replace(path)


def export_library(library, run_info=None, days=7, save_run=True):
    rows = library.db.execute('SELECT * FROM records ORDER BY first_seen DESC,id DESC').fetchall()
    documents = []
    for row in rows:
        item = dict(row)
        item['metadata'] = json.loads(item['metadata'])
        documents.append(item)
    atomic_text(library.root / 'catalog.jsonl', ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in documents))
    since = now() - timedelta(days=days)
    recent = [r for r in documents if parse_date(r['first_seen']) >= since]
    recent.sort(key=lambda r: (-r['metadata']['score'], r['metadata']['title']))
    lines = ['# 量化与金融工程文献目录', '', f'生成时间：{stamp()}（UTC）。最近 {days} 天首次发现的记录/版本：{len(recent)}。',
             '', '摘要来自原发布方，分类和评分来自关键词规则；没有调用 AI。首次发现时间不等于发表时间。', '']
    if run_info:
        lines += ['## 本次采集状态', '', '```json', json.dumps(run_info, ensure_ascii=False, indent=2), '```', '']
    cards = []
    for row in recent:
        p = row['metadata']
        lines += ['## ' + p['title'].replace('\n', ' '), '',
                  f"- 来源：{p['source']}；发表：{p['published'] or '未提取'}；更新：{p['updated'] or '未提取'}",
                  f"- 作者：{', '.join(p['authors']) or '未提取'}", f"- 分类：{', '.join(p['topics'])}；分数：{p['score']}；符合重新筛选规则：{p.get('rule_match', True)}",
                  f"- 关键词：{', '.join(p['matched_terms']) or '分类匹配'}", f"- 原文：{p['url']}",
                  f"- PDF：{row['pdf_status']}" + (f"；[{row['pdf_path']}]({row['pdf_path']})" if row['pdf_path'] else ''),
                  '', p['abstract'] or '当前采集入口未取得摘要。', '']
        title = html.escape(p['title'])
        link = html.escape(p['url'], quote=True)
        pdf_link = f'<a href="{html.escape(row["pdf_path"], quote=True)}">本地 PDF</a>' if row['pdf_path'] else ''
        if not p.get('rule_match', True):
            pdf_link += ' · 未通过重新筛选的规则'
        if p.get('source_deleted', False):
            pdf_link += ' · 来源条目已删除（保留历史）'
        cards.append(f'<article><h2><a href="{link}">{title}</a></h2><p>{html.escape(p["source"])} · '
                     f'{html.escape(", ".join(p["topics"]))} · {html.escape(row["pdf_status"])} {pdf_link}</p>'
                     f'<p>{html.escape(p["abstract"] or "当前采集入口未取得摘要。")}</p></article>')
    atomic_text(library.root / 'latest.md', '\n'.join(lines))
    status = html.escape(json.dumps(run_info or {}, ensure_ascii=False, indent=2))
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>量化研究文献库</title>'
            '<style>body{max-width:1080px;margin:32px auto;padding:0 20px;font:16px/1.7 system-ui;background:#f5f7fa;color:#172538}'
            'article{background:white;padding:20px;margin:16px 0;border-radius:10px}h2{font-size:20px}a{color:#1754a6}'
            'input{width:90%;padding:12px}pre{white-space:pre-wrap}</style>'
            '<h1>量化与金融工程文献库</h1><p>规则筛选 · 来源摘要 · 无 AI。此页展示最近首次发现的记录/版本。</p>'
            '<input id="q" placeholder="搜索本页标题、摘要、分类"><details><summary>采集状态</summary><pre>' + status + '</pre></details>'
            + ''.join(cards) + '<script>document.getElementById("q").addEventListener("input",function(){'
            'let q=this.value.toLowerCase();document.querySelectorAll("article").forEach(a=>a.hidden=!a.textContent.toLowerCase().includes(q))})</script></html>')
    atomic_text(library.root / 'index.html', page)
    if run_info and save_run:
        archive = '\n'.join(lines).replace('](pdf/', '](../pdf/')
        atomic_text(library.root / 'reports' / (run_info['run_id'] + '.md'), archive)
        atomic_text(library.root / 'last_run.json', json.dumps(run_info, ensure_ascii=False, indent=2))


def load_config(path):
    path = Path(path).resolve()
    with path.open('rb') as handle:
        config = json.load(handle) if path.suffix.lower() == '.json' else tomllib.load(handle)
    patterns = config.pop('source_files', [])
    if not isinstance(patterns, list) or any(not isinstance(p, str) or not p for p in patterns):
        raise ValueError('source_files must be an array of relative file/glob paths')
    included = set()
    for pattern in patterns:
        if Path(pattern).is_absolute() or '..' in Path(pattern).parts:
            raise ValueError('Source files must stay under the configuration directory')
        paths = sorted(path.parent.glob(pattern))
        if not paths:
            raise ValueError('No source files match: ' + pattern)
        for extra_path in paths:
            extra_path = extra_path.resolve()
            if not extra_path.is_relative_to(path.parent) or not extra_path.is_file():
                raise ValueError('Invalid source file path: ' + str(extra_path))
            if extra_path in included:
                continue
            included.add(extra_path)
            with extra_path.open('rb') as handle:
                extra = tomllib.load(handle)
            if set(extra) != {'sources'} or not isinstance(extra['sources'], list):
                raise ValueError('Source fragments may only contain [[sources]]: ' + str(extra_path))
            config.setdefault('sources', []).extend(extra['sources'])
    from maintenance import validate_config
    validate_config(config)
    ids = [s['id'] for s in config['sources']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate source ids')
    for source in config['sources']:
        if not re.fullmatch(r'[a-z0-9_-]+', source['id']):
            raise ValueError('Source id must contain lowercase ASCII letters, digits, hyphens or underscores')
        canonical_url(source['url'])
        if not source.get('allowed_domains'):
            raise ValueError('Each source needs allowed_domains')
        if source['kind'] == 'html':
            re.compile(source['link_pattern'])
    return config, path.parent


def select_sources(config, selection):
    if selection is None:
        sources = [s for s in config['sources'] if s.get('enabled', True)]
    else:
        selected = {value.strip() for value in selection.split(',') if value.strip()}
        if not selected:
            raise ValueError('No source ids supplied')
        known = {s['id'] for s in config['sources']}
        unknown = selected - known
        if unknown:
            raise ValueError('Unknown source id(s): ' + ', '.join(sorted(unknown)))
        sources = [s for s in config['sources'] if s['id'] in selected]
    if not sources:
        raise ValueError('No sources selected')
    return sources


def collect(args, config, config_root):
    from maintenance import source_hash
    import adapters
    import pipeline
    settings = config['collector']
    root = Path(args.data_dir or config_root / settings.get('data_dir', 'data')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(root / 'collector.log',
                maxBytes=int(settings.get('log_max_mb', 10) * 1024 * 1024),
                backupCount=int(settings.get('log_backups', 5)), encoding='utf-8')]
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=handlers, force=True)
    sources = select_sources(config, args.sources)
    total_deadline = min(getattr(args, '_deadline', float('inf')),
                         time.monotonic() + settings.get('max_run_minutes', 20) * 60)
    download_only = getattr(args, 'command', 'run') == 'download'
    discovery_deadline = (total_deadline if args.metadata_only else
                          time.monotonic() + max(0, total_deadline - time.monotonic()) * settings.get('discovery_budget_fraction', 0.65))
    client = Client(settings, sources, total_deadline)
    matcher = Matcher(config['filter'])
    with run_lock(root):
        library = Library(root)
        client.host_waits = pipeline.host_waits(library, sys.modules[__name__])
        client.retry_observer = lambda host, until: pipeline.state_value(library, 'host_retry:' + host, until)
        task = getattr(args, '_task', None)
        if task:
            current = library.db.execute('SELECT status FROM backfill_tasks WHERE id=?', (task['id'],)).fetchone()
            if current and current['status'] in ('done', 'limited', 'needs_review'):
                library.db.close()
                for handler in handlers:
                    logging.getLogger().removeHandler(handler)
                    handler.close()
                return 0
        client.cache_root = root / 'http_cache'
        # Owning the OS lock proves an earlier 'running' row has no active writer.
        library.db.execute("UPDATE runs SET status='interrupted',finished=? WHERE status='running'", (stamp(),))
        library.db.commit()
        recovered = library.reconcile_downloads()
        for configured in config['sources']:
            if not configured.get('download', True):
                library.db.execute("UPDATE records SET pdf_status='metadata_only' WHERE source=? AND pdf_status!='downloaded'", (configured['id'],))
            else:
                library.db.execute("UPDATE records SET pdf_status='pending' WHERE source=? AND pdf_status='metadata_only'", (configured['id'],))
        library.db.commit()
        run_id = now().strftime('%Y%m%dT%H%M%S') + '-' + str(time.time_ns())[-6:]
        config_json = json.dumps(config, sort_keys=True, ensure_ascii=False, indent=2)
        config_hash = hashlib.sha256(config_json.encode()).hexdigest()
        atomic_text(root / 'config_history' / (config_hash + '.json'), config_json)
        info = dict(run_id=run_id, started=stamp(), command=getattr(args, 'command', 'run'), sources=[], downloads={}, new_documents=0, new_records=0,
                    recovered_download_records=recovered, schema_version=SCHEMA_VERSION, config_hash=config_hash)
        library.db.execute('INSERT INTO runs(id,started,status) VALUES(?,?,?)', (run_id, info['started'], 'running'))
        library.db.commit()
        try:
            discovery_sources = [] if download_only else pipeline.rotate(sources, pipeline.state_value(library, 'last_discovery_source'))
            for source_index, source in enumerate(discovery_sources):
                source_started = time.monotonic()
                papers = []
                state = dict(source=source['id'], status='ok', fetched=0, accepted=0, evaluated=0, new_records=0, quarantined=0, errors=[])
                mode = 'backfill:' + str(task['id']) if task else str(args.days) if args.days is not None else 'normal'
                if time.monotonic() >= discovery_deadline:
                    state.update(status='deferred_budget', errors=['Discovery phase budget reached'])
                    info['sources'].append(state)
                    continue
                health = library.db.execute('SELECT * FROM source_health WHERE source=?', (source['id'],)).fetchone()
                fingerprint = source_hash(config, source)
                changed = health is None or health['config_hash'] != fingerprint
                next_retry = parse_date(health['next_retry']) if health else None
                if next_retry and next_retry > now() and health['attempt_hash'] == fingerprint and not getattr(args, 'force_sources', False) and args.days is None and not task:
                    state.update(status='backoff', next_retry=stamp(next_retry), errors=[health['error']])
                    info['sources'].append(state)
                    LOG.warning('Source %s cooling down until %s', source['id'], state['next_retry'])
                    continue
                try:
                    pipeline.state_value(library, 'last_discovery_source', source['id'])
                    fair_share = max(0.01, (discovery_deadline - time.monotonic()) / (len(discovery_sources) - source_index))
                    client.deadline = min(discovery_deadline, time.monotonic() + min(settings.get('source_max_seconds', 180), fair_share))
                    last = library.success(source['id'])
                    if args.days is not None:
                        since = now() - timedelta(days=args.days)
                    elif changed:
                        since = now() - timedelta(days=settings.get('initial_days', 14))
                        if last:
                            since = min(since, last - timedelta(days=settings.get('overlap_days', 3)))
                    elif last:
                        since = last - timedelta(days=settings.get('overlap_days', 3))
                    else:
                        since = now() - timedelta(days=settings.get('initial_days', 14))
                    state['since'] = stamp(since)
                    state['configuration_changed'] = changed
                    mode = ('backfill:' + str(task['id']) if task else
                            str(args.days) if args.days is not None else 'normal')
                    if task:
                        since = parse_date(task['window_since'])
                        state['since'] = task['window_since']
                        library.db.execute("UPDATE backfill_tasks SET status='running',updated_at=?,config_hash=? WHERE id=?",
                                           (stamp(), fingerprint, task['id']))
                        library.db.commit()
                    checkpoint = library.db.execute('SELECT * FROM harvest_checkpoints WHERE source=? AND mode=?',
                                                    (source['id'], mode)).fetchone()
                    runtime_source = dict(source, _until=task['window_until'] if task else info['started'])
                    def isolate(stage, payload, error):
                        pipeline.quarantine(library, source['id'], stage, payload, error, run_id,
                                            task['id'] if task else None, sys.modules[__name__])
                        state['quarantined'] += 1
                    runtime_source['_item_error_handler'] = isolate
                    client.raw_observer = lambda url, body: pipeline.capture(library, source['id'], url, body, settings, sys.modules[__name__])
                    if checkpoint and checkpoint['config_hash'] == fingerprint:
                        runtime_source['_checkpoint'] = dict(checkpoint)
                        since = parse_date(checkpoint['window_since'])
                        state.update(since=checkpoint['window_since'], resumed=True)
                    elif adapters.REGISTRY[source['kind']].resumable:
                        # Pin the first attempted window too: a first-page outage
                        # must not silently slide the initial lookback forward.
                        library.db.execute('INSERT OR REPLACE INTO harvest_checkpoints VALUES(?,?,?,?,?,?)',
                            (source['id'], mode, fingerprint, stamp(since), runtime_source['_until'], ''))
                        library.db.execute('INSERT OR REPLACE INTO checkpoint_tracking VALUES(?,?,?,?)',
                            (source['id'], mode, stamp(), stamp()))
                        library.db.commit()
                    try:
                        batch = fetch_source(client, runtime_source, since)
                    except adapters.CheckpointExpired:
                        if '_checkpoint' not in runtime_source:
                            raise
                        # An explicit token rejection permits a restart. Keep the
                        # original bounded window; ordinary network errors never reset it.
                        runtime_source['_checkpoint']['cursor'] = ''
                        library.db.execute('UPDATE harvest_checkpoints SET cursor=? WHERE source=? AND mode=?',
                                           ('', source['id'], mode))
                        library.db.commit()
                        state['checkpoint_restarted'] = True
                        LOG.warning('Expired checkpoint for %s; restarting its original window', source['id'])
                        batch = fetch_source(client, runtime_source, since)
                    papers, truncated = batch
                    state['fetched'] = len(papers)
                    if isinstance(batch, adapters.Harvest):
                        state.update(pages=batch.pages, window_until=batch.until)
                    for paper in papers:
                        try:
                            pipeline.validate_paper(paper, source, sys.modules[__name__])
                            date = parse_date(paper.updated) or parse_date(paper.published)
                            # HTML dates may be unknown; task coverage stays explicitly limited.
                            if date and date < since and not getattr(batch, 'server_filtered', False):
                                matcher.match(paper, source)
                                paper.rule_match = False
                                pipeline.remember(library, paper, source, config, sys.modules[__name__], reason='outside_window')
                                continue
                            if task and date and date >= parse_date(task['window_until']) and not getattr(batch, 'server_filtered', False):
                                matcher.match(paper, source)
                                paper.rule_match = False
                                pipeline.remember(library, paper, source, config, sys.modules[__name__], reason='outside_window')
                                continue
                            state['evaluated'] += 1
                            accepted = matcher.match(paper, source)
                            pipeline.remember(library, paper, source, config, sys.modules[__name__])
                            if not accepted:
                                continue
                            state['accepted'] += 1
                            record_id, new_doc, new_record = pipeline.ingest(library, paper, source, config, sys.modules[__name__])
                            state['new_records'] += int(new_record)
                            info['new_documents'] += int(new_doc)
                            info['new_records'] += int(new_record)
                        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.IntegrityError) as error:
                            isolate('paper', asdict(paper), error)
                    for notice in getattr(batch, 'deletions', []):
                        pipeline.apply_notice(library, source['id'], notice, sys.modules[__name__])
                    state['deletion_notices'] = len(getattr(batch, 'deletions', []))
                    if truncated:
                        if getattr(batch, 'cursor', ''):
                            state['status'] = 'continuing'
                            library.db.execute('INSERT OR REPLACE INTO harvest_checkpoints VALUES(?,?,?,?,?,?)',
                                (source['id'], mode, fingerprint, batch.since, batch.until, batch.cursor))
                            library.db.execute('''INSERT INTO checkpoint_tracking VALUES(?,?,?,?)
                                ON CONFLICT(source,mode) DO UPDATE SET updated_at=excluded.updated_at''',
                                (source['id'], mode, stamp(), stamp()))
                            state['checkpoint_saved'] = True
                        else:
                            state['status'] = 'truncated'
                            state['errors'].append('max_pages reached; watermark NOT advanced. Increase max_pages or shorten backfill window.')
                    else:
                        library.db.execute('DELETE FROM harvest_checkpoints WHERE source=? AND mode=?', (source['id'], mode))
                        library.db.execute('DELETE FROM checkpoint_tracking WHERE source=? AND mode=?', (source['id'], mode))
                        # A one-off historical/short scan must not skip an outstanding
                        # gap in the normal incremental cursor.
                        advance = not task and (args.days is None or last is None)
                        state['watermark_advanced'] = advance
                        if advance:
                            library.db.execute('INSERT OR REPLACE INTO source_state VALUES(?,?)',
                                               (source['id'], getattr(batch, 'until', '') or info['started']))
                        library.db.commit()
                    spec = adapters.REGISTRY[source['kind']]
                    state.update(coverage=spec.coverage, incremental_axis=spec.incremental_axis)
                    if getattr(batch, 'error', None) is not None:
                        error = batch.error
                        state['status'] = 'deferred_budget' if isinstance(error, RunBudgetExceeded) else 'failed'
                        state['errors'].append(str(error))
                        until = error_retry_after(error)
                        if until:
                            state['retry_after'] = stamp(until)
                    if state['status'] == 'ok' and pipeline.unresolved(library, source['id'], task['id'] if task else None):
                        state['status'] = 'needs_review'
                except RunBudgetExceeded as error:
                    state['status'] = 'deferred_budget'
                    state['errors'].append(str(error))
                except Exception as error:
                    state['status'] = 'failed'
                    state['errors'].append(str(error))
                    until = error_retry_after(error)
                    if until:
                        state['retry_after'] = stamp(until)
                    LOG.exception('Source failed: %s', source['id'])
                pipeline.record_metrics(library, state, papers, run_id, mode, fingerprint, source_started, sys.modules[__name__])
                pipeline.prune(library, settings, sys.modules[__name__])
                if task:
                    limited = source['kind'] in ('rss', 'html')
                    task_status = ('limited' if limited else 'done') if state['status'] == 'ok' else state['status']
                    library.db.execute('UPDATE backfill_tasks SET status=?,updated_at=?,coverage=?,error=? WHERE id=?',
                        (task_status, stamp(), 'current_window_only' if limited else 'api_window', '; '.join(state['errors']), task['id']))
                failures = 0 if state['status'] in ('ok', 'continuing', 'needs_review') else (health['failures'] if health else 0) + 1
                pause = min(settings.get('source_failure_max_hours', 24) * 60,
                            settings.get('source_failure_base_minutes', 30) * 2 ** min(max(failures - 1, 0), 10))
                if state['status'] != 'deferred_budget':
                    next_time = max(now() + timedelta(minutes=pause), parse_date(state.get('retry_after')) or now())
                    library.db.execute('INSERT OR REPLACE INTO source_health VALUES(?,?,?,?,?,?,?,?)',
                        (source['id'], fingerprint if state['status'] in ('ok', 'needs_review') and not task and (args.days is None or last is None)
                         else (health['config_hash'] if health else ''),
                         stamp(), state['status'], failures, stamp(next_time) if failures else '',
                         '; '.join(state['errors'])[:1500], fingerprint))
                library.db.commit()
                info['sources'].append(state)
                LOG.info('Source %s: %s, fetched=%s accepted=%s new=%s', source['id'], state['status'], state['fetched'], state['accepted'], state['new_records'])
            maximum = args.max_downloads if args.max_downloads is not None else settings.get('max_downloads_per_run', 30)
            client.deadline = total_deadline
            client.raw_observer = None
            if not args.metadata_only and maximum:
                pipeline.downloads(library, client, sources, settings, maximum, info, sys.modules[__name__])
            info['pipeline'] = pipeline.metrics(library, config, sources, sys.modules[__name__])
            info['finished'] = stamp()
            metrics = getattr(client, 'metrics', {})
            info['http'] = metrics if isinstance(metrics, dict) else {}
            issues = any(s['status'] != 'ok' for s in info['sources']) or any(info['downloads'].get(k, 0) for k in ('download_failed', 'policy_blocked')) or info['pipeline']['status'] != 'ok'
            info['status'] = 'partial' if issues else 'ok'
            info['pending_downloads'] = library.db.execute("SELECT count(*) FROM records WHERE pdf_status NOT IN ('downloaded','metadata_only')").fetchone()[0]
            info['download_status_counts'] = dict(library.db.execute('SELECT pdf_status,count(*) FROM records GROUP BY pdf_status').fetchall())
            info['metadata_only_records'] = info['download_status_counts'].get('metadata_only', 0)
            info['pdf_files'] = library.db.execute("SELECT count(DISTINCT pdf_path) FROM records WHERE pdf_status='downloaded'").fetchone()[0]
            info['total_documents'] = library.db.execute('SELECT count(*) FROM documents').fetchone()[0]
            info['total_records'] = library.db.execute('SELECT count(*) FROM records').fetchone()[0]
            library.db.execute('UPDATE runs SET finished=?,status=?,details=? WHERE id=?',
                               (info['finished'], info['status'], json.dumps(info, ensure_ascii=False), run_id))
            library.db.commit()
            export_library(library, info, settings.get('report_days', 7))
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 2 if issues else 0
        except BaseException as error:
            info.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                        finished=stamp(), fatal_error=str(error))
            library.db.rollback()
            library.db.execute('UPDATE runs SET finished=?,status=?,details=? WHERE id=?',
                               (info['finished'], info['status'], json.dumps(info, ensure_ascii=False), run_id))
            library.db.commit()
            try:
                export_library(library, info, settings.get('report_days', 7))
            except Exception:
                LOG.exception('Could not export final failure status; inspect SQLite runs')
            raise
        finally:
            library.db.close()
            # Release Windows log-file handles for repeated embedding/test invocations.
            for handler in handlers:
                logging.getLogger().removeHandler(handler)
                handler.close()


def doctor(args, config):
    sources = select_sources(config, args.sources)
    client = Client(config['collector'], sources, time.monotonic() + 600)
    results = []
    for source in sources:
        try:
            papers, truncated = fetch_source(client, source, now() - timedelta(days=14), max_pages=1)
            matches = sum(Matcher(config['filter']).match(p, source) for p in papers)
            result = dict(source=source['id'], status='ok', parsed=len(papers), matched=matches,
                          sample_limited=truncated, sample=[p.title for p in papers[:2]])
        except Exception as error:
            result = dict(source=source['id'], status='failed', error=str(error))
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 2 if any(r['status'] != 'ok' for r in results) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('run', 'doctor', 'report', 'status', 'verify', 'retry', 'backup', 'restore', 'reclassify', 'sources',
                                           'download', 'backfill', 'jobs', 'candidates', 'rescreen', 'quarantine', 'replay', 'resolve', 'metrics', 'search', 'export'))
    parser.add_argument('--config', default=str(BASE / 'config.toml'))
    parser.add_argument('--data-dir', help='Separate library directory, e.g. data-smoke')
    parser.add_argument('--sources', help='Comma-separated ids; explicitly selecting an optional source enables it for this invocation')
    parser.add_argument('--days', type=int, help='Run: override discovery lookback; report: recent first-seen window')
    parser.add_argument('--metadata-only', action='store_true')
    parser.add_argument('--max-downloads', type=int)
    parser.add_argument('--force-sources', action='store_true', help='Ignore source failure cooldown for this run')
    parser.add_argument('--record-id', type=int, action='append', help='retry: select an undownloaded record; repeatable')
    parser.add_argument('--repair', action='store_true', help='verify: requeue missing/corrupt PDFs')
    parser.add_argument('--output', help='backup/export: destination file, must not already exist')
    parser.add_argument('--archive', help='restore: previously created backup ZIP')
    parser.add_argument('--from-date', help='UTC inclusive start; backfill/search/export')
    parser.add_argument('--until-date', help='UTC exclusive end for local search/backfill task segmentation; API endpoints may include boundaries')
    parser.add_argument('--chunk-days', type=int, default=7, help='backfill: days per persistent task')
    parser.add_argument('--max-tasks', type=int, default=10, help='backfill: tasks attempted per invocation')
    parser.add_argument('--job-id', help='backfill: resume existing job; jobs: filter job')
    parser.add_argument('--item-id', type=int, action='append', help='replay/resolve: quarantined item ID; repeatable')
    parser.add_argument('--reason', help='resolve: explicitly document an accepted unrecovered item gap')
    parser.add_argument('--query', help='search/export: case-insensitive literal substring')
    parser.add_argument('--author')
    parser.add_argument('--topic')
    parser.add_argument('--doi')
    parser.add_argument('--pdf-status', choices=('pending','metadata_only','downloaded','no_public_pdf','download_failed','policy_blocked'))
    parser.add_argument('--date-field', choices=('published','updated','first_seen'), default='published')
    parser.add_argument('--all-versions', action='store_true', help='search/export: return every matching source/version, not one per document')
    parser.add_argument('--limit', type=int, help='search defaults to 50; export defaults to all matches')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--format', choices=('json','csv','bibtex','ris'), default='json')
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error('--days must be positive')
    if args.max_downloads is not None and args.max_downloads < 0:
        parser.error('--max-downloads cannot be negative')
    if args.record_id and (args.command != 'retry' or any(i < 1 for i in args.record_id)):
        parser.error('--record-id requires retry and positive record ids')
    if args.repair and args.command != 'verify':
        parser.error('--repair is only supported by verify')
    if args.archive and args.command != 'restore':
        parser.error('--archive is only supported by restore')
    if args.output and args.command not in ('backup', 'export'):
        parser.error('--output is only supported by backup/export')
    if args.command == 'download' and args.metadata_only:
        parser.error('download cannot be combined with --metadata-only')
    if args.chunk_days < 1 or args.max_tasks < 1 or args.offset < 0 or (args.limit is not None and args.limit < 1):
        parser.error('chunk-days/max-tasks/limit must be positive; offset cannot be negative')
    if args.item_id and (args.command not in ('replay', 'resolve') or any(i < 1 for i in args.item_id)):
        parser.error('--item-id requires replay/resolve and positive IDs')
    if args.reason and args.command != 'resolve':
        parser.error('--reason requires resolve')
    for value in (args.from_date, args.until_date):
        if value and not parse_date(value):
            parser.error('Dates must be valid ISO dates/timestamps (UTC when no timezone supplied)')
    if args.from_date and args.until_date and parse_date(args.from_date) >= parse_date(args.until_date):
        parser.error('--from-date must be earlier than --until-date')
    if (args.from_date or args.until_date) and args.command not in ('backfill', 'search', 'export'):
        parser.error('Date boundaries require backfill/search/export')
    if args.command == 'backfill' and args.days is not None:
        parser.error('backfill uses fixed date ranges, not --days')
    if args.command == 'backfill' and args.job_id and (args.from_date or args.until_date):
        parser.error('Resume a job with --job-id without new date boundaries')
    if args.job_id and args.command not in ('backfill', 'jobs'):
        parser.error('--job-id requires backfill/jobs')
    if any((args.query, args.author, args.topic, args.doi, args.pdf_status, args.all_versions,
            args.date_field != 'published')) and args.command not in ('search', 'export'):
        parser.error('Search filters require search/export')
    if (args.limit is not None or args.offset) and args.command not in ('search', 'export', 'candidates', 'quarantine'):
        parser.error('--limit/--offset require search/export/candidates/quarantine')
    config, config_root = load_config(args.config)
    if args.command == 'backfill':
        import pipeline
        return pipeline.backfill(args, config, config_root, sys.modules[__name__])
    if args.command in ('jobs', 'candidates', 'rescreen', 'quarantine', 'replay', 'resolve', 'metrics', 'search', 'export'):
        import pipeline
        return pipeline.execute(args, config, config_root, sys.modules[__name__])
    if args.command == 'sources':
        import adapters
        selected = select_sources(config, args.sources) if args.sources else config['sources']
        print(json.dumps([dict(source=s['id'], kind=s['kind'], url=s['url'], enabled=s.get('enabled', True),
                               download=s.get('download', True), notes=s.get('notes', ''),
                               coverage=adapters.REGISTRY[s['kind']].coverage,
                               incremental_axis=adapters.REGISTRY[s['kind']].incremental_axis,
                               resumable=adapters.REGISTRY[s['kind']].resumable) for s in selected], ensure_ascii=False, indent=2))
        return 0
    if args.command in ('status', 'verify', 'retry', 'backup', 'restore', 'reclassify'):
        import maintenance
        return maintenance.execute(args, config, config_root, sys.modules[__name__])
    if args.command == 'doctor':
        return doctor(args, config)
    if args.command == 'report':
        root = Path(args.data_dir or config_root / config['collector'].get('data_dir', 'data')).resolve()
        with run_lock(root):
            library = Library(root)
            try:
                previous_status = root / 'last_run.json'
                info = json.loads(previous_status.read_text(encoding='utf-8')) if previous_status.exists() else None
                export_library(library, info, args.days or 7, save_run=False)
                # report does not overwrite the previous run's status or historical report.
                print(str(root / 'index.html'))
            finally:
                library.db.close()
        return 0
    return collect(args, config, config_root)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('Interrupted; committed metadata and completed PDFs are retained.', file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
