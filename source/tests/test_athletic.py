import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import athletic

URL = 'https://www.athletic.net/CrossCountry/meet/286720/results/1142183'


def snapshot():
    return {'url': URL, 'requested_url': URL, 'heading': 'Womens 5,000 Meters JV',
            'event': {'url': URL, 'name': 'Metro JV', 'startDate': '2026-10-22',
                      'location': {'name': 'Champions Park'}},
            'rows': [{'place': '1', 'athlete': 'Test Runner', 'team': 'Sacred Heart',
                      'time': '19:39.9', 'stats': 'PB · Yr: 12 · +1pts',
                      'profile_url': 'https://www.athletic.net/athlete/123/cross-country'}]}


class AthleticTests(unittest.TestCase):
    def test_grade_source_identity_and_exact_miles(self):
        data = snapshot()
        data['heading'] = 'Girls 3 Miles JV'
        meet, races = athletic.parse_snapshot(data)
        self.assertAlmostEqual(races[0]['distance_m'], 4828.032)
        row = races[0]['rows'][0]
        self.assertEqual(str(row['grade']), '12')
        self.assertEqual(row['athlete_id'], '123')
        self.assertEqual(meet['date'], '2026-10-22')

    def test_wrong_race_gender_level_and_metadata_rejected(self):
        for key, value in [('heading', 'Mens 5,000 Meters JV'),
                           ('heading', 'Girls 3,000 Meters MS'),
                           ('url', URL.replace('1142183', '1142182')),
                           ('event', {'url': URL.replace('286720', '123')})]:
            with self.subTest(key=key, value=value):
                data = snapshot()
                data[key] = value
                with self.assertRaises(ValueError):
                    athletic.parse_snapshot(data)
        with self.assertRaises(ValueError):
            athletic.race_url('https://www.athletic.net/CrossCountry/meet/286720/info')

    def test_missing_home_and_conflicting_places_rejected(self):
        data = snapshot()
        data['rows'][0]['team'] = 'Other School'
        with self.assertRaises(ValueError):
            athletic.parse_snapshot(data)
        data = snapshot()
        other = copy.deepcopy(data['rows'][0])
        other.update(athlete='Other Runner', time='20:00.0', profile_url='')
        data['rows'].append(other)
        with self.assertRaises(ValueError):
            athletic.parse_snapshot(data)

    def test_import_replacement_and_cross_source_duplicate_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'test.db'
            artifact = Path(directory) / 'source.json'
            artifact.write_text(json.dumps(snapshot()), encoding='utf-8')
            meet, races = athletic.parse_snapshot(snapshot())
            self.assertEqual(athletic.save_import(meet, races, artifact, URL, db_path=db), 1)
            with self.assertRaises(ValueError):
                athletic.save_import(meet, races, artifact, URL, db_path=db)
            self.assertEqual(athletic.save_import(meet, races, artifact, URL, replace=True, db_path=db), 1)
            meet['key'] = 'different-source:123'
            with self.assertRaises(ValueError):
                athletic.save_import(meet, races, artifact, URL, db_path=db)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM results').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
