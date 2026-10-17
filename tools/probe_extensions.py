"""Small, read-only probes through the collector's normal access policy."""
import concurrent.futures
import json
from pathlib import Path
import sys
import urllib.parse

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import collector as c

PROBES = [
    ('crossref', 'https://api.crossref.org/works?' + urllib.parse.urlencode({'query.title': 'portfolio optimization', 'rows': 2}), ['crossref.org']),
    ('datacite', 'https://api.datacite.org/dois?' + urllib.parse.urlencode({'query': 'titles.title:"portfolio optimization"', 'page[size]': 2}), ['datacite.org']),
    ('hal', 'https://api.archives-ouvertes.fr/search/?' + urllib.parse.urlencode({'q': 'title_t:"portfolio optimization"', 'rows': 2, 'wt': 'json', 'fl': 'halId_s,title_s,abstract_s,authFullName_s,doiId_s,uri_s,fileMain_s,producedDate_s,modifiedDate_tdate,version_i,docType_s'}), ['archives-ouvertes.fr', 'hal.science']),
    ('econstor', 'https://www.econstor.eu/oai/request?verb=Identify', ['econstor.eu']),
    ('boe', 'https://www.bankofengland.co.uk/working-paper/staff-working-papers', ['bankofengland.co.uk']),
    ('nyfed', 'https://www.newyorkfed.org/research/staff_reports/', ['newyorkfed.org']),
]


def probe(item):
    name, url, domains = item
    root = BASE / 'tmp' / 'extensions-probe'
    root.mkdir(parents=True, exist_ok=True)
    client = c.Client(dict(timeout_seconds=20, http_attempts=1, request_interval_seconds=1.2), [dict(allowed_domains=domains)])
    try:
        body, headers, final = client.get(url, max_bytes=2_000_000)
        (root / (name + '.body')).write_bytes(body)
        result = dict(source=name, status='ok', bytes=len(body), final_url=final,
                      content_type=headers.get('Content-Type', ''), metrics=client.metrics)
        if name in ('boe', 'nyfed'):
            page = c.Page(body.decode('utf-8', 'replace'))
            result['sample_links'] = [(u, t[:120]) for u, t in page.links if ('/working-paper/' in u or 'staff_report' in u or '.pdf' in u) and t][:15]
        else:
            result['sample'] = body[:250].decode('utf-8', 'replace')
    except Exception as error:
        result = dict(source=name, status='failed', error=str(error))
    (root / (name + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(probe, PROBES):
            print(json.dumps(result, ensure_ascii=False), flush=True)
