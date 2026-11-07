#!/usr/bin/env python3
"""Earnings Season: how often SaaS and CRM companies say "agent" in their SEC filings, per 1,000 words.

Sources (all public, SEC EDGAR, User-Agent "Customermates mail@customermates.com", 4 requests/s):
- data.sec.gov/submissions: the 10-Q, 10-K and 8-K (Item 2.02, results) filings of each company in companies.json;
- the 10-Q/10-K primary document and the EX-99.1 earnings release of each Item 2.02 8-K (counted by mentions.py);
- data.sec.gov/api/xbrl/companyconcept dei:EntityCommonStockSharesOutstanding (cover-page share count);
- Alpha Vantage TIME_SERIES_WEEKLY (free tier, 25 calls/day): weekly close of the filing week.
Market cap = shares outstanding (all classes, latest cover page on or before the filing) x weekly close; an estimate.

Outputs: filings.csv (one row per document) and seasons.csv (company x calendar quarter of the fiscal period end),
to the open repo customermates/earnings-agent-mentions and the "Earnings filings" list in the Customermates Data
workspace. Every row carries its SEC source URL. Counting is deterministic; no model is involved.

  python3 -I ops/data/earnings/crawl.py [--since 2024-01-01] [--no-crm] [--no-publish] [--limit-companies N]
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'lib'))
sys.path.insert(0, HERE)
import fetch  # noqa: E402
import publish  # noqa: E402
from mentions import count, visible_text  # noqa: E402

SERIES = 'earnings'
REPO = 'earnings-agent-mentions'
SEC_HOSTS = {'www.sec.gov': 0.25, 'data.sec.gov': 0.25}
AV_URL = 'https://www.alphavantage.co/query?function=TIME_SERIES_WEEKLY&symbol={}&apikey={}'
AV_DAILY_CAP = 24  # free tier: 25 requests per day, shared by all runs of the day
FILING_COLUMNS = ['source_key', 'ticker', 'company', 'segment', 'form', 'document', 'filed', 'period_end', 'season',
                  'words', 'agent_mentions', 'agent_per_1k', 'raw_agent_mentions', 'ai_mentions', 'ai_per_1k',
                  'shares_outstanding', 'price_usd', 'price_date', 'market_cap_usd', 'source_url']
SEASON_COLUMNS = ['ticker', 'company', 'segment', 'season', 'documents', 'words', 'agent_mentions', 'agent_per_1k',
                  'release_agent_per_1k', 'report_agent_per_1k', 'ai_per_1k', 'market_cap_usd', 'source_urls']

CRM_SPEC = {
    'label': 'Earnings filing', 'plural': 'Earnings filings', 'icon': 'chart', 'key': 'Source key',
    'description': 'Earnings Season data series: "agent" and "AI" mentions per 1,000 words in SEC 10-Q, 10-K and '
                   'EX-99.1 earnings releases of SaaS and CRM companies, with estimated market cap. Company-level '
                   'facts with the SEC source link; counts by script (ops/data/earnings), no model. Public dataset: '
                   'github.com/customermates/earnings-agent-mentions.',
    'fields': [
        {'label': 'Company', 'type': 'text'},
        {'label': 'Ticker', 'type': 'text'},
        {'label': 'Segment', 'type': 'select', 'options': []},
        {'label': 'Document', 'type': 'select', 'options': ['10-Q', '10-K', 'Earnings release (EX-99.1)']},
        {'label': 'Filed', 'type': 'date'},
        {'label': 'Period end', 'type': 'date'},
        {'label': 'Season', 'type': 'select', 'options': []},
        {'label': 'Agent mentions', 'type': 'number', 'decimals': 0},
        {'label': 'Agent per 1k words', 'type': 'number', 'decimals': 2},
        {'label': 'AI per 1k words', 'type': 'number', 'decimals': 2},
        {'label': 'Words', 'type': 'number', 'decimals': 0},
        {'label': 'Market cap', 'type': 'currency', 'currency': 'USD', 'decimals': 0},
        {'label': 'Source', 'type': 'url'},
        {'label': 'Source key', 'type': 'text'},
    ],
}


def quarter_label(date_text):
    d = datetime.date.fromisoformat(date_text)
    return '%d-Q%d' % (d.year, (d.month - 1) // 3 + 1)


def archive_base(cik, accession):
    return 'https://www.sec.gov/Archives/edgar/data/%d/%s/' % (int(cik), accession.replace('-', ''))


def recent_filings(sub, since):
    """Rows of the submissions JSON (recent + older pages already merged) filed on or after since."""
    r = sub['filings']['recent']
    out = []
    for i, form in enumerate(r['form']):
        filed = r['filingDate'][i]
        if filed < since:
            continue
        items = r.get('items', [''] * len(r['form']))[i] or ''
        if form in ('10-Q', '10-K') or (form == '8-K' and '2.02' in items.split(',')):
            out.append({'form': form, 'accession': r['accessionNumber'][i], 'filed': filed,
                        'report_date': r['reportDate'][i] or None, 'primary': r['primaryDocument'][i]})
    return out


EX991 = re.compile(r'<tr[^>]*>(.*?)</tr>', re.I | re.S)


def find_release(index_html):
    """The EX-99.1 (or first EX-99.x) document name in an EDGAR filing index page."""
    best = None
    for row in EX991.findall(index_html):
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.I | re.S)]
        link = re.search(r'href="([^"]+)"', row)
        if len(cells) < 4 or not link:
            continue
        doc_type = cells[3].upper()
        name = link.group(1).rsplit('/', 1)[-1]
        if not re.search(r'\.(htm|html|txt)$', name, re.I):
            continue
        if doc_type in ('EX-99.1', 'EX-99.01', 'EX-99'):
            return name
        if doc_type.startswith('EX-99') and best is None:
            best = name
    return best


def share_points(concept):
    """{accession: {end, filed, shares}} from a companyconcept reply; classes reported at the same date are summed
    (distinct values per accession and date, so a fact repeated in one filing counts once)."""
    by_acc = {}
    for f in (concept.get('units') or {}).get('shares', []):
        entry = by_acc.setdefault(f.get('accn'), {})
        entry.setdefault(f.get('end'), {'filed': f.get('filed'), 'values': set()})['values'].add(f['val'])
    out = {}
    for acc, ends in by_acc.items():
        end = max(ends)
        out[acc] = {'end': end, 'filed': ends[end]['filed'], 'shares': sum(ends[end]['values'])}
    return out


IX_SHARES = re.compile(r'<ix:nonFraction([^>]*name="dei:EntityCommonStockSharesOutstanding"[^>]*)>(.*?)</ix:nonFraction>', re.I | re.S)


def cover_shares(raw):
    """Total cover-page shares outstanding (all classes) from a 10-Q/10-K inline XBRL document, else None."""
    by_context = {}
    for attrs, value in IX_SHARES.findall(raw):
        digits = re.sub(r'[^0-9.]', '', re.sub(r'<[^>]+>', '', value))
        if not digits:
            continue
        scale = re.search(r'scale="(-?\d+)"', attrs)
        context = re.search(r'contextRef="([^"]+)"', attrs)
        by_context[context.group(1) if context else len(by_context)] = round(float(digits) * 10 ** int(scale.group(1) if scale else 0))
    return sum(by_context.values()) or None


def shares_for(points, filed, accession=None):
    if accession and accession in points:
        return points[accession]['shares']
    prior = [p for p in points.values() if p['filed'] and p['filed'] <= filed]
    if not prior:
        return None
    return max(prior, key=lambda p: p['filed'])['shares']


def weekly_close(series, filed):
    """(close, week date) of the first weekly bar on or after the filing date, else the latest bar before it."""
    if not series:
        return None, None
    dates = sorted(series)
    after = [d for d in dates if d >= filed]
    day = after[0] if after else dates[-1]
    if not after and (datetime.date.fromisoformat(filed) - datetime.date.fromisoformat(day)).days > 10:
        return None, None
    return float(series[day]), day


def assign_periods(docs):
    """8-K releases get the fiscal period of the next 10-Q/10-K filed within 75 days (same company)."""
    reports = sorted((d for d in docs if d['form'] in ('10-Q', '10-K') and d.get('report_date')), key=lambda d: d['filed'])
    for d in docs:
        if d['form'] in ('10-Q', '10-K'):
            d['period_end'] = d.get('report_date')
            continue
        filed = datetime.date.fromisoformat(d['filed'])
        nxt = [r for r in reports if 0 <= (datetime.date.fromisoformat(r['filed']) - filed).days <= 75]
        if nxt:
            d['period_end'] = nxt[0]['report_date']
        else:  # results announced before the 10-Q exists yet: the quarter that ended before the announcement
            back = filed - datetime.timedelta(days=12)
            q_end_month = ((back.month - 1) // 3) * 3
            year = back.year if q_end_month else back.year - 1
            month = q_end_month or 12
            last = (datetime.date(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1))
            d['period_end'] = last.isoformat()
            d['period_estimated'] = True
    return docs


class Crawler:
    def __init__(self, fetcher, av_fetcher, av_key, state):
        self.f, self.av, self.av_key, self.state = fetcher, av_fetcher, av_key, state
        self.av_calls = 0

    def docs_for(self, company, since):
        cik10 = '%010d' % company['cik']
        sub = self.f.get_json('https://data.sec.gov/submissions/CIK%s.json' % cik10)
        docs = []
        for filing in recent_filings(sub, since):
            key = filing['accession'] + ':' + filing['form']
            cached = self.state['docs'].get(key)
            if cached and filing['form'] != '8-K' and 'cover_shares' not in cached and not cached.get('skip'):
                cached = None  # one-time refetch to read the cover-page share count
            if cached:
                docs.append(dict(cached, report_date=filing['report_date']))
                continue
            base = archive_base(company['cik'], filing['accession'])
            if filing['form'] == '8-K':
                index = self.f.get_text(base + filing['accession'] + '-index.htm')
                name = find_release(index)
                if not name:
                    self.state['docs'][key] = {'form': '8-K', 'skip': 'no EX-99 exhibit', 'filed': filing['filed']}
                    continue
                url, doc_label = base + name, 'Earnings release (EX-99.1)'
            else:
                url, doc_label = base + filing['primary'], filing['form']
            raw = self.f.get_text(url)
            text = visible_text(raw)
            entry = dict(count(text), form=filing['form'], cover_shares=cover_shares(raw) if filing['form'] != '8-K' else None, document=doc_label, filed=filing['filed'],
                         accession=filing['accession'], source_url=url, report_date=filing['report_date'])
            self.state['docs'][key] = entry
            docs.append(entry)
        return [d for d in docs if not d.get('skip')]

    def shares(self, company):
        cik10 = '%010d' % company['cik']
        url = 'https://data.sec.gov/api/xbrl/companyconcept/CIK%s/dei/EntityCommonStockSharesOutstanding.json' % cik10
        try:
            return share_points(self.f.get_json(url))
        except fetch.FetchFailed as error:
            if error.status == 404:
                return {}
            raise

    def prices(self, ticker, newest_filed):
        cache = self.state['prices'].get(ticker) or {}
        fresh = cache.get('fetched') and cache['fetched'] >= newest_filed and \
            (datetime.date.today() - datetime.date.fromisoformat(cache['fetched'])).days < 7
        day = self.state.setdefault('av_day', {})
        if day.get('date') != datetime.date.today().isoformat():
            day.update(date=datetime.date.today().isoformat(), calls=0)
        if fresh or not self.av_key or day['calls'] >= AV_DAILY_CAP:
            return cache.get('weekly') or {}
        day['calls'] += 1
        self.av_calls += 1
        reply = self.av.get_json(AV_URL.format(urllib.parse.quote(ticker), self.av_key))
        weekly = reply.get('Weekly Time Series')
        if not weekly:  # rate note or error: keep what we have, try again next run
            print('alphavantage %s: %s' % (ticker, (reply.get('Note') or reply.get('Information') or 'no data')[:120]), file=sys.stderr)
            return cache.get('weekly') or {}
        series = {d: v['4. close'] for d, v in weekly.items() if d >= '2023-06-01'}
        self.state['prices'][ticker] = {'fetched': datetime.date.today().isoformat(), 'weekly': series}
        return series


def build_rows(company, docs, points, prices):
    rows = []
    covers = sorted((d['filed'], d['cover_shares']) for d in docs if d.get('cover_shares'))
    for d in assign_periods(docs):
        shares = d.get('cover_shares')
        if not shares and d['form'] == '8-K':
            prior = [c for f, c in covers if f <= d['filed']]
            shares = prior[-1] if prior else None
        if not shares:
            shares = shares_for(points, d['filed'], d['accession'] if d['form'] != '8-K' else None)
        price, price_date = weekly_close(prices, d['filed'])
        rows.append({
            'source_key': 'sec:' + d['accession'] + ':' + ('ex99' if d['form'] == '8-K' else d['form'].lower()),
            'ticker': company['ticker'], 'company': company['name'], 'segment': company['segment'],
            'form': d['form'], 'document': d['document'], 'filed': d['filed'], 'period_end': d['period_end'],
            'season': quarter_label(d['period_end']), 'words': d['words'], 'agent_mentions': d['agent_mentions'],
            'agent_per_1k': d['agent_per_1k'], 'raw_agent_mentions': d['raw_agent_mentions'],
            'ai_mentions': d['ai_mentions'], 'ai_per_1k': d['ai_per_1k'], 'shares_outstanding': shares,
            'price_usd': price, 'price_date': price_date,
            'market_cap_usd': round(shares * price) if shares and price else None, 'source_url': d['source_url'],
        })
    return rows


def seasons(rows):
    groups = {}
    for r in rows:
        groups.setdefault((r['ticker'], r['season']), []).append(r)
    out = []
    for (ticker, season), rs in sorted(groups.items()):
        words = sum(r['words'] for r in rs)
        agent = sum(r['agent_mentions'] for r in rs)
        ai = sum(r['ai_mentions'] for r in rs)
        rel = [r for r in rs if r['form'] == '8-K']
        rep = [r for r in rs if r['form'] != '8-K']
        rate = lambda xs: round(1000 * sum(x['agent_mentions'] for x in xs) / sum(x['words'] for x in xs), 3) \
            if xs and sum(x['words'] for x in xs) else None
        caps = [r['market_cap_usd'] for r in sorted(rs, key=lambda r: r['filed']) if r['market_cap_usd']]
        out.append({'ticker': ticker, 'company': rs[0]['company'], 'segment': rs[0]['segment'], 'season': season,
                    'documents': len(rs), 'words': words, 'agent_mentions': agent,
                    'agent_per_1k': round(1000 * agent / words, 3) if words else None,
                    'release_agent_per_1k': rate(rel), 'report_agent_per_1k': rate(rep),
                    'ai_per_1k': round(1000 * ai / words, 3) if words else None,
                    'market_cap_usd': caps[-1] if caps else None,
                    'source_urls': ' '.join(r['source_url'] for r in rs)})
    return out


def crm_rows(rows):
    out = []
    for r in rows:
        out.append({'title': '%s %s %s' % (r['company'], 'earnings release' if r['form'] == '8-K' else r['form'], r['filed']),
                    'Company': r['company'], 'Ticker': r['ticker'], 'Segment': r['segment'], 'Document': r['document'],
                    'Filed': r['filed'], 'Period end': r['period_end'], 'Season': r['season'],
                    'Agent mentions': r['agent_mentions'], 'Agent per 1k words': r['agent_per_1k'],
                    'AI per 1k words': r['ai_per_1k'], 'Words': r['words'], 'Market cap': r['market_cap_usd'],
                    'Source': r['source_url'], 'Source key': r['source_key']})
    return out


README = """# Earnings Season: "agent" mentions in SaaS and CRM filings

How often do listed SaaS and CRM companies say "agent" in what they file with the SEC? Counts per 1,000 words in
10-Q and 10-K reports and in the EX-99.1 earnings releases attached to Item 2.02 8-Ks, with an estimated market cap.
Updated daily by a script from SEC EDGAR; maintained by [Customermates](https://customermates.com).

- `filings.csv`: one row per document, with its SEC source URL.
- `seasons.csv`: one row per company and calendar quarter of the fiscal period end.

## Method

- **agent mentions:** words starting with "agent" (agent, agents, agentic, Agentforce, Agentspace, ...), minus legal
  and financial uses such as "transfer agent", "administrative agent", "agent for service" or "employees and agents".
  `raw_agent_mentions` keeps the unfiltered count.
- **AI mentions:** the token "AI" plus "artificial intelligence".
- **words:** alphabetic tokens of the visible document text (inline XBRL header removed).
- **market cap (estimate):** cover-page shares outstanding (dei:EntityCommonStockSharesOutstanding, all classes) times
  the weekly close of the filing week (Alpha Vantage). Releases use the latest share count filed before them.
- **season:** calendar quarter in which the fiscal period ends (Salesforce's quarter ending 31 Oct counts as Q4).
  For a release filed before its 10-Q, the period is the quarter that ended just before the release.
- **companies:** {companies} US filers; see `companies.csv`. Foreign private issuers (20-F/6-K) are not included.

Source: U.S. Securities and Exchange Commission, EDGAR (public domain). Prices: Alpha Vantage. Licence of this
dataset: CC BY 4.0, credit "Customermates Data".
"""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2024-01-01')
    ap.add_argument('--no-crm', action='store_true')
    ap.add_argument('--no-publish', action='store_true')
    ap.add_argument('--limit-companies', type=int)
    args = ap.parse_args(argv)
    companies = json.load(open(os.path.join(HERE, 'companies.json')))['companies'][:args.limit_companies]
    sdir = publish.state_dir(SERIES)
    with publish.RunLock(SERIES):
        state = publish.load_json(sdir / 'state.json', {'docs': {}, 'prices': {}})
        sec = fetch.Fetcher(user_agent=fetch.SEC_USER_AGENT, intervals=SEC_HOSTS)
        av = fetch.Fetcher(intervals={'www.alphavantage.co': 2.5}, respect_robots=False)  # API host, documented free key
        crawler = Crawler(sec, av, os.environ.get('ALPHAVANTAGE_API_KEY'), state)
        rows, errors = [], []
        for company in companies:
            try:
                docs = crawler.docs_for(company, args.since)
                points = crawler.shares(company)
                newest = max((d['filed'] for d in docs), default=args.since)
                prices = crawler.prices(company['ticker'], newest)
                rows += build_rows(company, docs, points, prices)
            except (fetch.FetchFailed, fetch.Disallowed, ValueError, KeyError) as error:
                errors.append('%s: %s' % (company['ticker'], error))
            publish.save_json(sdir / 'state.json', state)
        rows.sort(key=lambda r: (r['filed'], r['ticker'], r['form']))
        season_rows = seasons(rows)
        files = {
            'filings.csv': publish.csv_text(rows, FILING_COLUMNS),
            'seasons.csv': publish.csv_text(season_rows, SEASON_COLUMNS),
            'companies.csv': publish.csv_text(companies, ['ticker', 'cik', 'name', 'segment']),
            'README.md': README.format(companies=len(companies)),
        }
        for name, text in files.items():
            (sdir / name).write_text(text)
        files.update(publish.code_files(HERE, ['crawl.py', 'mentions.py', 'companies.json']), LICENSE=publish.LICENSE_NOTE)
        result = {'series': SERIES, 'companies': len(companies), 'documents': len(rows), 'seasons': len(season_rows),
                  'sec_requests': sec.requests, 'alphavantage_calls': crawler.av_calls, 'errors': errors}
        if not args.no_publish:
            result['repo'] = publish.publish_repo(REPO, files, 'data: %s, %d documents' % (publish.today(), len(rows)))
        if not args.no_crm:
            import cm_sink
            if cm_sink.available():
                try:
                    result['crm'] = cm_sink.Sink().upsert(CRM_SPEC_WITH_OPTIONS(rows), crm_rows(rows))
                except Exception as error:  # noqa: BLE001 - reported, the CSV run still counts
                    result['crm_error'] = str(error)[:300]
            else:
                result['crm'] = 'no own MCP login for the Data workspace yet'
        state['last_run'] = result
        publish.save_json(sdir / 'state.json', state)
        print(json.dumps(result))
        return 1 if errors and not rows else 0


def CRM_SPEC_WITH_OPTIONS(rows):  # noqa: N802 - select options follow the data
    spec = json.loads(json.dumps(CRM_SPEC))
    for field in spec['fields']:
        if field['label'] == 'Segment':
            field['options'] = sorted({r['segment'] for r in rows})
        if field['label'] == 'Season':
            field['options'] = sorted({r['season'] for r in rows})
    return spec


if __name__ == '__main__':
    sys.exit(main())
