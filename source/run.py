"""Portable XC workflow. Python 3.10+; standard library only.

All paths are relative to this file. No imports from the surrounding St. X project.
Source-specific readers feed a common validated race format.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import closing
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import sys
import subprocess
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "sacred_heart.db"
SETTINGS = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
TIGER_URL = "https://ky.milesplit.com/meets/771610-stxxc-tiger-run-2026/results?type=formatted"


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    return " ".join(str(value).casefold().split())


def is_home(team):
    return normalize(team) in {normalize(s) for s in SETTINGS["team_aliases"]}


def initialize(path=DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO metadata VALUES ('schema_version','1');
        CREATE TABLE IF NOT EXISTS meets (
            id INTEGER PRIMARY KEY, meet_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, date TEXT NOT NULL, venue TEXT NOT NULL DEFAULT '',
            sport TEXT NOT NULL DEFAULT 'xc' CHECK(sport='xc'));
        CREATE TABLE IF NOT EXISTS races (
            id INTEGER PRIMARY KEY, meet_id INTEGER NOT NULL REFERENCES meets(id),
            division TEXT NOT NULL, gender TEXT NOT NULL CHECK(gender='F'),
            distance_m INTEGER NOT NULL CHECK(distance_m>0),
            source TEXT NOT NULL, source_url TEXT NOT NULL,
            source_file TEXT NOT NULL, imported_at TEXT NOT NULL,
            UNIQUE(meet_id,division,gender,distance_m));
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            place INTEGER, bib TEXT NOT NULL, athlete TEXT NOT NULL, grade TEXT NOT NULL,
            team TEXT NOT NULL, time_text TEXT NOT NULL, seconds REAL,
            status TEXT NOT NULL, athlete_key TEXT NOT NULL,
            CHECK((status='FINISHED' AND seconds>0 AND place>0)
               OR (status IN ('DNF','DNS','DQ') AND seconds IS NULL)));
        CREATE INDEX IF NOT EXISTS results_race ON results(race_id);
        CREATE INDEX IF NOT EXISTS results_athlete ON results(athlete_key);
        CREATE TABLE IF NOT EXISTS import_runs (
            id INTEGER PRIMARY KEY, imported_at TEXT NOT NULL, meet_id INTEGER NOT NULL,
            source TEXT NOT NULL, source_url TEXT NOT NULL, source_file TEXT NOT NULL,
            sha256 TEXT NOT NULL, races INTEGER NOT NULL, results INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS athlete_source_ids (
            source TEXT NOT NULL, source_athlete_id TEXT NOT NULL,
            athlete_key TEXT NOT NULL, PRIMARY KEY(source,source_athlete_id));
    """)
    columns={r[1] for r in conn.execute('PRAGMA table_info(races)')}
    if 'race_date' not in columns:
        conn.execute('ALTER TABLE races ADD COLUMN race_date TEXT')
    conn.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
    conn.commit()
    return conn


def backup(path=DB):
    path = Path(path)
    if not path.exists():
        return None
    folder = path.parent / "backups"
    folder.mkdir(exist_ok=True)
    target = folder / (path.stem + "_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".db")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as dest:
        source.backup(dest)
    return target


class SourceHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_pre = False
        self.in_title = False
        self.blocks = []
        self.title = ""
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "pre":
            self.in_pre = True
            self.blocks.append("")
        if tag == "h1":
            self.in_title = True
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "br" and self.in_pre:
            self.blocks[-1] += "\n"

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_pre = False
        if tag == "h1":
            self.in_title = False

    def handle_data(self, data):
        if self.in_pre:
            self.blocks[-1] += data
        if self.in_title:
            self.title += data


def seconds(mark):
    match = re.fullmatch(r"(\d{1,3}):([0-5]\d(?:\.\d+)?)", mark)
    if not match:
        raise ValueError(f"Invalid finish time: {mark!r}. Expected minutes:seconds.")
    value = int(match[1]) * 60 + float(match[2])
    if value <= 0:
        raise ValueError("Finish time must be positive.")
    return value


def validate_row(row):
    out = {k: str(row.get(k) or "").strip() for k in
           ("place", "bib", "athlete", "grade", "gender", "team", "time", "status", "athlete_id")}
    if out["gender"].upper() not in ("F", "GIRLS", "FEMALE"):
        raise ValueError("Every row in a selected girls' race must identify female gender.")
    if not out["athlete"] or not out["team"]:
        raise ValueError("Athlete and team are required on every row.")
    status = out["status"].upper() or (out["time"].upper() if out["time"].upper() in ("DNF", "DNS", "DQ") else "FINISHED")
    if status not in ("FINISHED", "DNF", "DNS", "DQ"):
        raise ValueError(f"Unsupported result status: {status}")
    out["status"] = status
    out["seconds"] = seconds(out["time"]) if status == "FINISHED" else None
    out["place"] = int(out["place"]) if out["place"].isdigit() else None
    if status == "FINISHED" and (out["place"] is None or out["place"] <= 0):
        raise ValueError("Every finisher needs a positive overall place.")
    return out


def parse_tabular(text, divisions=("JV", "Varsity")):
    """Read MileSplit's tab-delimited Completed Results, failing on unknown rows."""
    if re.search(r'^\s*Event\s+\d+\s+Girls',text,re.M):
        return parse_hytek(text, divisions)
    wanted = {normalize(d) for d in divisions}
    races = []
    race = None
    header_seen = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = re.fullmatch(r"(Girls|Boys)\s+(\d+(?:\.\d+)?)(k|m)\s+Run\s+(.+)", line, re.I)
        if heading:
            race = None
            header_seen = False
            if heading[1].lower() == "girls" and normalize(heading[4]) in wanted:
                race = {"division": heading[4], "distance_m": int(float(heading[2]) * (1000 if heading[3].lower() == "k" else 1)), "rows": []}
                races.append(race)
            continue
        if race is None:
            continue
        if line.startswith("PLACE\tBIB\tNAME\tGRADE\tGENDER\tTEAM\tMARK"):
            header_seen = True
            continue
        fields = line.split("\t")
        if not header_seen or len(fields) < 7:
            raise ValueError(f"Unrecognized row in {race['division']}: {line[:100]}")
        race["rows"].append(validate_row(dict(zip(("place", "bib", "athlete", "grade", "gender", "team", "time"), fields[:7]))))
    if {normalize(r["division"]) for r in races} != wanted:
        raise ValueError("Not all requested girls' divisions were found. No data imported.")
    return races


def parse_hytek(text, divisions):
    """Single-event HY-TEK export; division must be selected explicitly."""
    events = re.findall(r'^\s*Event\s+\d+\s+(Girls|Boys)\s+(\d+(?:\.\d+)?)(k|m)\s+Run[^\r\n]*',text,re.M)
    if len(events)!=1 or events[0][0]!='Girls' or len(divisions)!=1:
        raise ValueError('This HY-TEK file requires one girls event and an explicit division, e.g. --divisions JV.')
    distance = int(float(events[0][1])*(1000 if events[0][2]=='k' else 1))
    rows=[]
    started=False
    for line in text.splitlines():
        if 'Team Scores' in line:
            break
        if re.search(r'Name\s+Year\s+School\s+Finals',line):
            started=True
            continue
        if not started or not line.strip() or set(line.strip())=={'='}:
            continue
        parts = re.split(r'\s{2,}',line.strip())
        if len(parts)<3:
            raise ValueError(f'Unrecognized HY-TEK result: {line}')
        match = re.fullmatch(r'(\d+)\s+(.+)',parts[0])
        if not match:
            raise ValueError(f'Cannot read HY-TEK place/name: {line}')
        name=match[2]
        grade=''
        if len(parts)>=4 and re.fullmatch(r'\d{1,2}',parts[1]):
            grade=parts.pop(1)
        if ',' in name:
            last,first=name.split(',',1)
            name=first.strip()+' '+last.strip()
        rows.append(validate_row({'place':match[1],'athlete':name,'grade':grade,'gender':'F','team':parts[1],'time':parts[2]}))
    return [{'division':divisions[0],'distance_m':distance,'rows':rows}]


def validate_races(races):
    if not races:
        raise ValueError("No races to import.")
    keys = set()
    for race in races:
        key = (normalize(race["division"]), race["distance_m"])
        if key in keys or not race["division"].strip() or race["distance_m"] <= 0:
            raise ValueError("Duplicate or invalid race identity.")
        keys.add(key)
        if not race["rows"]:
            raise ValueError("Empty race refused.")
        race["rows"] = [validate_row(row) for row in race["rows"]]
        seen = set()
        for row in race["rows"]:
            validate_row(row)
            identity = (normalize(row["team"]), normalize(row["athlete"]), row["bib"])
            if identity in seen:
                raise ValueError("Duplicate runner in source race; review before import.")
            seen.add(identity)


def athlete_key(row, meet_date, source):
    # Raw results lack stable athlete IDs. Estimated graduating cohort prevents
    # many same-name collisions; never expose it as a verified graduation year.
    if row.get("athlete_id"):
        identity = f"{source}:{row['athlete_id']}"
    else:
        grade = row["grade"]
        d = date.fromisoformat(meet_date)
        cohort = str(d.year + (d.month >= 7) + 12 - int(grade)) if grade.isdigit() and 1 <= int(grade) <= 12 else "unknown"
        team = SETTINGS["school"] if is_home(row["team"]) else row["team"]
        identity = f"{normalize(team)}|{normalize(row['athlete'])}|{cohort}"
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def resolve_identity(conn, row, meet_date, source, previous_people):
    source_id=row.get('athlete_id','')
    if source_id:
        saved=conn.execute('SELECT athlete_key FROM athlete_source_ids WHERE source=? AND source_athlete_id=?',(source,source_id)).fetchone()
        if saved:
            return saved[0]
    provisional=athlete_key({**row,'athlete_id':''},meet_date,source)
    candidates=[p for p in previous_people.get(normalize(row['athlete']),[]) if normalize(p['team'])==normalize(row['team']) or (is_home(p['team']) and is_home(row['team']))]
    keys={p['athlete_key'] for p in candidates}
    key=athlete_key(row,meet_date,source)
    if provisional in keys:
        key=provisional
    elif len(keys)==1 and (not row['grade'] or all(not p['grade'] for p in candidates) or row['grade'] in {p['grade'] for p in candidates}):
        key=next(iter(keys))
    elif candidates and (not row['grade'] or is_home(row['team'])):
        raise ValueError(f"Ambiguous athlete identity/grade: {row['athlete']}. Review before import.")
    if source_id:
        other=conn.execute('SELECT source_athlete_id FROM athlete_source_ids WHERE source=? AND athlete_key=? AND source_athlete_id<>?',(source,key,source_id)).fetchone()
        if other:
            raise ValueError(f"Conflicting MileSplit athlete IDs for {row['athlete']}; review before merging.")
        conn.execute('INSERT INTO athlete_source_ids VALUES(?,?,?)',(source,source_id,key))
    return key


def import_races(meet, races, source, source_url, artifact, *, replace=False, db_path=DB):
    date.fromisoformat(meet["date"])
    if not meet["key"].strip() or not meet["name"].strip():
        raise ValueError("Meet key and name are required.")
    validate_races(races)
    artifact = Path(artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    backup(db_path)
    conn = initialize(db_path)
    try:
        with conn:
            existing = conn.execute("SELECT * FROM meets WHERE meet_key=?", (meet["key"],)).fetchone()
            if existing and existing["date"] != meet["date"]:
                raise ValueError("Meet date differs from existing record; review before importing.")
            conn.execute("INSERT OR IGNORE INTO meets(meet_key,name,date,venue) VALUES(?,?,?,?)", (meet["key"],meet["name"],meet["date"],meet.get("venue", "")))
            mid = conn.execute("SELECT id FROM meets WHERE meet_key=?", (meet["key"],)).fetchone()[0]
            previous_people = conn.execute('SELECT DISTINCT athlete,team,grade,athlete_key FROM results JOIN races ON races.id=results.race_id JOIN meets ON meets.id=races.meet_id WHERE substr(meets.date,1,4)=?',(meet['date'][:4],)).fetchall()
            people_by_name = {}
            for person in previous_people:
                people_by_name.setdefault(normalize(person['athlete']), []).append(person)
            previous_people = people_by_name
            stamp = now()
            for race in races:
                race_date=race.get('race_date',meet['date'])
                date.fromisoformat(race_date)
                previous = conn.execute("SELECT id FROM races WHERE meet_id=? AND lower(division)=lower(?) AND gender='F' AND distance_m=?", (mid,race["division"],race["distance_m"])).fetchone()
                if previous and not replace:
                    raise ValueError(f"{race['division']} already exists. Review the source, then explicitly choose replacement.")
                if previous:
                    rid = previous[0]
                    conn.execute("DELETE FROM results WHERE race_id=?",(rid,))
                    conn.execute("UPDATE races SET source=?,source_url=?,source_file=?,imported_at=? WHERE id=?", (source,source_url,artifact.name,stamp,rid))
                else:
                    rid = conn.execute("INSERT INTO races(meet_id,division,gender,distance_m,source,source_url,source_file,imported_at) VALUES(?,?,'F',?,?,?,?,?)", (mid,race["division"],race["distance_m"],source,source_url,artifact.name,stamp)).lastrowid
                for row in race["rows"]:
                    key=resolve_identity(conn,row,race_date,source,previous_people)
                    conn.execute("INSERT INTO results(race_id,place,bib,athlete,grade,team,time_text,seconds,status,athlete_key) VALUES(?,?,?,?,?,?,?,?,?,?)", (rid,row["place"],row["bib"],row["athlete"],row["grade"],row["team"],row["time"],row["seconds"],row["status"],key))
                conn.execute('UPDATE races SET race_date=? WHERE id=?',(race_date,rid))
            count = sum(len(r["rows"]) for r in races)
            conn.execute("INSERT INTO import_runs(imported_at,meet_id,source,source_url,source_file,sha256,races,results) VALUES(?,?,?,?,?,?,?,?)",(stamp,mid,source,source_url,artifact.name,digest,len(races),count))
        return count
    finally:
        conn.close()


def download(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".milesplit.com"):
        raise ValueError("Enter an https MileSplit meet URL.")
    with urlopen(Request(url, headers={"User-Agent":"Mozilla/5.0"}), timeout=45) as response:
        return response.read().decode("utf-8")


def fetch_milesplit(url, divisions):
    match = re.search(r"/meets/(\d+)", url)
    if not match:
        raise ValueError("Expected a MileSplit meet URL.")
    page = download(url)
    parsed = SourceHTML()
    parsed.feed(page)
    date_match = re.search(r"startDate:\s*['\"](\d{4}-\d{2}-\d{2})", page)
    if not date_match:
        raise ValueError("Cannot verify meet date automatically; use reviewed saved-source import.")
    links = list(dict.fromkeys(urljoin(url, link) for link in parsed.links if re.search(r"/results/\d+/raw(?:$|\?)",link)))
    if len(links) != 1:
        raise ValueError("This meet needs individual result-file selection. Use saved-source or reviewed CSV import for now.")
    raw = download(links[0])
    raw_parsed = SourceHTML()
    raw_parsed.feed(raw)
    races = parse_tabular("\n".join(raw_parsed.blocks), divisions)
    folder = ROOT / "data" / "sources"
    folder.mkdir(parents=True,exist_ok=True)
    artifact = folder / f"milesplit_{match[1]}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}.html"
    artifact.write_text(raw,encoding="utf-8")
    meet = {"key":"milesplit:"+match[1],"name":" ".join(parsed.title.split()),"date":date_match[1]}
    return meet,races,links[0],artifact


def show_import(meet,races):
    print(f"\n{meet['date']} | {meet['name']} | {meet['key']}")
    for race in races:
        home = [r for r in race["rows"] if is_home(r["team"])]
        print(f"Girls {race['division']} | {race['distance_m']}m | {len(race['rows'])} results | {len(home)} Sacred Heart")
        for row in home:
            print(f"  {str(row['place']):>4}  {row['athlete']:<28} {row['time']}")


def build_site(db_path=DB, output=None):
    from presentation import build
    output = Path(output or ROOT / "output" / "site")
    conn = initialize(db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT r.*,c.division,c.distance_m,c.source,c.source_url,c.meet_id,m.name AS meet_name,COALESCE(c.race_date,m.date) AS date FROM results r JOIN races c ON c.id=r.race_id JOIN meets m ON m.id=c.meet_id ORDER BY date DESC,c.division,r.place IS NULL,r.place")]
        meets = [dict(m) for m in conn.execute("SELECT * FROM meets ORDER BY date DESC")]
    finally:
        conn.close()
    path = build(rows, meets, output, SETTINGS, is_home)
    print(f"Built Sacred Heart reports: {path}")
    return path


def menu():
    while True:
        print("\nSacred Heart Running\n1 Import MileSplit JV + Varsity\n2 Rebuild website\n3 Preview website\n4 Back up database\n5 Show command-line help (manual imports)\n6 Publish website\n7 Back up/update code on GitHub\nQ Quit")
        choice = input("Choose: ").strip().lower()
        try:
            if choice == "q":
                return
            if choice == "1":
                url = input("MileSplit meet URL: ").strip()
                meet,races,source_url,artifact = fetch_milesplit(url,("JV","Varsity"))
                show_import(meet,races)
                if input("Import these results? (y/n): ").strip().lower() == "y":
                    replace = input("Replace these races if already imported? (y/n): ").strip().lower() == "y"
                    print("Imported",import_races(meet,races,"milesplit",source_url,artifact,replace=replace),"results")
                    build_site()
            elif choice == "2":
                build_site()
            elif choice == "3":
                webbrowser.open(build_site().as_uri())
            elif choice == "4":
                print(backup() or "No database yet.")
            elif choice in ("6", "7"):
                publish_args = [] if choice == "6" else ["--code-only"]
                subprocess.run([sys.executable, str(ROOT / "publish.py"), *publish_args], check=True)
            elif choice == "5":
                print("Read START_HERE.txt, or run: python run.py --help")
        except Exception as exc:
            print(f"Stopped: {exc}\nReview the error before retrying. Existing imports are protected by transactions.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    for name in ("init","build","preview","backup","publish","backup-code"):
        sub.add_parser(name)
    p = sub.add_parser("milesplit")
    p.add_argument("url")
    p.add_argument("--divisions",nargs="+",default=["JV","Varsity"])
    p.add_argument("--commit",action="store_true",help="Without this flag only preview the import")
    p.add_argument("--replace",action="store_true")
    for command in ("saved-milesplit","csv"):
        p = sub.add_parser(command)
        p.add_argument("file",type=Path)
        p.add_argument("--meet-key",required=True,help="Reuse the existing meet key when correcting a race from another source")
        p.add_argument("--name",required=True)
        p.add_argument("--date",required=True)
        p.add_argument("--venue",default="")
        p.add_argument("--source-url",required=True)
        p.add_argument("--commit",action="store_true")
        p.add_argument("--replace",action="store_true")
        if command == "csv":
            p.add_argument("--source",choices=["manual","athletic.net","milesplit"],default="manual")
            p.add_argument("--division",required=True)
            p.add_argument("--distance",type=int,required=True)
        else:
            p.add_argument("--divisions",nargs="+",default=["JV","Varsity"])
    args = parser.parse_args()
    if not args.command:
        menu()
        return
    if args.command == "init":
        initialize().close()
        print(DB)
    elif args.command in ("build","preview"):
        path = build_site()
        if args.command == "preview":
            webbrowser.open(path.as_uri())
    elif args.command in ("publish", "backup-code"):
        publish_args = [] if args.command == "publish" else ["--code-only"]
        subprocess.run([sys.executable, str(ROOT / "publish.py"), *publish_args], check=True)
    elif args.command == "backup":
        print(backup())
    else:
        if args.command == "milesplit":
            meet,races,source_url,artifact = fetch_milesplit(args.url,args.divisions)
            source = "milesplit"
        else:
            meet = {"key":args.meet_key,"name":args.name,"date":args.date,"venue":args.venue}
            artifact = args.file.resolve()
            if args.command == "csv":
                with artifact.open(encoding="utf-8-sig",newline="") as f:
                    rows = [validate_row(r) for r in csv.DictReader(f)]
                races = [{"division":args.division,"distance_m":args.distance,"rows":rows}]
                source = args.source
            else:
                text = artifact.read_text(encoding="utf-8")
                if "<pre" in text.lower():
                    doc = SourceHTML()
                    doc.feed(text)
                    text = "\n".join(doc.blocks)
                races = parse_tabular(text,args.divisions)
                source = "milesplit"
            source_url = args.source_url
            # Store a content-addressed copy so the handoff retains external inputs.
            folder = ROOT / "data" / "sources"
            folder.mkdir(parents=True,exist_ok=True)
            payload = artifact.read_bytes()
            archived = folder / (hashlib.sha256(payload).hexdigest()[:12]+"_"+artifact.name)
            if artifact != archived:
                archived.write_bytes(payload)
            artifact = archived
        validate_races(races)
        show_import(meet,races)
        if args.commit:
            print("Imported",import_races(meet,races,source,source_url,artifact,replace=args.replace),"results")
            build_site()
        else:
            print("Preview only. Add --commit to import after reviewing; --replace to replace existing races.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError,OSError,sqlite3.Error) as exc:
        print(f"Stopped: {exc}",file=sys.stderr)
        sys.exit(1)
