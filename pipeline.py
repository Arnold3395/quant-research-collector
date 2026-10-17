"""Persistent collection workflow and local discovery tools. Standard library only."""
from collections import deque
from datetime import timedelta
import csv
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import io
import json
from pathlib import Path
import statistics
import time
import unicodedata
import urllib.parse
import uuid


SCHEMA = '''
CREATE TABLE IF NOT EXISTS candidates (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, metadata TEXT NOT NULL, accepted INTEGER NOT NULL,
 reason TEXT NOT NULL, rule_hash TEXT NOT NULL, decisions TEXT NOT NULL,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 UNIQUE(source,source_id,fingerprint)
);
CREATE INDEX IF NOT EXISTS candidates_seen ON candidates(last_seen);
CREATE TABLE IF NOT EXISTS item_errors (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, stage TEXT NOT NULL,
 payload TEXT NOT NULL, fingerprint TEXT NOT NULL, error TEXT NOT NULL,
 run_id TEXT NOT NULL, task_id INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 resolved_at TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
 UNIQUE(source,stage,fingerprint)
);
CREATE TABLE IF NOT EXISTS raw_responses (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL,
 observed_at TEXT NOT NULL, sha256 TEXT NOT NULL, body BLOB NOT NULL,
 UNIQUE(source,url,sha256)
);
CREATE TABLE IF NOT EXISTS pipeline_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS error_tasks (
 error_id INTEGER NOT NULL REFERENCES item_errors(id), task_id INTEGER NOT NULL REFERENCES backfill_tasks(id),
 PRIMARY KEY(error_id,task_id)
);
CREATE TABLE IF NOT EXISTS backfill_tasks (
 id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
 window_since TEXT NOT NULL, window_until TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 config_hash TEXT NOT NULL DEFAULT '', coverage TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 UNIQUE(job_id,source,window_since,window_until)
);
CREATE TABLE IF NOT EXISTS source_metrics (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, source TEXT NOT NULL, mode TEXT NOT NULL,
 config_hash TEXT NOT NULL, observed_at TEXT NOT NULL, status TEXT NOT NULL,
 elapsed_seconds REAL NOT NULL, fetched INTEGER NOT NULL, accepted INTEGER NOT NULL, evaluated INTEGER NOT NULL DEFAULT 0,
 quarantined INTEGER NOT NULL, missing_abstract INTEGER NOT NULL, missing_authors INTEGER NOT NULL,
 UNIQUE(run_id,source)
);
CREATE INDEX IF NOT EXISTS source_metrics_history ON source_metrics(source,mode,config_hash,id);
CREATE TABLE IF NOT EXISTS checkpoint_tracking (
 source TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(source,mode)
);
'''


def rule_hash(config, source):
    payload = dict(filter=config['filter'], minimum_score=source.get('minimum_score'))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def decision(paper, source, matcher):
    if any(matcher.contains(unicodedata.normalize('NFKC', paper.title).casefold(), t) for t in matcher.exclude):
        return 'excluded_title'
    return 'accepted' if paper.rule_match else 'below_threshold'


def remember(lib, paper, source, config, api, reason=None):
    """Retain bounded normalized metadata, not an unbounded platform mirror."""
    if not config['collector'].get('retain_candidates', True):
        return
    metadata = json.dumps(api.asdict(paper), ensure_ascii=False)
    reason = reason or decision(paper, source, api.Matcher(config['filter']))
    digest, timestamp = rule_hash(config, source), api.stamp()
    old = lib.db.execute('SELECT id,decisions FROM candidates WHERE source=? AND source_id=? AND fingerprint=?',
                         (source['id'], paper.source_id, paper.fingerprint())).fetchone()
    history = json.loads(old['decisions']) if old else []
    event = dict(rule_hash=digest, accepted=paper.rule_match, reason=reason, score=paper.score)
    if not history or any(history[-1].get(k) != v for k, v in event.items()):
        history.append(dict(event, observed_at=timestamp))
    history = history[-20:]
    lib.db.execute('''INSERT INTO candidates(source,source_id,fingerprint,metadata,accepted,reason,rule_hash,decisions,first_seen,last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,source_id,fingerprint) DO UPDATE SET
        metadata=excluded.metadata,accepted=excluded.accepted,reason=excluded.reason,rule_hash=excluded.rule_hash,
        decisions=excluded.decisions,last_seen=excluded.last_seen''',
        (source['id'], paper.source_id, paper.fingerprint(), metadata, int(paper.rule_match), reason,
         digest, json.dumps(history, ensure_ascii=False), timestamp, timestamp))


def prune(lib, settings, api):
    """Logical payload quotas; SQLite reuses freed pages, does not shrink the file."""
    timestamp = api.stamp(api.now() - timedelta(days=settings.get('candidate_retention_days', 90)))
    lib.db.execute('DELETE FROM candidates WHERE last_seen<?', (timestamp,))
    for table, expression, order, budget in (
            ('candidates', 'length(CAST(metadata AS BLOB))+length(CAST(decisions AS BLOB))+512',
             'last_seen DESC,id DESC', settings.get('candidate_max_mb', 100)),
            ('raw_responses', 'length(body)+length(CAST(url AS BLOB))+256',
             'observed_at DESC,id DESC', settings.get('raw_response_max_mb', 50))):
        if table == 'raw_responses':
            lib.db.execute('DELETE FROM raw_responses WHERE observed_at<?', (timestamp,))
        used, discard = 0, []
        for row in lib.db.execute(f'SELECT id,{expression} AS size FROM {table} ORDER BY {order}'):
            used += row['size']
            if used > budget * 1024 * 1024:
                discard.append((row['id'],))
        lib.db.executemany(f'DELETE FROM {table} WHERE id=?', discard)
    lib.db.commit()


def capture(lib, source, url, body, settings, api):
    if not settings.get('retain_raw_responses', False):
        return
    packed = gzip.compress(body, mtime=0)
    if len(packed) + len(url.encode()) + 256 > settings.get('raw_response_max_mb', 50) * 1024 * 1024:
        return
    lib.db.execute('''INSERT INTO raw_responses(source,url,observed_at,sha256,body) VALUES(?,?,?,?,?)
        ON CONFLICT(source,url,sha256) DO UPDATE SET observed_at=excluded.observed_at''',
        (source, url, api.stamp(), hashlib.sha256(body).hexdigest(), packed))
    prune(lib, settings, api)


def quarantine(lib, source, stage, payload, error, run_id, task_id, api):
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
    timestamp = api.stamp()
    lib.db.execute('''INSERT INTO item_errors(source,stage,payload,fingerprint,error,run_id,task_id,first_seen,last_seen)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source,stage,fingerprint) DO UPDATE SET
        error=excluded.error,last_seen=excluded.last_seen,resolved_at='',run_id=excluded.run_id,
        task_id=coalesce(excluded.task_id,item_errors.task_id)''',
        (source, stage, serialized, fingerprint, str(error)[:4000], run_id, task_id, timestamp, timestamp))
    if task_id is not None:
        row = lib.db.execute('SELECT id FROM item_errors WHERE source=? AND stage=? AND fingerprint=?',
                             (source, stage, fingerprint)).fetchone()
        lib.db.execute('INSERT OR IGNORE INTO error_tasks VALUES(?,?)', (row[0], task_id))
    lib.db.commit()


def unresolved(lib, source, task_id=None):
    if task_id is None:
        return lib.db.execute("SELECT 1 FROM item_errors WHERE source=? AND resolved_at=''", (source,)).fetchone() is not None
    return lib.db.execute("SELECT 1 FROM item_errors e JOIN error_tasks t ON t.error_id=e.id WHERE t.task_id=? AND e.resolved_at=''",
                           (task_id,)).fetchone() is not None


def apply_notice(lib, source, notice, api):
    lib.db.execute('INSERT OR IGNORE INTO source_notices VALUES(?,?,?,?,?)',
                   (source, notice['source_id'], notice['datestamp'], 'deleted', api.stamp()))
    for table in ('records', 'candidates'):
        for row in lib.db.execute(f'SELECT id,metadata FROM {table} WHERE source=? AND source_id=?',
                                  (source, notice['source_id'])).fetchall():
            metadata = json.loads(row['metadata'])
            metadata['source_deleted'] = True
            lib.db.execute(f'UPDATE {table} SET metadata=? WHERE id=?', (json.dumps(metadata, ensure_ascii=False), row['id']))


def validate_paper(paper, source, api):
    for field in ('source', 'source_id', 'title', 'url', 'abstract', 'published', 'updated', 'pdf_url',
                  'doi', 'arxiv', 'version', 'license_url', 'resource_type'):
        if not isinstance(getattr(paper, field), str):
            raise ValueError('Paper field must be a string: ' + field)
    for field in ('authors', 'categories'):
        value = getattr(paper, field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError('Paper field must be a string list: ' + field)
    if not paper.title.strip() or not paper.source_id.strip() or paper.source != source['id']:
        raise ValueError('Missing title/source id or mismatched source')
    api.canonical_url(paper.url)


def ingest(lib, paper, source, config, api):
    """Isolate identity/validation failures without partially inserting a document."""
    validate_paper(paper, source, api)
    lib.db.execute('SAVEPOINT ingest_item')
    try:
        result = lib.upsert(paper, commit=False)
        if not source.get('download', True):
            lib.db.execute("UPDATE records SET pdf_status='metadata_only' WHERE id=? AND pdf_status='pending'", (result[0],))
        lib.db.execute('RELEASE ingest_item')
        lib.db.commit()
        return result
    except Exception:
        lib.db.execute('ROLLBACK TO ingest_item')
        lib.db.execute('RELEASE ingest_item')
        raise


def state_value(lib, key, value=None):
    if value is not None:
        lib.db.execute('INSERT OR REPLACE INTO pipeline_state VALUES(?,?)', (key, str(value)))
        lib.db.commit()
    row = lib.db.execute('SELECT value FROM pipeline_state WHERE key=?', (key,)).fetchone()
    return row[0] if row else ''


def rotate(items, last, key=lambda x: x['id']):
    for index, item in enumerate(items):
        if key(item) == last:
            return items[index + 1:] + items[:index + 1]
    return items


def host_waits(lib, api):
    return {row['key'][11:]: row['value'] for row in lib.db.execute("SELECT * FROM pipeline_state WHERE key LIKE 'host_retry:%'")
            if api.parse_date(row['value']) and api.parse_date(row['value']) > api.now()}


def downloads(lib, client, sources, settings, maximum, info, api):
    """Round-robin eligible queues, independent of metadata discovery outcomes."""
    by_id = {s['id']: s for s in sources if s.get('download', True)}
    queues = {sid: deque() for sid in by_id}
    waiting = host_waits(lib, api)
    for row in lib.db.execute("SELECT * FROM records WHERE pdf_status NOT IN ('downloaded','metadata_only') ORDER BY attempts,first_seen,id"):
        if row['source'] not in by_id or row['attempts'] >= settings.get('max_download_attempts', 8):
            continue
        metadata = json.loads(row['metadata'])
        if urllib.parse.urlsplit(row['pdf_url'] or metadata['url']).hostname in waiting:
            continue
        if not metadata.get('rule_match', True) or metadata.get('source_deleted', False):
            continue
        due = api.parse_date(row['next_retry'])
        previous = api.parse_date(row['last_attempt'])
        delay = (settings.get('no_pdf_retry_hours', 168) if row['pdf_status'] == 'no_public_pdf' else
                 min(settings.get('retry_max_hours', 168), settings.get('retry_hours', 24) * 2 ** min(max(row['attempts'] - 1, 0), 10)))
        if (due and api.now() < due) or (previous and api.now() - previous < timedelta(hours=delay)):
            continue
        queues[row['source']].append(row)
    order = rotate(list(by_id.values()), state_value(lib, 'last_download_source'))
    counts, total = {}, 0
    cap = settings.get('download_max_per_source', 10)
    while total < maximum:
        progressed = False
        for source in order:
            sid = source['id']
            if total >= maximum:
                break
            if not queues[sid] or (cap and counts.get(sid, 0) >= cap):
                continue
            row = queues[sid].popleft()
            host = urllib.parse.urlsplit(row['pdf_url'] or json.loads(row['metadata'])['url']).hostname
            wait_until = api.parse_date(getattr(client, 'host_waits', {}).get(host, '') or
                                        state_value(lib, 'host_retry:' + host))
            if wait_until and wait_until > api.now():
                continue
            client.raw_observer = None
            result = lib.download(client, row, source, settings)
            state_value(lib, 'last_download_source', sid)
            counts[sid] = counts.get(sid, 0) + 1
            total += 1
            progressed = True
            info['downloads'][result] = info['downloads'].get(result, 0) + 1
            per_source = info.setdefault('downloads_by_source', {}).setdefault(sid, {})
            per_source[result] = per_source.get(result, 0) + 1
            if result == 'deferred_budget':
                info['download_deferred_reason'] = 'run_time_budget'
                return
        if not progressed:
            return


def record_metrics(lib, state, papers, run_id, mode, config_hash, started, api):
    lib.db.execute('INSERT OR REPLACE INTO source_metrics(run_id,source,mode,config_hash,observed_at,status,elapsed_seconds,fetched,accepted,evaluated,quarantined,missing_abstract,missing_authors) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (run_id, state['source'], mode, config_hash, api.stamp(), state['status'],
         round(time.monotonic() - started, 3), state['fetched'], state['accepted'], state.get('evaluated', state['fetched']), state.get('quarantined', 0),
         sum(not p.abstract for p in papers), sum(not p.authors for p in papers)))
    lib.db.commit()


def metrics(lib, config, sources, api):
    alerts, summaries = [], []
    for source in sources:
        # Compare only completed normal scans with identical discovery/rule configuration.
        from maintenance import source_hash
        rows = lib.db.execute("SELECT * FROM source_metrics WHERE source=? AND mode='normal' AND config_hash=? AND status IN ('ok','needs_review') ORDER BY id DESC LIMIT 8",
                              (source['id'], source_hash(config, source))).fetchall()
        latest = lib.db.execute('SELECT * FROM source_metrics WHERE source=? ORDER BY id DESC LIMIT 1', (source['id'],)).fetchone()
        events = dict(lib.db.execute('''SELECT e.result,count(*) FROM download_events e JOIN records r ON r.id=e.record_id
            WHERE r.source=? AND e.timestamp>=? GROUP BY e.result''',
            (source['id'], api.stamp(api.now() - timedelta(days=30)))).fetchall())
        attempts = sum(v for k, v in events.items() if k != 'deferred_budget')
        successes = sum(events.get(k, 0) for k in ('downloaded', 'reused_pdf', 'already_downloaded'))
        summaries.append(dict(source=source['id'], latest=dict(latest) if latest else None,
                              download_results_30d=events, download_success_rate_30d=successes / attempts if attempts else None))
        if len(rows) >= 5:
            baseline = rows[2:]
            typical = statistics.median(r['fetched'] for r in baseline)
            if typical >= 5 and all(r['fetched'] < typical * 0.2 for r in rows[:2]):
                alerts.append(dict(source=source['id'], kind='volume_drop', baseline=typical))
            rate = statistics.median(r['accepted'] / max(r['evaluated'], 1) for r in baseline)
            if rate >= 0.2 and all(r['evaluated'] >= 5 and r['accepted'] / r['evaluated'] < rate * 0.2 for r in rows[:2]):
                alerts.append(dict(source=source['id'], kind='acceptance_drop', baseline=rate))
        if len(rows) >= 5:
            for field in ('missing_abstract', 'missing_authors'):
                usual_missing = statistics.median(r[field] / max(r['fetched'], 1) for r in rows[2:])
                if usual_missing < 0.5 and all(r['fetched'] >= 5 and r[field] / r['fetched'] >= 0.9 for r in rows[:2]):
                    alerts.append(dict(source=source['id'], kind=field))
    selected = {s['id'] for s in sources}
    download_sources = {s['id'] for s in sources if s.get('download', True)}
    oldest, ready = None, 0
    waiting = host_waits(lib, api)
    for row in lib.db.execute("SELECT source,first_seen,metadata,attempts,next_retry,last_attempt,pdf_status,pdf_url FROM records WHERE pdf_status NOT IN ('downloaded','metadata_only')"):
        if row['source'] not in download_sources:
            continue
        p = json.loads(row['metadata'])
        if not p.get('rule_match', True) or p.get('source_deleted'):
            continue
        oldest = min(oldest or row['first_seen'], row['first_seen'])
        due, previous = api.parse_date(row['next_retry']), api.parse_date(row['last_attempt'])
        delay = (config['collector'].get('no_pdf_retry_hours', 168) if row['pdf_status'] == 'no_public_pdf' else
                 min(config['collector'].get('retry_max_hours', 168), config['collector'].get('retry_hours', 24) * 2 ** min(max(row['attempts'] - 1, 0), 10)))
        ready += int(urllib.parse.urlsplit(row['pdf_url'] or p['url']).hostname not in waiting and
                     row['attempts'] < config['collector'].get('max_download_attempts', 8) and
                     (not due or due <= api.now()) and (not previous or api.now() - previous >= timedelta(hours=delay)))
    threshold = config['collector'].get('backlog_alert_days', 7)
    if oldest and api.now() - api.parse_date(oldest) > timedelta(days=threshold):
        alerts.append(dict(kind='old_download_backlog', oldest_first_seen=oldest))
    checkpoints = [dict(r) for r in lib.db.execute('SELECT h.*,t.created_at,t.updated_at FROM harvest_checkpoints h LEFT JOIN checkpoint_tracking t USING(source,mode)') if r['source'] in selected]
    for checkpoint in checkpoints:
        if checkpoint['created_at'] and api.now() - api.parse_date(checkpoint['created_at']) > timedelta(days=threshold):
            alerts.append(dict(source=checkpoint['source'], mode=checkpoint['mode'], kind='old_checkpoint'))
    unresolved = [dict(r) for r in lib.db.execute("SELECT source,count(*) AS count FROM item_errors WHERE resolved_at='' GROUP BY source") if r['source'] in selected]
    storage = dict(candidates=lib.db.execute('SELECT count(*) FROM candidates').fetchone()[0],
                   candidate_payload_bytes=lib.db.execute('SELECT coalesce(sum(length(CAST(metadata AS BLOB))+length(CAST(decisions AS BLOB))+512),0) FROM candidates').fetchone()[0],
                   raw_responses=lib.db.execute('SELECT count(*) FROM raw_responses').fetchone()[0],
                   compressed_raw_bytes=lib.db.execute('SELECT coalesce(sum(length(body)),0) FROM raw_responses').fetchone()[0],
                   unresolved_payload_bytes=lib.db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM item_errors WHERE resolved_at=''").fetchone()[0])
    return dict(status='attention' if alerts or unresolved else 'ok', sources=summaries, alerts=alerts,
                checkpoints=checkpoints, unresolved_items=unresolved, ready_downloads=ready,
                oldest_pending_first_seen=oldest, storage=storage, host_cooldowns=waiting)


def backfill(args, config, config_root, api):
    root = Path(args.data_dir or config_root / config['collector'].get('data_dir', 'data')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    selected = (config['sources'] if args.job_id and not args.sources else api.select_sources(config, args.sources))
    with api.run_lock(root):
        lib = api.Library(root)
        try:
            job = args.job_id
            if not job:
                start, end = api.parse_date(args.from_date), api.parse_date(args.until_date)
                if not start or not end or start >= end or end > api.now():
                    raise ValueError('backfill requires --from-date < --until-date <= now (UTC)')
                job = 'bf-' + uuid.uuid4().hex[:12]
                timestamp = api.stamp()
                while start < end:
                    stop = min(end, start + timedelta(days=args.chunk_days))
                    for source in selected:
                        lib.db.execute('INSERT INTO backfill_tasks(job_id,source,window_since,window_until,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                            (job, source['id'], api.stamp(start), api.stamp(stop), timestamp, timestamp))
                    start = stop
                lib.db.commit()
            tasks = [dict(r) for r in lib.db.execute("SELECT * FROM backfill_tasks WHERE job_id=? AND status NOT IN ('done','limited','needs_review') ORDER BY window_since,id", (job,))]
            if not lib.db.execute('SELECT 1 FROM backfill_tasks WHERE job_id=?', (job,)).fetchone():
                raise ValueError('Unknown backfill job: ' + job)
            ids = {s['id'] for s in selected}
            missing = {r['source'] for r in tasks} - {s['id'] for s in config['sources']}
            if missing:
                raise ValueError('Backfill source configuration missing: ' + ', '.join(sorted(missing)))
            tasks = [r for r in tasks if r['source'] in ids]
        finally:
            lib.db.close()
    deadline = time.monotonic() + config['collector'].get('max_run_minutes', 20) * 60
    executed = 0
    from copy import copy
    from contextlib import redirect_stdout
    for task in tasks[:args.max_tasks]:
        if time.monotonic() >= deadline:
            break
        child = copy(args)
        child.sources, child.days, child.metadata_only = task['source'], None, True
        child._task = task
        child._deadline = deadline
        # A backfill invocation emits one machine-readable job result, not interleaved run JSON.
        with redirect_stdout(io.StringIO()):
            api.collect(child, config, config_root)
        executed += 1
    with api.run_lock(root):
        lib = api.Library(root)
        try:
            rows = [dict(r) for r in lib.db.execute('SELECT * FROM backfill_tasks WHERE job_id=? ORDER BY id', (job,))]
        finally:
            lib.db.close()
    finished = all(r['status'] in ('done', 'limited', 'needs_review') for r in rows)
    result = dict(job_id=job, executed_tasks=executed, scan_finished=finished,
                  all_api_windows_done=all(r['status'] == 'done' for r in rows),
                  historical_coverage_guaranteed=False, tasks=rows,
                  note='Task completion covers the available API window, not all historical literature.')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if finished and not any(r['status'] == 'needs_review' for r in rows) else 2


def refresh_tasks(lib, api):
    for row in lib.db.execute("SELECT * FROM backfill_tasks WHERE status='needs_review'").fetchall():
        if not unresolved(lib, row['source'], row['id']):
            waived = lib.db.execute("SELECT 1 FROM error_tasks t JOIN item_errors e ON e.id=t.error_id WHERE t.task_id=? AND e.error LIKE 'Reviewed without recovery:%'", (row['id'],)).fetchone()
            coverage = 'accepted_item_gaps' if waived else row['coverage']
            status = 'limited' if row['coverage'] == 'current_window_only' or waived else 'done'
            lib.db.execute('UPDATE backfill_tasks SET status=?,coverage=?,updated_at=? WHERE id=?', (status, coverage, api.stamp(), row['id']))
    lib.db.commit()


def replay(lib, args, config, sources, api):
    import adapters
    selected = {s['id']: s for s in sources}
    results = []
    rows = lib.db.execute("SELECT * FROM item_errors WHERE resolved_at='' ORDER BY id").fetchall()
    for row in rows:
        if row['source'] not in selected or (args.item_id and row['id'] not in args.item_id):
            continue
        source = selected[row['source']]
        try:
            payload = json.loads(row['payload'])
            papers = ([api.Paper(**payload)] if row['stage'] == 'paper' else
                      adapters.replay_item(row['stage'], payload, source, api))
            for paper in papers:
                if isinstance(paper, dict):
                    apply_notice(lib, source['id'], paper, api)
                    continue
                accepted = api.Matcher(config['filter']).match(paper, source)
                remember(lib, paper, source, config, api)
                if accepted:
                    ingest(lib, paper, source, config, api)
            lib.db.execute('UPDATE item_errors SET resolved_at=?,attempts=attempts+1 WHERE id=?', (api.stamp(), row['id']))
            results.append(dict(id=row['id'], status='resolved'))
        except Exception as error:
            lib.db.rollback()
            lib.db.execute('UPDATE item_errors SET error=?,attempts=attempts+1 WHERE id=?', (str(error)[:4000], row['id']))
            results.append(dict(id=row['id'], status='failed', error=str(error)))
        lib.db.commit()
    refresh_tasks(lib, api)
    prune(lib, config['collector'], api)
    return dict(status='failed' if any(r['status'] == 'failed' for r in results) else 'ok', items=results, network_requests=0)


def resolve(lib, args, sources, api):
    if not args.item_id or not args.reason or not args.reason.strip():
        raise ValueError('resolve requires explicit --item-id and a nonempty --reason')
    selected = {s['id'] for s in sources}
    ids = set(args.item_id)
    rows = [r for r in lib.db.execute('SELECT * FROM item_errors') if r['id'] in ids and r['source'] in selected]
    if {r['id'] for r in rows} != ids:
        raise ValueError('Some item IDs do not exist or are outside the selected sources')
    with lib.db:
        for row in rows:
            if not row['resolved_at']:
                lib.db.execute('UPDATE item_errors SET resolved_at=?,error=? WHERE id=?',
                    (api.stamp(), 'Reviewed without recovery: ' + args.reason.strip() + '; Previous: ' + row['error'], row['id']))
    refresh_tasks(lib, api)
    return dict(status='ok', reviewed_items=sorted(ids), reason=args.reason.strip(), recovered_records=0,
                note='Original payload retained; linked tasks with accepted gaps remain limited.')


def rescreen(lib, config, sources, api):
    selected = {s['id']: s for s in sources}
    accepted, promoted, failed = 0, 0, 0
    # Keep labels on archived records in sync too; never delete records/PDFs.
    for row in lib.db.execute('SELECT id,source,metadata FROM records').fetchall():
        if row['source'] in selected:
            paper = api.Paper(**json.loads(row['metadata']))
            api.Matcher(config['filter']).match(paper, selected[row['source']])
            lib.db.execute('UPDATE records SET metadata=? WHERE id=?', (json.dumps(api.asdict(paper), ensure_ascii=False), row['id']))
    lib.db.commit()
    # Materialize because remember updates these same candidate rows.
    for row in lib.db.execute('SELECT * FROM candidates ORDER BY id').fetchall():
        if row['source'] not in selected:
            continue
        source = selected[row['source']]
        paper = api.Paper(**json.loads(row['metadata']))
        if api.Matcher(config['filter']).match(paper, source):
            accepted += 1
            try:
                _, _, added = ingest(lib, paper, source, config, api)
                promoted += int(added)
            except Exception as error:
                quarantine(lib, source['id'], 'paper', api.asdict(paper), error, 'rescreen', None, api)
                failed += 1
        remember(lib, paper, source, config, api)
    lib.db.commit()
    prune(lib, config['collector'], api)
    return dict(status='attention' if failed else 'ok', accepted=accepted, new_records=promoted,
                quarantined=failed, network_requests=0,
                note='Existing labels updated; only retained candidates can be recovered.')


def folded(value):
    return unicodedata.normalize('NFKC', str(value)).casefold()


def search(lib, args, api):
    result, seen = [], set()
    sources = {s.strip() for s in args.sources.split(',')} if args.sources else None
    for row in lib.db.execute('SELECT * FROM records ORDER BY first_seen DESC,id DESC'):
        p = json.loads(row['metadata'])
        if sources and row['source'] not in sources:
            continue
        if args.pdf_status and row['pdf_status'] != args.pdf_status:
            continue
        if args.doi and api.doi_id(args.doi) != p.get('doi', '').lower():
            continue
        if args.author and not any(folded(args.author) in folded(a) for a in p['authors']):
            continue
        if args.topic and not any(folded(args.topic) in folded(t) for t in p['topics']):
            continue
        haystack = ' '.join([p['title'], p['abstract'], *p['authors'], *p['topics'], p.get('doi', '')])
        if args.query and folded(args.query) not in folded(haystack):
            continue
        value = row['first_seen'] if args.date_field == 'first_seen' else p.get(args.date_field, '')
        date = api.parse_date(value)
        start, end = api.parse_date(args.from_date), api.parse_date(args.until_date)
        if (start and (not date or date < start)) or (end and (not date or date >= end)):
            continue
        if not args.all_versions and row['document_id'] in seen:
            continue
        seen.add(row['document_id'])
        result.append(dict(row, metadata=p))
    total = len(result)
    limit = args.limit if args.limit is not None else (50 if args.command == 'search' else None)
    return total, result[args.offset:args.offset + limit if limit is not None else None]


def citation_text(value):
    return ' '.join(str(value).split())


def bib_escape(value):
    mapping = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}', '%': r'\%', '&': r'\&',
               '_': r'\_', '#': r'\#', '$': r'\$', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
    return ''.join(mapping.get(c, c) for c in citation_text(value))


def citation_year(value):
    import re
    if not isinstance(value, str):
        return ''
    if re.match(r'^\d{4}(?:[-/]|$)', value):
        return value[:4]
    try:
        return str(parsedate_to_datetime(value).year)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return ''


def render(rows, kind):
    if kind == 'json':
        return json.dumps(rows, ensure_ascii=False, indent=2) + '\n'
    if kind == 'csv':
        buffer = io.StringIO(newline='')
        names = ('record_id', 'document_id', 'title', 'authors', 'published', 'doi', 'url', 'source', 'topics', 'abstract', 'pdf_status', 'pdf_path')
        writer = csv.DictWriter(buffer, fieldnames=names)
        writer.writeheader()
        for row in rows:
            p = row['metadata']
            value = dict(record_id=row['id'], document_id=row['document_id'], **{k: p[k] for k in ('title', 'published', 'doi', 'url', 'abstract')},
                         authors='; '.join(p['authors']), topics='; '.join(p['topics']), source=row['source'],
                         pdf_status=row['pdf_status'], pdf_path=row['pdf_path'])
            # CSVs are often opened in spreadsheets; preserve text instead of formulas.
            for key, text in value.items():
                if isinstance(text, str) and text.lstrip().startswith(('=', '+', '-', '@')):
                    value[key] = "'" + text
            writer.writerow(value)
        return buffer.getvalue()
    output = []
    for row in rows:
        p = row['metadata']
        if kind == 'bibtex':
            fields = dict(title=p['title'], author=' and '.join(p['authors']), year=citation_year(p['published']),
                          doi=p['doi'], url=p['url'], abstract=p['abstract'], note='Source: ' + row['source'])
            output.append('@misc{qrc' + str(row['document_id']) + 'r' + str(row['id']) + ',\n' +
                          ',\n'.join('  ' + key + ' = {' + bib_escape(value) + '}' for key, value in fields.items() if value) + '\n}\n')
        else:
            fields = [('TY', 'GEN'), ('ID', f'qrc{row["document_id"]}r{row["id"]}'), ('TI', p['title'])]
            fields.extend(('AU', author) for author in p['authors'])
            fields.extend((('PY', citation_year(p['published'])), ('DO', p['doi']), ('UR', p['url']), ('AB', p['abstract']), ('N1', 'Source: ' + row['source'])))
            output.append('\n'.join(key + '  - ' + citation_text(value) for key, value in fields if value) + '\nER  -\n')
    return '\n'.join(output)


def execute(args, config, config_root, api):
    root = Path(args.data_dir or config_root / config['collector'].get('data_dir', 'data')).resolve()
    if not (root / 'library.sqlite3').is_file():
        raise ValueError('Library not found: run collection first or specify --data-dir')
    with api.run_lock(root):
        lib = api.Library(root)
        try:
            sources = (api.select_sources(config, args.sources) if args.sources or args.command == 'metrics' else config['sources'])
            if args.command in ('search', 'export'):
                total, rows = search(lib, args, api)
                if args.command == 'search':
                    result = dict(status='ok', total=total, offset=args.offset, records=rows)
                else:
                    content = render(rows, args.format)
                    if args.output:
                        destination = Path(args.output).resolve()
                        if destination.exists():
                            raise ValueError('Export destination already exists')
                        api.atomic_text(destination, content)
                        result = dict(status='ok', total_matches=total, exported=len(rows), output=str(destination), format=args.format)
                    else:
                        print(content, end='')
                        return 0
            elif args.command == 'metrics':
                result = metrics(lib, config, sources, api)
            elif args.command == 'jobs':
                result = dict(status='ok', tasks=[dict(r) for r in lib.db.execute('SELECT * FROM backfill_tasks WHERE (? IS NULL OR job_id=?) ORDER BY id', (args.job_id, args.job_id))])
            elif args.command in ('quarantine', 'candidates'):
                table = 'item_errors' if args.command == 'quarantine' else 'candidates'
                ids = [s['id'] for s in sources]
                where = 'source IN (' + ','.join('?' for _ in ids) + ')'
                total = lib.db.execute(f'SELECT count(*) FROM {table} WHERE {where}', ids).fetchone()[0]
                rows = [dict(r) for r in lib.db.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?',
                                                       [*ids, args.limit or 50, args.offset])]
                for row in rows:
                    for field in ('metadata', 'decisions', 'payload'):
                        if field in row:
                            row[field] = json.loads(row[field])
                result = dict(status='ok', total=total, offset=args.offset,
                              **{'items' if table == 'item_errors' else 'candidates': rows})
            elif args.command == 'replay':
                if not args.item_id and not args.sources:
                    raise ValueError('replay requires --item-id or --sources')
                result = replay(lib, args, config, sources, api)
            elif args.command == 'resolve':
                result = resolve(lib, args, sources, api)
            elif args.command == 'rescreen':
                result = rescreen(lib, config, sources, api)
            else:
                raise ValueError('Unknown pipeline command')
            if args.command in ('replay', 'rescreen', 'resolve'):
                previous = root / 'last_run.json'
                info = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else None
                api.export_library(lib, info, config['collector'].get('report_days', 7), save_run=False)
                api.atomic_text(root / 'maintenance_history' / (api.now().strftime('%Y%m%dT%H%M%S%f') + '-' + args.command + '.json'),
                                json.dumps(dict(command=args.command, result=result), ensure_ascii=False, indent=2))
        finally:
            lib.db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'ok' else 2
