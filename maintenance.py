"""Offline library operations; standard library only, no network calls."""
from datetime import timedelta
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import zipfile


def digest_file(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def source_hash(config, source):
    discovery = {k: v for k, v in source.items() if k not in ('enabled', 'download')}
    return hashlib.sha256(json.dumps(dict(source=discovery, filter=config['filter']),
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_config(config):
    settings = config.get('collector', {})
    positive = ('initial_days', 'report_days', 'timeout_seconds', 'http_attempts', 'max_run_minutes',
                'max_download_attempts', 'max_pdf_mb', 'log_max_mb', 'log_backups', 'source_failure_base_minutes',
                'source_failure_max_hours', 'stale_source_hours', 'retry_max_hours', 'candidate_retention_days',
                'candidate_max_mb', 'raw_response_max_mb', 'source_max_seconds', 'backlog_alert_days', 'discovery_budget_fraction')
    nonnegative = ('overlap_days', 'request_interval_seconds', 'max_downloads_per_run', 'retry_hours', 'no_pdf_retry_hours', 'download_max_per_source')
    known = set(positive + nonnegative) | {'data_dir', 'user_agent', 'ca_file', 'retain_candidates', 'retain_raw_responses'}
    unknown = set(settings) - known
    if unknown:
        raise ValueError('Unknown collector setting(s): ' + ', '.join(sorted(unknown)))
    for key in positive + nonnegative:
        if key in settings:
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (key in positive and value == 0):
                raise ValueError('Invalid collector setting: ' + key)
    if settings.get('discovery_budget_fraction', 0.65) > 1:
        raise ValueError('discovery_budget_fraction must be <= 1')
    for key in ('retain_candidates', 'retain_raw_responses'):
        if key in settings and not isinstance(settings[key], bool):
            raise ValueError('Setting must be boolean: ' + key)
    for key in ('http_attempts', 'max_download_attempts', 'log_backups', 'max_downloads_per_run', 'download_max_per_source'):
        if key in settings and not isinstance(settings[key], int):
            raise ValueError('Setting must be an integer: ' + key)
    filtering = config.get('filter', {})
    if 'minimum_score' in filtering and (not isinstance(filtering['minimum_score'], int) or filtering['minimum_score'] < 0):
        raise ValueError('filter.minimum_score must be a nonnegative integer')
    if not isinstance(filtering.get('exclude_terms', []), list) or any(
            not isinstance(t, str) or not t.strip() for t in filtering.get('exclude_terms', [])):
        raise ValueError('exclude_terms must be nonempty strings')
    if 'topics' in filtering:
        if not isinstance(filtering['topics'], dict) or not filtering['topics']:
            raise ValueError('filter.topics must be a nonempty table')
        for topic, terms in filtering['topics'].items():
            if not topic or not isinstance(terms, list) or not terms or any(not isinstance(t, str) or not t.strip() for t in terms):
                raise ValueError('Each topic requires nonempty string keywords')
    for source in config.get('sources', []):
        import adapters
        kind = source.get('kind')
        if kind not in adapters.REGISTRY:
            raise ValueError('Unsupported source kind: ' + str(kind))
        source_keys = {'id', 'name', 'kind', 'url', 'allowed_domains', 'enabled', 'download', 'minimum_score',
                       'resolve_pdf', 'page_size', 'max_pages', 'notes'} | set(adapters.REGISTRY[kind].options)
        if set(source) - source_keys:
            raise ValueError('Unknown source setting(s): ' + ', '.join(sorted(set(source) - source_keys)))
        for key in ('page_size', 'max_pages'):
            if key in source and (not isinstance(source[key], int) or isinstance(source[key], bool) or source[key] < 1):
                raise ValueError('Invalid source setting: ' + key)
        if source['kind'] == 'arxiv' and not source.get('query', '').strip():
            raise ValueError('arxiv source requires query')
        if kind == 'crossref' and not (source.get('query', '').strip() or source.get('issn', '').strip()):
            raise ValueError('crossref requires query or issn to avoid unbounded harvesting')
        if kind == 'crossref' and source.get('issn'):
            import re
            if not re.fullmatch(r'\d{4}-[\dXx]{4}', source['issn']):
                raise ValueError('Invalid ISSN')
        if kind == 'datacite' and not source.get('query', '').strip():
            raise ValueError('datacite requires query')
        if kind == 'datacite' and ('resource_types' in source and (not isinstance(source['resource_types'], list) or not source['resource_types'] or any(not isinstance(v, str) or not v for v in source['resource_types']))):
            raise ValueError('resource_types must be a nonempty string array')
        if kind == 'oai_pmh' and source.get('metadata_prefix', 'oai_dc') != 'oai_dc':
            raise ValueError('Only oai_dc metadata is supported by this adapter')
        if kind == 'oai_pmh' and source.get('date_granularity', 'seconds') not in ('seconds', 'days'):
            raise ValueError('date_granularity must be seconds or days; check repository Identify')
        if kind in ('crossref', 'datacite') and source.get('page_size', 100) > 1000:
            raise ValueError('API page_size cannot exceed 1000')
        if not isinstance(source.get('allowed_domains'), list) or any(
                not isinstance(d, str) or not d or '/' in d or ':' in d or d.startswith('.') for d in source['allowed_domains']):
            raise ValueError('allowed_domains must be domain names')
        from urllib.parse import urlsplit
        host = urlsplit(source['url']).hostname or ''
        if adapters.REGISTRY[kind].resumable and urlsplit(source['url']).query:
            raise ValueError('Paged adapter endpoint must not contain a query string')
        if not any(host == d or host.endswith('.' + d) for d in source['allowed_domains']):
            raise ValueError('Source URL is outside its allowed_domains: ' + source['id'])


def integrity(library, repair=False):
    errors, invalid_ids = [], []
    db = library.db
    if [r[0] for r in db.execute('PRAGMA integrity_check')] != ['ok']:
        errors.append('SQLite integrity_check failed')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        errors.append('SQLite foreign key violation')
    rows = db.execute("SELECT id,pdf_path,sha256 FROM records WHERE pdf_status='downloaded'").fetchall()
    checked = {}
    for row in rows:
        key = row['pdf_path'], row['sha256']
        if key not in checked:
            path = (library.root / row['pdf_path']).resolve()
            try:
                if not row['pdf_path'] or not path.is_relative_to(library.root.resolve()):
                    raise ValueError('PDF path escapes library')
                with path.open('rb') as handle:
                    if not handle.read(1024).lstrip().startswith(b'%PDF-'):
                        raise ValueError('Invalid PDF header')
                if digest_file(path) != row['sha256']:
                    raise ValueError('SHA-256 mismatch')
                checked[key] = ''
            except (OSError, ValueError) as error:
                checked[key] = str(error)
        if checked[key]:
            errors.append(dict(record_id=row['id'], path=row['pdf_path'], error=checked[key]))
            invalid_ids.append(row['id'])
    if repair and invalid_ids:
        with db:
            for rid in invalid_ids:
                db.execute("UPDATE records SET pdf_status='pending',pdf_path='',sha256='',attempts=0,last_attempt='',next_retry='',error='Integrity check failed; queued for recovery' WHERE id=?", (rid,))
    return dict(status='failed' if errors else 'ok', pdf_files=len(checked), errors=errors,
                repaired_records=len(invalid_ids) if repair else 0)


def status(library, config, sources, api):
    settings = config['collector']
    items = []
    unhealthy = False
    for source in sources:
        row = library.db.execute('SELECT * FROM source_health WHERE source=?', (source['id'],)).fetchone()
        last = library.success(source['id'])
        stale = last is None or api.now() - last > timedelta(hours=settings.get('stale_source_hours', 72))
        changed = row is None or row['config_hash'] != source_hash(config, source)
        bad = stale or changed or bool(row and row['failures'])
        unhealthy |= bad
        items.append({**(dict(row) if row else {}), 'source': source['id'],
                      'last_success': api.stamp(last) if last else None,
                      'stale': stale, 'configuration_changed': changed})
    states = dict(library.db.execute('SELECT pdf_status,count(*) FROM records GROUP BY pdf_status').fetchall())
    exhausted = library.db.execute("SELECT count(*) FROM records WHERE pdf_status!='downloaded' AND attempts>=?",
                                   (settings.get('max_download_attempts', 8),)).fetchone()[0]
    latest = library.db.execute('SELECT id,started,finished,status FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
    if latest and latest['status'] != 'ok':
        unhealthy = True
    import pipeline
    workflow = pipeline.metrics(library, config, sources, api)
    return dict(status='attention' if unhealthy or exhausted or workflow['status'] != 'ok' else 'ok', sources=items,
                pipeline=workflow,
                latest_run=dict(latest) if latest else None, download_states=states, exhausted_records=exhausted,
                checkpoints=[dict(r) for r in library.db.execute('SELECT source,mode,window_since,window_until FROM harvest_checkpoints')],
                deletion_notices=library.db.execute("SELECT count(*) FROM source_notices WHERE status='deleted'").fetchone()[0],
                documents=library.db.execute('SELECT count(*) FROM documents').fetchone()[0],
                records=library.db.execute('SELECT count(*) FROM records').fetchone()[0])


def backup(library, destination, config_path, api):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError('Backup destination already exists; choose a new file')
    audit = integrity(library)
    if audit['status'] != 'ok':
        raise ValueError('Refusing incomplete backup: ' + json.dumps(audit, ensure_ascii=False))
    with tempfile.TemporaryDirectory(prefix='backup-', dir=destination.parent) as work:
        work = Path(work)
        snapshot = work / 'library.sqlite3'
        with closing(sqlite3.connect(snapshot)) as target:
            library.db.backup(target)
        members = {'library.sqlite3': snapshot, 'config.snapshot' + Path(config_path).suffix: Path(config_path)}
        expanded, _ = api.load_config(config_path)
        expanded_path = work / 'config.expanded.json'
        expanded_path.write_text(json.dumps(expanded, ensure_ascii=False, indent=2), encoding='utf-8')
        members['config.expanded.json'] = expanded_path
        for name in ('catalog.jsonl', 'index.html', 'latest.md', 'last_run.json'):
            path = library.root / name
            if path.is_file():
                members[name] = path
        for subdir in ('reports', 'config_history', 'maintenance_history'):
            for path in (library.root / subdir).glob('*'):
                if path.is_file():
                    members[path.relative_to(library.root).as_posix()] = path
        for row in library.db.execute("SELECT DISTINCT pdf_path FROM records WHERE pdf_status='downloaded'"):
            members[row[0]] = library.root / row[0]
        manifest = dict(format_version=1, created=api.stamp(), schema_version=api.SCHEMA_VERSION,
                        files={name: dict(sha256=digest_file(path), size=path.stat().st_size) for name, path in members.items()})
        archive = work / 'archive.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            for name, path in members.items():
                output.write(path, name)
            output.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.rename(destination)
    return dict(status='ok', archive=str(destination), files=len(members), sha256=digest_file(destination))


def restore(archive, destination, api):
    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Restore destination must not exist; existing library is never overwritten')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        names = source.namelist()
        if len(set(names)) != len(names):
            raise ValueError('Duplicate archive entries')
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or not path.parts:
                raise ValueError('Unsafe archive path')
        manifest = json.loads(source.read('manifest.json'))
        if manifest.get('format_version') != 1 or manifest.get('schema_version', 999) > api.SCHEMA_VERSION:
            raise ValueError('Unsupported backup version')
        if set(names) != set(manifest['files']) | {'manifest.json'} or 'library.sqlite3' not in manifest['files']:
            raise ValueError('Manifest file set mismatch')
        with tempfile.TemporaryDirectory(prefix='restore-', dir=destination.parent) as staging:
            root = Path(staging) / 'library'
            root.mkdir()
            for name, expected in manifest['files'].items():
                if source.getinfo(name).file_size != expected['size']:
                    raise ValueError('Archive size mismatch: ' + name)
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with source.open(name) as incoming, path.open('xb') as output:
                    for block in iter(lambda: incoming.read(1024 * 1024), b''):
                        output.write(block)
                        digest.update(block)
                if digest.hexdigest() != expected['sha256']:
                    raise ValueError('Archive checksum mismatch: ' + name)
            lib = api.Library(root)
            try:
                result = integrity(lib)
                if result['status'] != 'ok':
                    raise ValueError('Restored database/PDF verification failed')
            finally:
                lib.db.close()
            if destination.exists():
                raise ValueError('Restore destination appeared during extraction')
            root.rename(destination)
    return dict(status='ok', restored_to=str(destination), pdf_files=result['pdf_files'])


def execute(args, config, config_root, api):
    root = Path(args.data_dir or config_root / config['collector'].get('data_dir', 'data')).resolve()
    if args.command == 'restore':
        if not args.archive or not args.data_dir:
            raise ValueError('restore requires --archive and an explicit new --data-dir')
        result = restore(args.archive, root, api)
    else:
        if not (root / 'library.sqlite3').is_file():
            raise ValueError('Library not found: run collection first or specify --data-dir')
        with api.run_lock(root):
            lib = api.Library(root)
            try:
                sources = api.select_sources(config, args.sources)
                if args.command == 'status':
                    result = status(lib, config, sources, api)
                elif args.command == 'verify':
                    result = integrity(lib, args.repair)
                    if args.repair:
                        previous = lib.root / 'last_run.json'
                        info = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else None
                        api.export_library(lib, info, config['collector'].get('report_days', 7), save_run=False)
                elif args.command == 'backup':
                    target = args.output or root / 'backups' / ('library-' + api.now().strftime('%Y%m%dT%H%M%S%f') + '.zip')
                    result = backup(lib, target, args.config, api)
                elif args.command == 'reclassify':
                    selected = {s['id']: s for s in sources}
                    matcher = api.Matcher(config['filter'])
                    matched, unmatched = 0, 0
                    with lib.db:
                        for row in lib.db.execute('SELECT id,source,metadata FROM records').fetchall():
                            if row['source'] not in selected:
                                continue
                            paper = api.Paper(**json.loads(row['metadata']))
                            good = matcher.match(paper, selected[row['source']])
                            matched += int(good)
                            unmatched += int(not good)
                            lib.db.execute('UPDATE records SET metadata=? WHERE id=?',
                                           (json.dumps(api.asdict(paper), ensure_ascii=False), row['id']))
                    previous = lib.root / 'last_run.json'
                    info = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else None
                    api.export_library(lib, info, config['collector'].get('report_days', 7), save_run=False)
                    result = dict(status='ok', matched=matched, unmatched=unmatched, network_requests=0,
                                  note='All records and PDFs retained; rejected historical feed entries were never stored')
                elif args.command == 'retry':
                    if not args.record_id and not args.sources:
                        raise ValueError('retry requires --record-id or --sources to select its scope')
                    selected = {s['id'] for s in sources}
                    rows = lib.db.execute("SELECT id,source FROM records WHERE pdf_status!='downloaded'").fetchall()
                    ids = [r['id'] for r in rows if r['source'] in selected and (not args.record_id or r['id'] in args.record_id)]
                    with lib.db:
                        for rid in ids:
                            lib.db.execute("UPDATE records SET pdf_status='pending',attempts=0,last_attempt='',next_retry='',error='Manually queued for retry' WHERE id=?", (rid,))
                    previous = lib.root / 'last_run.json'
                    info = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else None
                    api.export_library(lib, info, config['collector'].get('report_days', 7), save_run=False)
                    result = dict(status='ok', queued_records=ids, network_requests=0)
                else:
                    raise ValueError('Unknown maintenance command')
                if args.command in ('retry', 'reclassify') or (args.command == 'verify' and args.repair):
                    history = root / 'maintenance_history' / (api.now().strftime('%Y%m%dT%H%M%S%f') + '-' + args.command + '.json')
                    api.atomic_text(history, json.dumps(dict(command=args.command, result=result,
                                    config_hash=hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()),
                                    ensure_ascii=False, indent=2))
            finally:
                lib.db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'ok' else 2
