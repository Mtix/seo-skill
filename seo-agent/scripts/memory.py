#!/usr/bin/env python3
"""Transactional SEO memory, using only Python's standard library."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

STATUSES = {'backlog', 'planned', 'in_progress', 'draft', 'published', 'measuring', 'done', 'blocked', 'cancelled'}

def site_key(value):
    url = urlsplit(value if '://' in value else 'https://' + value)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise ValueError('Expected website URL without credentials')
    host = url.hostname.lower().encode('idna').decode('ascii').rstrip('.')
    if host.startswith('www.'):
        host = host[4:]
    if url.port and url.port not in (80, 443):
        host += ':' + str(url.port)
    return host

def validate(state, previous):
    if not isinstance(state, dict) or state.get('schema_version') != 1:
        raise ValueError('schema_version must equal 1')
    for field, kind in [('profile', dict), ('research', list), ('pages', list), ('tasks', list), ('decisions', list), ('metrics', list)]:
        if not isinstance(state.get(field), kind):
            raise ValueError(f'{field} must be {kind.__name__}')
    ids = set()
    for task in state['tasks']:
        if not isinstance(task, dict):
            raise ValueError('Each task must be an object')
        task_id = task.get('id')
        if not isinstance(task_id, str) or not task_id.strip() or task_id in ids:
            raise ValueError('Task IDs must be unique nonempty strings')
        ids.add(task_id)
        if not task.get('title') or task.get('status') not in STATUSES:
            raise ValueError(f'{task_id}: title and valid status required')
        if not isinstance(task.get('depends_on', []), list):
            raise ValueError(f'{task_id}: depends_on must be an array')
        if task['status'] in {'published', 'measuring'} and not task.get('published_url'):
            raise ValueError(f'{task_id}: published_url required')
        if task['status'] == 'done' and not task.get('completion_evidence'):
            raise ValueError(f'{task_id}: completion_evidence required')
    graph = {t['id']: t.get('depends_on', []) for t in state['tasks']}
    active, visited = set(), set()
    def visit(key):
        if key in active:
            raise ValueError('Task dependency cycle')
        if key in visited:
            return
        active.add(key)
        for dep in graph[key]:
            if not isinstance(dep, str) or dep not in ids:
                raise ValueError(f'{key}: unknown dependency')
            visit(dep)
        active.remove(key)
        visited.add(key)
    for key in graph:
        visit(key)
    old_ids = {t['id'] for t in previous['tasks']}
    if not old_ids <= ids:
        raise ValueError('Do not delete existing tasks; cancel them instead')
    for field in ('research', 'decisions', 'metrics'):
        old = previous[field]
        if state[field][:len(old)] != old:
            raise ValueError(f'{field} is append-only; append corrections')

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('sites')
    for name in ('init', 'show', 'history', 'save'):
        sub = commands.add_parser(name)
        sub.add_argument('site')
        if name == 'save':
            sub.add_argument('--file', required=True, type=Path)
            sub.add_argument('--expected-version', required=True, type=int)
            sub.add_argument('--note', required=True)
    args = parser.parse_args(argv)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.db, timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS sites (site TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, site TEXT NOT NULL, version INTEGER NOT NULL, at TEXT NOT NULL, note TEXT NOT NULL, state TEXT NOT NULL)')
        if args.command == 'sites':
            print(json.dumps([dict(site=s, version=v) for s, v in db.execute('SELECT site,version FROM sites ORDER BY site')], ensure_ascii=False, indent=2))
            return
        key = site_key(args.site)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT version,state FROM sites WHERE site=?', (key,)).fetchone()
        if args.command == 'init' and not row:
            state = dict(schema_version=1, profile={'site': key, 'input_url': args.site}, research=[], pages=[], tasks=[], decisions=[], metrics=[])
            raw = json.dumps(state, ensure_ascii=False)
            db.execute('INSERT INTO sites VALUES (?,?,?)', (key, 1, raw))
            db.execute('INSERT INTO events(site,version,at,note,state) VALUES (?,?,?,?,?)', (key, 1, datetime.now(timezone.utc).isoformat(), 'Workspace created', raw))
            row = (1, raw)
        if not row:
            raise ValueError('Unknown site; run init first')
        version, raw = row
        if args.command == 'history':
            result = [dict(version=v, at=t, note=n) for v,t,n in db.execute('SELECT version,at,note FROM events WHERE site=? ORDER BY id', (key,))]
        elif args.command == 'save':
            if args.expected_version != version:
                raise ValueError(f'Version conflict: expected {args.expected_version}, current {version}; reload and merge')
            state = json.loads(args.file.read_text(encoding='utf-8'))
            validate(state, json.loads(raw))
            if state['profile'].get('site') != key:
                raise ValueError('profile.site must match workspace')
            raw = json.dumps(state, ensure_ascii=False)
            version += 1
            db.execute('UPDATE sites SET version=?,state=? WHERE site=?', (version, raw, key))
            db.execute('INSERT INTO events(site,version,at,note,state) VALUES (?,?,?,?,?)', (key, version, datetime.now(timezone.utc).isoformat(), args.note, raw))
            result = dict(site=key, version=version, state=state)
        else:
            result = dict(site=key, version=version, state=json.loads(raw))
        print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, TypeError, RecursionError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
