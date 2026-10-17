"""Portable school report presentation, adapted from the St. X report layouts."""
import csv
import html
import json
import re
import unicodedata
from pathlib import Path
import shutil
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
E = lambda value: html.escape(str(value), quote=True)


def distance_label(value):
    for miles in (2, 2.5, 3):
        if abs(value - miles * 1609.344) < .001:
            return f'{miles:g} miles'
    return f'{value:,}m'


def page(title, body, settings):
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)} | {E(settings['site_title'])}</title><link rel="stylesheet" href="style.css"><script src="site.js" defer></script></head><body>
<a class="skip" href="#main">Skip to content</a>
<header class="site-header"><a class="brand" href="index.html"><img src="assets/school-logo.png" alt="Sacred Heart Academy"><span class="brand-section">Running<br>Cross country</span></a><button id="menuOpen" class="menu-open" aria-label="Open navigation" aria-haspopup="dialog" aria-controls="siteMenu" aria-expanded="false">☰</button></header>
<dialog id="siteMenu" aria-labelledby="menuTitle"><div class="menu-layout"><div class="menu-brand"><p>SACRED HEART ACADEMY</p><h2 id="menuTitle">Valkyries.<br><em>Every season.</em></h2></div><div class="menu-content"><button id="menuClose" aria-label="Close navigation">×</button><p class="eyebrow">EXPLORE RUNNING</p><nav aria-label="Primary"><a href="index.html">Home</a><a href="index.html#meets">Meet history</a><a href="index.html#athletes">Athletes</a><a href="data_downloads.html">Data downloads</a></nav></div></div></dialog>
<main id="main">{body}</main><footer>Results cover imported races only. Best times are compared by distance; courses and conditions vary.<br><a href="{E(settings['team_url'])}">MileSplit team profile</a> · <a href="https://sha.shslou.org/">Sacred Heart Academy</a></footer></body></html>'''


def csv_export(path, rows):
    columns = ['date','meet_name','division','distance_m','place','athlete','grade','team','time_text','status','source','source_url']
    with path.open('w',encoding='utf-8-sig',newline='') as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        for row in rows:
            values = [row.get(c, '') for c in columns]
            # Keep spreadsheet text from being interpreted as formulas.
            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v for v in values])


def bests(performances):
    result = {}
    for row in performances:
        if row['seconds'] is not None and (row['distance_m'] not in result or row['seconds'] < result[row['distance_m']]['seconds']):
            result[row['distance_m']] = row
    return result


def athlete_urls(people, output):
    """Persist readable names so later same-name athletes don't change old URLs."""
    registry = output / '.athlete_urls.json'
    names = json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else {}
    used = set(names.values())
    for key, performances in sorted(people.items()):
        if key in names:
            continue
        text = unicodedata.normalize('NFKD',performances[0]['athlete']).encode('ascii','ignore').decode()
        base = re.sub(r'[^a-z0-9]+','-',text.lower()).strip('-') or 'runner'
        slug = base
        if slug in used:
            slug = f'{base}-{key[:8]}'
        while slug in used:
            slug += '-2'
        names[key] = slug
        used.add(slug)
    registry.write_text(json.dumps(names,indent=2)+'\n',encoding='utf-8')
    return {key:'athlete-'+names[key] for key in people}


def build(rows, meets, output, settings, is_home):
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    (output/'assets').mkdir(exist_ok=True)
    shutil.copyfile(ROOT/'assets/school-logo.png',output/'assets/school-logo.png')
    shutil.copyfile(ROOT/'assets/site.css',output/'style.css')
    shutil.copyfile(ROOT/'assets/site.js',output/'site.js')
    (output/'.nojekyll').touch()
    home = [r for r in rows if is_home(r['team'])]
    people = {}
    for row in home:
        people.setdefault(row['athlete_key'],[]).append(row)
    urls = athlete_urls(people, output)
    years = sorted({m['date'][:4] for m in meets},reverse=True)
    best_by_person = {key:bests(perfs) for key,perfs in people.items()}
    # PR flags require a strictly earlier dated result, never just a first mark.
    improved = set()
    for perfs in people.values():
        for row in perfs:
            prior = [r['seconds'] for r in perfs if r['date'] < row['date'] and r['distance_m']==row['distance_m'] and r['seconds'] is not None]
            if prior and row['seconds'] is not None and row['seconds'] < min(prior):
                improved.add(row['id'])

    def badge(row):
        if row['id'] in improved:
            return ' <span class="pr" title="Faster than earlier imported results at this distance">PR</span>'
        return ''

    options = '<option value="all">All seasons</option>'+''.join(f'<option>{y}</option>' for y in years)
    intro = f'''<div class="topbar"><div><p class="eyebrow">SACRED HEART ACADEMY · LOUISVILLE, KY</p><h1>Sacred Heart Running</h1><p class="subline">Cross country meet results and athlete history</p></div><a class="button" href="data_downloads.html">Data downloads ↓</a></div><div class="filterbar"><span class="pill">Girls cross country</span><label>Season <select id="season">{options}</select></label><span class="muted">{len(meets)} meets · {len(people)} athletes · {len(home)} performances</span></div>'''
    content = intro+'<div class="home-grid"><section class="panel" id="meets"><div class="panel-heading"><h2>Meets</h2><span class="count">Sacred Heart</span></div><div class="meet-list">'
    for year in years:
        content += f'<section class="meet-year" data-year="{year}"><h3 class="year-bar">{year}</h3>'
        for meet in [m for m in meets if m['date'].startswith(year)]:
            content += f'<a class="meet-item" href="meet-{meet["id"]}.html"><span class="meet-rail">XC</span><span class="meet-main"><small>{E(meet["date"])}</small><strong>{E(meet["name"])}</strong><small>{E(meet["venue"])}</small></span><span class="arrow">→</span></a>'
        content += '</section>'
    content += '<p class="coverage">Showing imported meets. More seasons will appear as results are added.</p></div></section><section class="panel" id="athletes"><div class="panel-heading"><h2>Athletes</h2><span class="count" id="athleteCount">'+str(len(people))+'</span></div><div class="athlete-controls"><label>Find an athlete<input id="athleteSearch" type="search" placeholder="Search by name…"></label><label>Sort<select id="athleteSort"><option value="name">Name</option><option value="grade">Latest grade</option><option value="best">5K best</option></select></label></div><p class="coverage">Grades are from the latest imported race. Bests cover imported results only.</p><div class="athlete-cards">'
    for key,perfs in sorted(people.items(),key=lambda item:item[1][0]['athlete']):
        latest = perfs[0]
        five = best_by_person[key].get(5000)
        mark = E(five['time_text']) if five else '—'
        seasons = ' '.join(sorted({r['date'][:4] for r in perfs}))
        content += f'<a class="athlete-card" href="{urls[key]}.html" data-name="{E(latest["athlete"].lower())}" data-grade="{E(latest["grade"] or 0)}" data-best="{five["seconds"] if five else 999999}" data-seasons="{seasons}"><span class="grade-bubble"><small>GRADE</small>{E(latest["grade"] or "—")}</span><span><strong>{E(latest["athlete"])}</strong><small>5K best <b>{mark}</b></small></span></a>'
        marks = ''.join(f'<div class="best-card"><small>{distance_label(distance)} · Best imported</small><strong>{E(row["time_text"])}</strong><span>{E(row["date"])}</span></div>' for distance,row in sorted(best_by_person[key].items()))
        body = f'<a class="back" href="index.html#athletes">← All athletes</a><p class="eyebrow">SACRED HEART · CROSS COUNTRY</p><h1>{E(latest["athlete"])}</h1><p class="subline">Grade {E(latest["grade"] or "not listed")} at latest imported race · {len(perfs)} performances</p><div class="best-grid">{marks}</div><section class="panel"><div class="panel-heading"><h2>Race history</h2><a href="{urls[key]}.csv" download>Download CSV ↓</a></div><div class="table-wrap"><table><thead><tr><th>Date</th><th>Meet</th><th>Race</th><th>Distance</th><th>Place</th><th>Time</th></tr></thead><tbody>'
        for r in perfs:
            body += f'<tr><td>{E(r["date"])}</td><td><a href="meet-{r["meet_id"]}.html">{E(r["meet_name"])}</a></td><td>{E(r["division"])}</td><td>{distance_label(r["distance_m"])}</td><td>{E(r["place"] or "—")}</td><td>{E(r["time_text"])}{badge(r)}</td></tr>'
        body += '</tbody></table></div></section><p class="coverage">PR marks require a faster time than an earlier imported race at the same distance. A first recorded result is a baseline.</p>'
        (output/f'{urls[key]}.html').write_text(page(latest['athlete'],body,settings),encoding='utf-8')
        csv_export(output/f'{urls[key]}.csv',perfs)
    content += '</div><p id="noAthletes" hidden>No athletes match these filters.</p></section></div>'
    (output/'index.html').write_text(page('Home',content,settings),encoding='utf-8')

    downloads = '<h1>Data downloads</h1><p class="subline">CSV files open in Excel. Downloads include source links and all imported seasons.</p><div class="download-list"><a class="button" href="sacred-heart-results.csv" download>Sacred Heart results ↓</a><a class="button secondary" href="all-results.csv" download>All race results ↓</a></div><h2>By meet</h2>'
    csv_export(output/'sacred-heart-results.csv',home)
    csv_export(output/'all-results.csv',rows)
    for meet in meets:
        selected = [r for r in rows if r['meet_id']==meet['id']]
        races = list(dict.fromkeys(r['race_id'] for r in selected))
        download_name = f'meet-{meet["id"]}.csv'
        csv_export(output/download_name,selected)
        downloads += f'<p><a href="{download_name}" download>{E(meet["date"])} · {E(meet["name"])}</a></p>'
        body = f'<a class="back" href="index.html#meets">← All meets</a><div class="topbar"><div><p class="date">{E(meet["date"])}</p><h1>{E(meet["name"])}</h1><p class="subline">{E(meet["venue"])} · Girls cross country</p></div><a class="button" href="{download_name}" download>Download results ↓</a></div><div class="filterbar"><label><input type="checkbox" id="homeOnly"> Sacred Heart only</label><span class="muted">Sacred Heart runners are highlighted</span></div><div class="tabs" role="group" aria-label="Race filter"><button class="tab active" data-race="all" aria-pressed="true">All races</button>'
        for rid in races:
            first = next(r for r in selected if r['race_id']==rid)
            body += f'<button class="tab" data-race="{rid}" aria-pressed="false">{E(first["division"])}</button>'
        body += '</div>'
        for rid in races:
            race = [r for r in selected if r['race_id']==rid]
            first = race[0]
            source = first['source_url']
            source_link = f'<a href="{E(source)}">Source results ↗</a>' if urlparse(source).scheme in ('https','http') else E(source)
            body += f'<section class="race-section panel" data-race-id="{rid}"><div class="panel-heading"><h2>Girls {E(first["division"])} · {distance_label(first["distance_m"])}</h2>{source_link}</div><p class="race-summary">{len(race)} results · {sum(is_home(r["team"]) for r in race)} Sacred Heart runners</p><div class="table-wrap"><table><thead><tr><th>Place</th><th>Athlete</th><th>Grade</th><th>Team</th><th>Time</th></tr></thead><tbody>'
            for r in race:
                own = is_home(r['team'])
                name = f'<a href="{urls[r["athlete_key"]]}.html">{E(r["athlete"])}</a>' if own else E(r['athlete'])
                body += f'<tr class="{"home" if own else "other"}"><td>{E(r["place"] or "—")}</td><td>{name}</td><td>{E(r["grade"])}</td><td>{E(r["team"])}</td><td>{E(r["time_text"])}{badge(r) if own else ""}</td></tr>'
            body += '</tbody></table></div></section>'
        (output/f'meet-{meet["id"]}.html').write_text(page(meet['name'],body,settings),encoding='utf-8')
    (output/'data_downloads.html').write_text(page('Data downloads',downloads,settings),encoding='utf-8')
    current = {f'{urls[key]}.{ext}' for key in people for ext in ('html','csv')} | {f'meet-{m["id"]}.{ext}' for m in meets for ext in ('html','csv')}
    for pattern in ('athlete-*.html','meet-*.html','athlete-*.csv','meet-*.csv'):
        for path in output.glob(pattern):
            if path.name not in current:
                path.unlink()
    return output/'index.html'
