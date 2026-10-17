"""Read public structured MileSplit results with explicit race labels and IDs."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re
from urllib.parse import urlencode
from run import ROOT, SETTINGS, download, validate_row, validate_races, import_races, build_site

FIELDS='id,meetId,meetName,teamId,teamName,athleteId,firstName,lastName,gender,genderName,levelId,levelName,divisionId,divisionName,meetResultsId,ageGroupId,ageGroupName,gradYear,eventName,eventCode,eventDistance,round,roundName,heat,units,mark,place,profileUrl,teamProfileUrl,statusCode'


def event_distance(row):
    code=str(row.get('eventCode',''))
    match=re.fullmatch(r'(\d+(?:\.\d+)?)Mile',code,re.I)
    if match:
        return float(Decimal(match[1])*Decimal('1609.344'))
    match=re.fullmatch(r'(\d+)m',code)
    if match:
        value=int(match[1])
        if float(row.get('eventDistance',value))!=value:
            raise ValueError('Conflicting metric distance in source.')
        return value
    raise ValueError(f'Unrecognized XC event: {code}')


def grouping(row):
    return (row.get('gender'),str(row.get('levelId')),str(row.get('divisionId') or ''),row.get('eventCode'),str(row.get('ageGroupId') or ''),row.get('round'),str(row.get('meetResultsId') or ''))


def parse_results(document, meet, race_dates=None, division_labels=None, allow_duplicate_places=False):
    if document.get('_meta',{}).get('status_code')!=200 or not isinstance(document.get('data'),list):
        raise ValueError('Invalid structured results response.')
    if document.get('_links',{}).get('next'):
        raise ValueError('Paginated results must be retrieved in full before importing.')
    records=document['data']
    selected={grouping(r) for r in records if str(r.get('teamId'))==SETTINGS['milesplit_team_id'] and r.get('gender')=='F' and r.get('levelName')=='High School'}
    if not selected:
        raise ValueError('No Sacred Heart high-school girls results found.')
    grouped=defaultdict(list)
    seen=set()
    expected_meet_id=meet['key'].split(':')[-1]
    for row in records:
        if grouping(row) not in selected:
            continue
        if str(row.get('meetId'))!=expected_meet_id:
            raise ValueError('Performance belongs to a different meet.')
        if row.get('roundName')!='Finals':
            raise ValueError('Non-final race requires explicit review.')
        if row['id'] in seen:
            raise ValueError('Duplicate performance ID in response.')
        seen.add(row['id'])
        grouped[grouping(row)].append(row)
    races=[]
    for group, items in grouped.items():
        first=items[0]
        division=(division_labels or {}).get(str(first.get('meetResultsId')), first.get('divisionName') or 'High School')
        if first.get('ageGroupName'):
            division+=' · '+first['ageGroupName']
        race_date=(race_dates or {}).get(division,meet['date'])
        d=date.fromisoformat(race_date)
        rows=[]
        for r in items:
            grad=str(r.get('gradYear') or '')
            grade=d.year+(d.month>=7)+12-int(grad) if grad.isdigit() else None
            mark=str(r.get('mark') or '').strip()
            if not mark:
                raise ValueError('Missing mark; review DNS/DNF before import.')
            record=validate_row({'place':r.get('place'),'bib':'','athlete':str(r.get('firstName') or '').strip()+' '+str(r.get('lastName') or '').strip(),'grade':str(grade) if grade is not None and 1<=grade<=12 else '', 'gender':'F','team':r.get('teamName'),'time':mark,'athlete_id':str(r.get('athleteId') or ''),'status':r.get('statusCode') or ''})
            if record['seconds'] is not None and abs(record['seconds']-float(r['units'])/1000)>.011:
                raise ValueError('Formatted time and numeric time disagree.')
            rows.append(record)
        places={}
        for row in rows:
            if row['place'] is not None:
                places.setdefault(row['place'],set()).add(row['seconds'])
        if not allow_duplicate_places and any(len(times)>1 for times in places.values()):
            raise ValueError(f'Repeated finish places with different times in {division}; review separate races or source errors.')
        rows.sort(key=lambda r:(r['place'] is None,r['place'] or 0))
        races.append({'division':division,'distance_m':event_distance(first),'race_date':race_date,'rows':rows})
    validate_races(races)
    return races


def fetch_schedule(year):
    url=f"https://ky.milesplit.com/api/v1/teams/{SETTINGS['milesplit_team_id']}/schedules?season=cc&year={int(year)}"
    text=download(url)
    folder=ROOT/'data'/'sources'
    folder.mkdir(parents=True,exist_ok=True)
    (folder/f'schedule_{year}.json').write_text(text,encoding='utf-8')
    document=json.loads(text)
    groups=document.get('data',{}).get('monthGroups',[])
    if any(str(group.get('seasonYear'))!=str(int(year)) for group in groups):
        raise ValueError(f'MileSplit returned a different season for {year}; use original meet-history links instead.')
    return document


def fetch_results(meet_id):
    url=f'https://ky.milesplit.com/api/v1/meets/{int(meet_id)}/performances?'+urlencode({'fields':FIELDS})
    text=download(url)
    import hashlib
    artifact=ROOT/'data'/'sources'/f'{meet_id}_{hashlib.sha256(text.encode()).hexdigest()[:12]}_performances.json'
    artifact.parent.mkdir(parents=True,exist_ok=True)
    artifact.write_text(text,encoding='utf-8')
    return json.loads(text),artifact


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('meet_id',type=int)
    parser.add_argument('--name',required=True)
    parser.add_argument('--date',required=True,help='Verified date of the imported races')
    parser.add_argument('--venue',default='')
    parser.add_argument('--commit',action='store_true')
    parser.add_argument('--replace',action='store_true')
    args=parser.parse_args()
    meet={'key':f'milesplit:{args.meet_id}','name':args.name,'date':args.date,'venue':args.venue}
    document,artifact=fetch_results(args.meet_id)
    races=parse_results(document,meet)
    from run import show_import
    show_import(meet,races)
    if args.commit:
        count=import_races(meet,races,'milesplit',f'https://ky.milesplit.com/meets/{args.meet_id}/results?type=formatted',artifact,replace=args.replace)
        print(f'Imported {count} results.')
        build_site()
    else:
        print('Preview only; add --commit after review.')


if __name__=='__main__':
    main()
