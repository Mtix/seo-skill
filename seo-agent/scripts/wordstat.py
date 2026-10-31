#!/usr/bin/env python3
"""Collect Wordstat keyword evidence via Yandex Search API (stdlib only)."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = 'https://searchapi.api.cloud.yandex.net/v2/wordstat/'
DEVICES = {'all': 'DEVICE_ALL', 'desktop': 'DEVICE_DESKTOP', 'phone': 'DEVICE_PHONE', 'tablet': 'DEVICE_TABLET'}

class WordstatError(Exception):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a redirect destination.


def load_env(path, environ=None):
    """Read literal KEY=value; environment overrides file. Never evaluate shell syntax."""
    values = {}
    if path.exists():
        for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            key, sep, value = line.partition('=')
            if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
                raise WordstatError(f'Invalid env syntax at line {number}; expected KEY=value')
            value = value.strip()
            if value.startswith(('"', "'")):
                quote = value[0]
                end = value.find(quote, 1)
                if end < 0 or (value[end+1:].strip() and not value[end+1:].strip().startswith('#')):
                    raise WordstatError(f'Invalid quoted env value at line {number}')
                value = value[1:end]
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
            values[key.strip()] = value
    values.update(os.environ if environ is None else environ)
    return values


def credentials(values):
    key = values.get('YANDEX_API_KEY', '').strip()
    folder = values.get('YANDEX_FOLDER_ID', '').strip()
    if not key or not folder:
        raise WordstatError('Set YANDEX_API_KEY and YANDEX_FOLDER_ID in .env or environment')
    if any(c in key + folder for c in '\r\n'):
        raise WordstatError('Invalid credentials format')
    return key, folder


def call_api(method, payload, key):
    request = Request(BASE + method, data=json.dumps(payload).encode('utf-8'),
                      headers={'Authorization': 'Api-Key ' + key, 'Content-Type': 'application/json'}, method='POST')
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            data = json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        hints = {401: 'Check API key', 403: 'Check service-account role search-api.webSearch.user, key scope yc.search-api.execute and folder access',
                 429: 'Quota exceeded; stop and retry later', 400: 'Check phrase and region parameters',
                 501: 'API method unavailable; check current official API documentation'}
        raise WordstatError(f'Wordstat HTTP {error.code}: {hints.get(error.code, "Request failed; no automatic retry")}') from None
    except (URLError, TimeoutError, OSError):
        raise WordstatError('Wordstat network error; no automatic retry') from None
    except (ValueError, UnicodeError):
        raise WordstatError('Wordstat returned invalid JSON') from None
    if not isinstance(data, dict) or 'error' in data:
        raise WordstatError('Wordstat returned an unexpected response')
    return data


def collect(phrases, regions, device, limit, folder, key, cache_dir, refresh=False, max_requests=20):
    reports, keywords = [], {}
    network_calls = 0
    for phrase in phrases:
        payload = dict(phrase=phrase, numPhrases=str(limit), regions=regions,
                       devices=[DEVICES[device]], folderId=folder)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        cache = cache_dir / (digest + '.json')
        report = None
        if not refresh and cache.exists():
            try:
                candidate = json.loads(cache.read_text(encoding='utf-8'))
                age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(candidate['fetched_at']).timestamp()
                if 0 <= age <= 7 * 86400 and candidate['request'] == payload:
                    report = candidate
            except (ValueError, KeyError, TypeError):
                pass
        if report is None:
            if network_calls >= max_requests:
                raise WordstatError('Request budget exhausted; use fewer seeds or explicitly raise --max-requests')
            if network_calls:
                time.sleep(1)
            data = call_api('topRequests', payload, key)
            # Empty repeated protobuf fields may be omitted in REST JSON.
            for field in ('results', 'associations'):
                if not isinstance(data.get(field, []), list):
                    raise WordstatError('Invalid Wordstat keyword list')
                for item in data.get(field, []):
                    if not isinstance(item, dict) or not isinstance(item.get('phrase'), str):
                        raise WordstatError('Invalid Wordstat keyword entry')
                    try:
                        if int(item['count']) < 0:
                            raise ValueError()
                    except (KeyError, ValueError, TypeError):
                        raise WordstatError('Invalid Wordstat frequency') from None
            report = dict(fetched_at=datetime.now(timezone.utc).isoformat(), source=BASE+'topRequests',
                          period='last_30_days', request=payload, response=data)
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp = cache.with_suffix('.tmp')
            temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(cache)
            network_calls += 1
        reports.append(report)
        for field in ('results', 'associations'):
            for item in report['response'].get(field, []):
                normalized = ' '.join(item['phrase'].casefold().split())
                keyword = keywords.setdefault(normalized, dict(phrase=item['phrase'], observations=[]))
                keyword['observations'].append(dict(seed=phrase, kind=field, count=int(item['count']), fetched_at=report['fetched_at']))
    return dict(source='Yandex Wordstat / Yandex Search API', regions=regions, device=device,
                period='last_30_days_at_each_fetch', network_calls=network_calls,
                limitation='Query counts are not unique users or a traffic forecast. Overlapping phrases must not be summed.',
                keywords=list(keywords.values()), reports=reports)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--check-config', action='store_true')
    parser.add_argument('--phrase', action='append', default=[])
    parser.add_argument('--phrases-file', type=Path)
    parser.add_argument('--regions', default='', help='Comma-separated region IDs; empty means all regions')
    parser.add_argument('--device', choices=DEVICES, default='all')
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--max-requests', type=int, default=20)
    parser.add_argument('--cache-dir', type=Path, default=Path('.seo/wordstat-cache'))
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--regions-tree', action='store_true')
    args = parser.parse_args(argv)
    key, folder = credentials(load_env(args.env_file))
    if args.check_config:
        print('Wordstat config present (credentials hidden; API access not tested)')
        return
    if args.regions_tree:
        result = call_api('getRegionsTree', {'folderId': folder}, key)
    else:
        phrases = list(args.phrase)
        if args.phrases_file:
            phrases.extend(line.strip() for line in args.phrases_file.read_text(encoding='utf-8-sig').splitlines() if line.strip() and not line.lstrip().startswith('#'))
        phrases = list(dict.fromkeys(p.strip() for p in phrases if p.strip()))
        regions = list(dict.fromkeys(r.strip() for r in args.regions.split(',') if r.strip()))
        if not phrases or any(len(p) > 400 for p in phrases):
            raise WordstatError('Provide seed phrases of 1–400 characters')
        if not 1 <= args.limit <= 2000 or not 1 <= args.max_requests <= 100:
            raise WordstatError('limit must be 1–2000; max-requests must be 1–100')
        if len(regions) > 100 or any(not r.isdigit() for r in regions):
            raise WordstatError('Use up to 100 numeric region IDs')
        result = collect(phrases, regions, args.device, args.limit, folder, key, args.cache_dir, args.refresh, args.max_requests)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + '\n', encoding='utf-8')
        print(f'Saved Wordstat data: {args.output}')
    else:
        print(rendered)

if __name__ == '__main__':
    try:
        main()
    except (WordstatError, OSError) as error:
        # Never print request headers or raw server errors, which may contain credentials.
        print(f'Error: {error if isinstance(error, WordstatError) else "Local file operation failed"}', file=sys.stderr)
        sys.exit(1)
