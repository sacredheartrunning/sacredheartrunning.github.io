import importlib.util
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec = importlib.util.spec_from_file_location("sacred_heart_app",ROOT / "run.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

SOURCE = """Girls 5k Run Varsity
PLACE\tBIB\tNAME\tGRADE\tGENDER\tTEAM\tMARK\tHEAT\tWIND
1\t101\tRunner One\t9\tf\tSacred Heart\t19:00.12\t1\t
2\t102\tRunner Two\t10\tf\tOther School\t20:00.00\t1\t
Boys 5k Run Varsity
PLACE\tBIB\tNAME\tGRADE\tGENDER\tTEAM\tMARK\tHEAT\tWIND
1\t103\tBoy Runner\t9\tm\tOther School\t16:00.00\t1\t
Girls 5k Run JV
PLACE\tBIB\tNAME\tGRADE\tGENDER\tTEAM\tMARK\tHEAT\tWIND
1\t104\tRunner Three\t9\tf\tSacred Heart\t22:00.00\t1\t
Girls 3k Run MS
PLACE\tBIB\tNAME\tGRADE\tGENDER\tTEAM\tMARK\tHEAT\tWIND
1\t105\tYounger Runner\t7\tf\tOther School\t13:00.00\t1\t
"""


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "data" / "test.db"
        self.source = self.root / "source.txt"
        self.source.write_text(SOURCE,encoding="utf-8")
        self.meet = {"key":"milesplit:1","name":"Test Meet","date":"2026-08-22"}

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, races=None, **kwargs):
        return app.import_races(self.meet,races or app.parse_tabular(SOURCE),"milesplit","https://ky.milesplit.com/meets/1/results",self.source,db_path=self.db,**kwargs)

    def count(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT count(*) FROM results").fetchone()[0]

    def test_only_selected_girls_races(self):
        races = app.parse_tabular(SOURCE)
        self.assertEqual([len(r["rows"]) for r in races],[2,1])
        self.assertEqual(self.load(),3)

    def test_reimport_requires_explicit_replace(self):
        self.load()
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(self.count(),3)
        self.load(replace=True)
        self.assertEqual(self.count(),3)
        self.assertTrue(list((self.db.parent / "backups").glob("*.db")))

    def test_replacement_removes_stale_results_only_in_selected_race(self):
        self.load()
        race = app.parse_tabular(SOURCE,("Varsity",))
        race[0]["rows"] = race[0]["rows"][:1]
        self.load(race,replace=True)
        self.assertEqual(self.count(),2)

    def test_transaction_rolls_back_if_later_race_exists(self):
        self.load(app.parse_tabular(SOURCE,("JV",)))
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(self.count(),1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM races").fetchone()[0],1)

    def test_bad_gender_time_and_duplicate_rejected(self):
        for bad in (SOURCE.replace("19:00.12","19:99.12"),SOURCE.replace("101\tRunner One\t9\tf","101\tRunner One\t9\tm")):
            with self.assertRaises(ValueError):
                app.parse_tabular(bad)
        races = app.parse_tabular(SOURCE)
        races[0]["rows"].append(races[0]["rows"][0].copy())
        with self.assertRaises(ValueError):
            self.load(races)
        self.assertFalse(self.db.exists())

    def test_missing_division_and_unknown_format_fail_closed(self):
        with self.assertRaises(ValueError):
            app.parse_tabular(SOURCE,("Freshman",))
        with self.assertRaises(ValueError):
            app.parse_tabular(SOURCE.replace("19:00.12\t1", "bad mark\t1"))

    def test_cross_source_correction_replaces_same_race(self):
        self.load()
        races = app.parse_tabular(SOURCE,("JV",))
        races[0]["rows"][0]["time"] = "21:00.00"
        races[0]["rows"][0]["seconds"] = 1260.0
        app.import_races(self.meet,races,"manual","Screenshot supplied by coach",self.source,replace=True,db_path=self.db)
        self.assertEqual(self.count(),3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT source FROM races WHERE division='JV'").fetchone()[0],"manual")

    def test_athlete_cohort_matching_across_seasons(self):
        row = app.parse_tabular(SOURCE)[0]["rows"][0]
        old = app.athlete_key(row,"2026-08-22","milesplit")
        row["grade"] = "10"
        self.assertEqual(old,app.athlete_key(row,"2027-08-22","milesplit"))
        self.assertNotEqual(old,app.athlete_key(row,"2026-08-22","milesplit"))

    def test_local_site_links_and_html_escaping(self):
        self.meet["name"] = 'Test <Meet> & "Run"'
        self.load()
        out = self.root / "website"
        app.build_site(self.db,out)
        self.assertIn("Test &lt;Meet&gt;",(out / "index.html").read_text(encoding="utf-8"))
        import re
        for page in out.glob("*.html"):
            for href in re.findall(r'href="([^"]+)"',page.read_text(encoding="utf-8")):
                if not href.startswith("https:"):
                    self.assertTrue((out / href.split("#")[0]).exists(),href)
        self.assertEqual(len(list(out.glob("athlete-*.html"))),2)

    def test_backup_can_be_restored(self):
        self.load()
        saved = app.backup(self.db)
        with closing(sqlite3.connect(saved)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(conn.execute("SELECT count(*) FROM results").fetchone()[0],3)

    def test_readable_urls_handle_collisions_and_remain_stable(self):
        from presentation import athlete_urls
        first={'a'*24:[{'athlete':'Zoë O’Neil'}]}
        urls=athlete_urls(first,self.root)
        self.assertEqual(urls['a'*24],'athlete-zoe-oneil')
        both={**first,'b'*24:[{'athlete':'Zoë O’Neil'}]}
        updated=athlete_urls(both,self.root)
        self.assertEqual(updated['a'*24],urls['a'*24])
        self.assertNotEqual(updated['a'*24],updated['b'*24])

    def test_missing_grade_reuses_unique_same_season_athlete(self):
        self.load()
        self.meet={'key':'manual:2','name':'Second','date':'2026-08-25'}
        race=app.parse_tabular(SOURCE,('Varsity',))
        race[0]['rows'][0]['grade']=''
        self.load(race)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(DISTINCT athlete_key) FROM results WHERE athlete='Runner One'").fetchone()[0],1)

    def test_hytek_requires_explicit_division_and_normalizes_names(self):
        source='Event 1  Girls 3k Run CC\n    Name                    Year School                  Finals  Points\n  1 One, Runner                 Sacred Heart          12:16.70    1\nTeam Scores\n'
        with self.assertRaises(ValueError):
            app.parse_tabular(source)
        race=app.parse_tabular(source,('JV',))[0]
        self.assertEqual(race['distance_m'],3000)
        self.assertEqual(race['rows'][0]['athlete'],'Runner One')

    def test_pr_requires_earlier_result_and_downloads_preserve_counts(self):
        import csv
        self.load()
        out = self.root / "website"
        app.build_site(self.db,out)
        self.assertNotIn('class="pr"',(out / 'meet-1.html').read_text(encoding='utf-8'))
        self.meet = {"key":"milesplit:2","name":"Later Meet","date":"2026-09-01"}
        races = app.parse_tabular(SOURCE,("Varsity",))
        races[0]['rows'][0]['time'] = '18:45.00'
        self.load(races)
        app.build_site(self.db,out)
        self.assertIn('class="pr"',(out / 'meet-2.html').read_text(encoding='utf-8'))
        with (out / 'sacred-heart-results.csv').open(encoding='utf-8-sig',newline='') as file:
            self.assertEqual(len(list(csv.DictReader(file))),3)


    def test_source_ids_preserve_existing_history(self):
        self.load()
        with closing(sqlite3.connect(self.db)) as conn:
            original=conn.execute("SELECT athlete_key FROM results WHERE athlete='Runner One'").fetchone()[0]
        self.meet={"key":"milesplit:2","name":"Second","date":"2026-09-05"}
        races=app.parse_tabular(SOURCE,("Varsity",))
        races[0]['rows'][0]['athlete_id']='12345'
        self.load(races)
        with closing(sqlite3.connect(self.db)) as conn:
            keys=conn.execute("SELECT DISTINCT athlete_key FROM results WHERE athlete='Runner One'").fetchall()
            self.assertEqual(keys,[(original,)])
            self.assertEqual(conn.execute('SELECT athlete_key FROM athlete_source_ids').fetchone()[0],original)

    def test_structured_selection_dates_distances_and_time_validation(self):
        import milesplit
        from presentation import distance_label
        row=dict(id=1,meetId=1,teamId=3710,gender='F',levelId=1,levelName='High School',divisionId=1,divisionName='Varsity',eventCode='3Mile',eventDistance=4828,round=1,roundName='Finals',athleteId=100,firstName='Runner',lastName='One',gradYear=2030,mark='19:00.12',units=1140120,place=1,teamName='Sacred Heart')
        doc={'_meta':{'status_code':200},'data':[row,{**row,'id':2,'athleteId':101,'teamId':99,'teamName':'Other School','lastName':'Two','place':2},{**row,'id':3,'gender':'M'}]}
        races=milesplit.parse_results(doc,self.meet,{'Varsity':'2026-08-23'})
        self.assertEqual(len(races[0]['rows']),2)
        self.assertEqual(races[0]['race_date'],'2026-08-23')
        self.assertEqual(races[0]['distance_m'],4828.032)
        self.assertEqual(distance_label(races[0]['distance_m']),'3 miles')
        self.assertEqual(races[0]['rows'][0]['grade'],'9')
        row['units']=999
        with self.assertRaises(ValueError):milesplit.parse_results(doc,self.meet)


    def test_code_backup_preserves_website_and_excludes_data(self):
        import publish
        from unittest.mock import patch
        import shutil
        for path in publish.code_files(ROOT):
            target=self.root/path.relative_to(ROOT)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target)
        (self.root/'data').mkdir(exist_ok=True)
        (self.root/'data'/'private.db').write_text('private')
        (self.root/'.env').write_text('secret')
        repo=self.root/'publishing'
        (repo/'.git').mkdir(parents=True)
        (repo/'index.html').write_text('existing website')
        def fake_git(repo,*args):
            if args[:2]==('remote','get-url'):return 'https://example.test/repo.git'
            if args[:2]==('branch','--show-current'):return 'main'
            return ''
        with patch.object(publish,'ROOT',self.root), patch.object(publish,'SETTINGS',{'publishing_repository':'https://example.test/repo.git'}), patch.object(publish,'git',side_effect=fake_git), patch.object(publish,'build_site') as build, patch('sys.argv',['publish.py','--code-only']):
            publish.main()
            build.assert_not_called()
        self.assertEqual((repo/'index.html').read_text(),'existing website')
        self.assertTrue((repo/'source'/'run.py').exists())
        self.assertFalse((repo/'source'/'data').exists())
        self.assertFalse((repo/'source'/'.env').exists())


    def test_structured_race_files_stay_separate_and_ties_are_preserved(self):
        import milesplit
        row=dict(id=1,meetId=1,meetResultsId=10,teamId=3710,gender='F',levelId=1,levelName='High School',divisionId=None,divisionName=None,eventCode='5000m',eventDistance=5000,round='f',roundName='Finals',athleteId=100,firstName='Runner',lastName='One',gradYear=2030,mark='19:00.12',units=1140120,place=1,teamName='Sacred Heart')
        other={**row,'id':2,'meetResultsId':20,'athleteId':101,'lastName':'Two'}
        doc={'_meta':{'status_code':200},'data':[row,other]}
        with self.assertRaises(ValueError):milesplit.parse_results(doc,self.meet)
        races=milesplit.parse_results(doc,self.meet,division_labels={'10':'Varsity','20':'JV'})
        self.assertEqual([len(r['rows']) for r in races],[1,1])
        other['meetResultsId']=10
        races=milesplit.parse_results(doc,self.meet)
        self.assertEqual(len(races[0]['rows']),2)
        other['mark']='19:01.00';other['units']=1141000
        with self.assertRaises(ValueError):milesplit.parse_results(doc,self.meet)


    def test_schedule_rejects_silent_fallback_to_current_year(self):
        import milesplit
        from unittest.mock import patch
        payload='{"data":{"monthGroups":[{"seasonYear":"2026","items":[]}]}}'
        with patch.object(milesplit,'ROOT',self.root), patch.object(milesplit,'download',return_value=payload):
            with self.assertRaisesRegex(ValueError,'different season'):
                milesplit.fetch_schedule(2015)
        self.assertTrue((self.root/'data'/'sources'/'schedule_2015.json').exists())


if __name__ == "__main__":
    unittest.main()
