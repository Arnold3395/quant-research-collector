"""Read-only endpoint probe; no authentication, downloads or scheduled tasks."""
import concurrent.futures
import json
import urllib.request
from html.parser import HTMLParser

URLS = [
    'https://export.arxiv.org/api/query?search_query=cat:q-fin.TR&start=0&max_results=2&sortBy=lastUpdatedDate&sortOrder=descending',
    'https://www.bis.org/doclist/wppubls.rss',
    'https://www.federalreserve.gov/feeds/feds.xml',
    'https://www.federalreserve.gov/feeds/ifdp.xml',
    'https://www.ecb.europa.eu/rss/wppub.html',
    'https://www.cfm.com/our-thinking/',
    'https://www.sse.com.cn/aboutus/research/latest/',
    'https://www.aqr.com/insights/research',
]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a':
            self.current = [a.get('href', ''), '']

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.items.append(self.current)
            self.current = None


def probe(url):
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'QuantResearchCollector/0.1 (personal research)'})
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read(2_000_000)
            result = dict(url=url, status=response.status, final_url=response.url,
                          content_type=response.headers.get('Content-Type'), bytes=len(body))
            text = body.decode('utf-8', 'replace')
            if 'rss' in text[:1000].lower() or '<feed' in text[:1000]:
                result['sample'] = text[:2400]
            else:
                parser = Links()
                parser.feed(text)
                result['links'] = [[u, ' '.join(t.split())[:160]] for u, t in parser.items
                                   if t.strip() and any(x in (u + t).lower() for x in
                                      ('research', 'publ', '.pdf', 'trend', 'volatil', '期权', '报告', '市场质量', 'quant'))][-35:]
            return result
    except Exception as error:
        return dict(url=url, error=str(error))


if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(probe, URLS):
            print(json.dumps(result, ensure_ascii=True), flush=True)
