"""Optional Athletic.net girls XC import; Chrome and Playwright are required."""
from __future__ import annotations

from datetime import date
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.request import urlopen

from run import ROOT, DB, is_home, validate_row, validate_races, import_races, build_site, show_import


def race_url(value):
    match = re.fullmatch(
        r'https://(?:www\.)?athletic\.net/CrossCountry/meet/(\d+)/results/(\d+)/?(?:[?#].*)?',
        value.strip(), re.I)
    if not match:
        raise ValueError('Paste the race Results URL, ending in /results/ followed by its race ID, rather than the meet Info URL.')
    return f'https://www.athletic.net/CrossCountry/meet/{match[1]}/results/{match[2]}', match[1], match[2]


def parse_snapshot(snapshot, *, division=None, meet_key=None):
    url, mid, rid = race_url(snapshot['url'])
    if race_url(snapshot['requested_url'])[0] != url:
        raise ValueError('The browser redirected to a different race. No import performed.')
    heading = ' '.join(snapshot['heading'].split())
    match = re.fullmatch(r"(?:Women'?s|Girls)\s+([\d,]+(?:\.\d+)?)\s+(Meters|Kilometers|Miles)\s+(.+)", heading, re.I)
    if not match:
        raise ValueError('The selected page must explicitly identify a girls race and its distance.')
    label = match[3].strip()
    if re.search(r'\b(MS|Middle|Elementary|EL|Youth|College)\b', label, re.I):
        raise ValueError('Only high-school girls XC races are supported.')
    if label.casefold() not in {'jv', 'junior varsity', 'varsity', 'freshman', 'freshmen', 'open', 'high school'}:
        raise ValueError(f'Unrecognized high-school race label: {label}. Review the source first.')
    distance = float(match[1].replace(',', '')) * {'meters': 1, 'kilometers': 1000, 'miles': 1609.344}[match[2].lower()]
    details = snapshot['event']
    if not re.search(rf'/meet/{mid}(?:/|$|\?)', str(details.get('url') or details.get('mainEntityOfPage') or '')):
        raise ValueError('Meet metadata belongs to a different meet.')
    day = date.fromisoformat(str(details.get('startDate', ''))[:10]).isoformat()
    if not details.get('name'):
        raise ValueError('The source did not provide a meet name.')
    venue = details.get('location') or {}
    meet = {'key': meet_key or f'athletic.net:{mid}', 'name': details['name'], 'date': day,
            'venue': venue.get('name', '') if isinstance(venue, dict) else ''}
    rows = []
    for raw in snapshot['rows']:
        grade = re.search(r'\bYr:\s*(\d{1,2})\b', raw.get('stats', ''))
        athlete_id = re.search(r'/athlete/(\d+)(?:/|$)', raw.get('profile_url', ''))
        mark = raw['time'].strip()
        status = mark.upper() if mark.upper() in {'DNF', 'DNS', 'DQ'} else 'FINISHED'
        rows.append(validate_row({'place': raw['place'], 'athlete': raw['athlete'],
            'team': raw['team'], 'time': mark, 'status': status, 'gender': 'F',
            'grade': grade[1] if grade else '', 'athlete_id': athlete_id[1] if athlete_id else ''}))
    if not any(is_home(row['team']) for row in rows):
        raise ValueError('No Sacred Heart runners were found in this race.')
    places = {}
    for row in rows:
        if row['status'] == 'FINISHED':
            places.setdefault(row['place'], set()).add(row['seconds'])
    if any(len(marks) > 1 for marks in places.values()):
        raise ValueError('Conflicting finish places require source review.')
    races = [{'division': division or ('JV' if label.lower() == 'junior varsity' else label),
              'distance_m': distance, 'race_date': day, 'rows': rows}]
    validate_races(races)
    return meet, races


def fetch_snapshot(value):
    url, mid, rid = race_url(value)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError('Athletic.net needs Playwright and Google Chrome. Run: python -m pip install -r requirements-athletic.txt (use py -3 instead of python if you use the Windows launcher).') from exc
    endpoint = 'http://127.0.0.1:9222'
    attached = False
    try:
        with urlopen(endpoint + '/json/version', timeout=2) as response:
            attached = bool(json.load(response).get('webSocketDebuggerUrl'))
    except (OSError, ValueError):
        pass
    with sync_playwright() as p:
        if attached:
            browser = p.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
        else:
            context = p.chromium.launch_persistent_context(
                str(ROOT / 'data' / 'athletic-browser-profile'), channel='chrome', headless=False)
        page = context.new_page()
        try:
            print('Opening the girls race in Chrome. Complete a browser check there if requested.', flush=True)
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            page.locator('div.result-row').first.wait_for(timeout=120000)
            previous = -1
            stable = 0
            for _ in range(30):
                count = page.locator('div.result-row').count()
                stable = stable + 1 if count == previous else 0
                previous = count
                if stable >= 3:
                    break
                time.sleep(1)
            else:
                raise ValueError('The results are still changing. Retry when the race has finished loading.')
            snapshot = page.evaluate(r'''() => {
                const clean = s => (s || '').replace(/\s+/g, ' ').trim();
                const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5')].map(x=>clean(x.textContent));
                const heading = headings.find(x=>/^(Womens|Women's|Girls|Mens|Men's|Boys)\s+/.test(x)) || '';
                const objects = v => Array.isArray(v) ? v.flatMap(objects) : v && typeof v === 'object' ? [v, ...objects(v['@graph'] || [])] : [];
                const events = [...document.querySelectorAll('script[type="application/ld+json"]')].flatMap(s=>{try{return objects(JSON.parse(s.textContent))}catch{return []}});
                const event = events.find(x=>x['@type']==='SportsEvent') || {};
                const rows = [...document.querySelectorAll('div.result-row')].map(row=>({
                    place: clean(row.querySelector(':scope > .place-column')?.textContent),
                    athlete: clean(row.querySelector('.primary .title a[href*="/athlete/"]')?.textContent),
                    profile_url: row.querySelector('.primary .title a[href*="/athlete/"]')?.href || '',
                    team: clean(row.querySelector('.primary .subtitle.team .text-overflow-ellipsis')?.textContent),
                    time: clean(row.querySelector('.secondary .title')?.textContent),
                    stats: clean(row.querySelector('.tertiary-content')?.textContent)
                }));
                const more = [...document.querySelectorAll('button,a')].filter(x=>x.getClientRects().length && !x.disabled && /^(load more|show more results|next page)$/i.test(clean(x.textContent))).length;
                return {url:location.href, heading, event, rows, more};
            }''')
            if snapshot['more']:
                raise ValueError('The page has more results to load; refusing a partial import.')
            snapshot['requested_url'] = url
            folder = ROOT / 'data' / 'sources'
            folder.mkdir(parents=True, exist_ok=True)
            raw = page.content()
            digest = hashlib.sha256(raw.encode()).hexdigest()
            html_path = folder / f'athletic_{mid}_{rid}_{digest[:12]}.html'
            html_path.write_text(raw, encoding='utf-8')
            snapshot['html_file'] = html_path.name
            snapshot['html_sha256'] = digest
            payload = json.dumps(snapshot, indent=2, ensure_ascii=False)
            artifact = folder / f'athletic_{mid}_{rid}_{hashlib.sha256(payload.encode()).hexdigest()[:12]}.json'
            artifact.write_text(payload, encoding='utf-8')
            return snapshot, artifact
        finally:
            page.close()
            if not attached:
                context.close()


def save_import(meet, races, artifact, url, *, replace=False, explicit_meet_key=False, db_path=DB):
    if Path(db_path).exists():
        with closing(sqlite3.connect(db_path)) as conn:
            same_day = conn.execute('SELECT meet_key,name FROM meets WHERE date=? AND meet_key<>?', (meet['date'], meet['key'])).fetchall()
        if same_day and not explicit_meet_key:
            raise ValueError(f'Another meet already exists on this date: {same_day}. Review it and supply --meet-key explicitly to reuse that meet or confirm a distinct meet.')
    return import_races(meet, races, 'athletic.net', url, artifact, replace=replace, db_path=db_path)


def interactive(input_fn=input):
    snapshot, artifact = fetch_snapshot(input_fn('Girls race Results URL: ').strip())
    meet, races = parse_snapshot(snapshot)
    show_import(meet, races)
    if input_fn('Import this girls race? (y/n): ').strip().lower() != 'y':
        print('Preview only; no database changes.')
        return
    replace = input_fn('Replace this race if already imported? (y/n): ').strip().lower() == 'y'
    count = save_import(meet, races, artifact, snapshot['url'], replace=replace)
    print(f'Imported {count} girls results.')
    build_site()


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', nargs='?')
    parser.add_argument('--saved', type=Path, help='Replay a previously saved Athletic.net snapshot without a browser')
    parser.add_argument('--division', help='Reviewed display label; gender and school level are still validated')
    parser.add_argument('--meet-key', help='Explicit existing meet key for a reviewed cross-source update')
    parser.add_argument('--commit', action='store_true')
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args(argv)
    if args.saved:
        if args.url:
            parser.error('Use a URL or --saved, not both.')
        artifact = args.saved.resolve()
        snapshot = json.loads(artifact.read_text(encoding='utf-8'))
    else:
        if not args.url:
            parser.error('A girls race Results URL or --saved is required.')
        snapshot, artifact = fetch_snapshot(args.url)
    meet, races = parse_snapshot(snapshot, division=args.division, meet_key=args.meet_key)
    show_import(meet, races)
    print('Saved source:', artifact)
    if args.commit:
        print('Imported', save_import(meet, races, artifact, snapshot['url'], replace=args.replace,
              explicit_meet_key=bool(args.meet_key)), 'girls results.')
        build_site()
    else:
        print('Preview only. Add --commit after review; --replace is required to replace an existing race.')


if __name__ == '__main__':
    main()
