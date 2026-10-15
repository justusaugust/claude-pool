import copy
import json
from pathlib import Path
import tempfile
import unittest
from validation import archive, evaluate, load, seed

NOW = 1900000000000


def snapshot(at, pct, reset, *, source='live', partial=False, seconds=600):
    accounts = [{'name': name, 'label': name, 'id': 'private-id', 'seenAt': at, 'status': 'ready' if pct < 95 else 'limited',
                 'fiveHour': {'pct': pct, 'resetsAt': reset}, 'sevenDay': {'pct': 10, 'resetsAt': NOW + 7 * 86400000}}
                for name in ('gmail', 'team')]
    forecast = {'seconds': seconds if pct < 95 else None, 'threshold': 95, 'partial': partial,
                'accounts': [{'name': a['name'], 'windows': {'fiveHour': {'ratePerHour': 10}, 'sevenDay': {'ratePerHour': 0}}} for a in accounts]}
    return {'at': at, 'source': source, 'accounts': accounts, 'forecast': forecast}


class ValidationTests(unittest.TestCase):
    def test_reset_projection_is_frozen_and_does_not_fit_target(self):
        origin = snapshot(NOW, 90, NOW + 1800000)
        target = snapshot(NOW + 3600000, 5, NOW + 19800000)
        a = evaluate([origin, target])
        self.assertEqual(a['observedResets'], 2)
        self.assertEqual(a['groups'][0]['maePoints'], 0)
        self.assertEqual(a['poolOutcomes']['reset censored'], 1)
        target['accounts'][0]['fiveHour']['pct'] = 25
        b = evaluate([origin, target])
        self.assertEqual([f['prediction'] for f in a['folds']], [f['prediction'] for f in b['folds']])
        self.assertNotEqual(a['folds'][0]['actual'], b['folds'][0]['actual'])

    def test_pool_scores_observed_depletion_interval_and_not_just_one_account(self):
        rows = [snapshot(NOW, 90, NOW + 86400000), snapshot(NOW + 300000, 94, NOW + 86400000),
                snapshot(NOW + 600000, 96, NOW + 86400000)]
        report = evaluate(rows)
        self.assertEqual(report['completedPoolForecasts'][0]['actualSecondsInterval'], [300, 600])
        self.assertEqual(report['completedPoolForecasts'][0]['errorSeconds'], 0)
        rows[-1]['accounts'][1]['fiveHour']['pct'] = 94
        self.assertEqual(evaluate(rows)['completedPoolForecasts'], [])

    def test_partial_stale_missing_and_routing_changes_are_not_successes(self):
        origin = snapshot(NOW, 90, NOW + 86400000, partial=True)
        future = snapshot(NOW + 600000, 96, NOW + 86400000)
        report = evaluate([origin, future])
        self.assertEqual(report['poolOutcomes']['partial forecast'], 1)
        origin['forecast']['partial'] = False
        for mutation, outcome in [(lambda r: r['accounts'][0].update(status='paused'), 'routing changed'),
                                  (lambda r: r['accounts'][0].update(seenAt=NOW - 1), 'sampling gap'),
                                  (lambda r: r['accounts'][0].update(sevenDay=None), 'incomplete readings')]:
            changed = copy.deepcopy(future); mutation(changed)
            result = evaluate([origin, changed])
            self.assertEqual(result['poolOutcomes'][outcome], 1)
            self.assertEqual(result['completedPoolForecasts'], [])
        future['accounts'][0]['fiveHour']['pct'] = 60
        self.assertEqual(evaluate([origin, future])['poolOutcomes']['quota correction'], 1)

    def test_rounding_does_not_count_as_reset_and_second_reset_is_not_projected(self):
        origin = snapshot(NOW, 30, NOW + 60000)
        future = snapshot(NOW + 3600000, 40, NOW + 59000)
        self.assertEqual(evaluate([origin, future])['observedResets'], 0)
        self.assertFalse(any(f['window'] == 'fiveHour' for f in evaluate([origin, future])['folds']))
        future = snapshot(NOW + 24 * 3600000, 5, NOW + 25 * 3600000)
        result = evaluate([origin, future])
        self.assertFalse(any(f['window'] == 'fiveHour' for f in result['folds']))

    def test_archive_survives_partial_line_and_only_saves_allowed_fields(self):
        row = snapshot(NOW, 30, NOW + 3600000)
        row['accounts'][0]['credential'] = 'fixture-secret'
        row['forecast']['credential'] = 'fixture-secret'
        with tempfile.TemporaryDirectory() as folder:
            archive(folder, row['accounts'], row['forecast'], NOW)
            path = next(Path(folder).glob('*.jsonl'))
            with path.open('a') as file: file.write('{broken')
            archive(folder, row['accounts'], row['forecast'], NOW + 600000)
            rows, rejected = load(folder)
            self.assertEqual(len(rows), 2); self.assertEqual(rejected, 1)
            self.assertNotIn('private-id', path.read_text()); self.assertNotIn('fixture-secret', path.read_text())

    def test_seed_replays_past_only_and_never_claims_historical_availability(self):
        with tempfile.TemporaryDirectory() as folder:
            evidence = Path(folder) / 'evidence.json'
            evidence.write_text(json.dumps({'history': [{'label': 'Gmail', 'samples': [
                {'at': NOW + i * 600000, 'fiveHour': {'pct': 10 + i * 5, 'resetsAt': NOW + 86400000}}
                for i in range(4)]}]}))
            dest = Path(folder) / 'history'; seed(dest, evidence)
            rows, _ = load(dest)
            self.assertEqual(len(rows), 4); self.assertTrue(all(r['source'] == 'replay' for r in rows))
            self.assertTrue(all(r['accounts'][0]['status'] == 'unknown' for r in rows))
            self.assertEqual(evaluate(rows)['completedPoolForecasts'], [])
            seed(dest, evidence); self.assertEqual(len(load(dest)[0]), 4)


if __name__ == '__main__': unittest.main()
