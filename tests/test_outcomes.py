from fixture_mind import TEST_ZONE
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from steward import jobs
from steward.state import writer
from test_timeline import NOW


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fixture_mind import install
        install(self, Path(self.tmp.name))
        self.store = self.enterContext(writer(Path(self.tmp.name)/'state', initialize=True))
        self.db = self.store.db
        jobs.change(self.db, 'tick', NOW-timedelta(days=20), 'tick')

    def report(self, payload=None):
        from steward.outcomes import weekly
        def runner(argv):
            if 'contract' in argv:
                return json.dumps({'$id':'gsd.runs.v1'})
            return json.dumps(payload or dict(schema='gsd.runs.v1', since='2026-10-19', runs=[]))
        return {r['metric']: r for r in weekly(self.db, '2026-10-18', now=NOW, runner=runner)['rows']}

    def test_default_week_uses_athens_sunday_including_dst_overlap(self):
        from steward.outcomes import weekly
        for at, key, since in [
            ('2026-10-24T20:59:59+00:00', '2026-10-18', '2026-10-18T00:00:00+03:00'),
            ('2026-10-24T21:00:00+00:00', '2026-10-25', '2026-10-25T00:00:00+03:00'),
            ('2026-10-25T00:30:00+00:00', '2026-10-25', '2026-10-25T00:00:00+03:00'),
            ('2026-10-25T01:30:00+00:00', '2026-10-25', '2026-10-25T00:00:00+03:00'),
        ]:
            with self.subTest(at=at):
                report = weekly(self.db, now=datetime.fromisoformat(at), runner=lambda argv: '{}')
                self.assertEqual((report['week'], report['since']), (key, since))
        report = weekly(self.db, '2026-10-18', now=datetime.fromisoformat('2026-10-25T06:00:00+02:00'),
                        runner=lambda argv: '{}')
        self.assertEqual(report['until'], '2026-10-25T00:00:00+03:00')

    def test_reply_is_find_action_but_questions_and_kept_are_unknown(self):
        cid = jobs.change(self.db, 'notify', NOW)
        for n in (1, 2):
            self.db.execute("INSERT INTO outbox(id,change_id,topic,kind,text,state,message_id,sent_at) VALUES (?,?,'Finds','result','find','sent',?,?)", (str(n), cid, n, NOW.isoformat()))
        for n in (1, 2):
            self.db.execute("INSERT INTO inbox(update_id,message_id,at,text,reply_to) VALUES (?,?,?,'yes',1)", (n, 10+n, NOW.isoformat()))
        rows = self.report()
        self.assertEqual((rows['finds_acted_on']['count'], rows['finds_acted_on']['total']), (1, 2))
        self.assertEqual(rows['messages_answered']['status'], 'unknown')
        self.assertEqual(rows['calendar_kept']['status'], 'unknown')
        self.assertEqual(rows['r1_amendment_rate']['status'], 'unknown')

    def test_calendar_owner_transitions_not_steward_moves(self):
        payload = dict(start=NOW.isoformat(), end=(NOW+timedelta(hours=1)).isoformat())
        for version, state, actor in [(1,'applied','model'), (2,'applied','model'), (3,'applied','tick'), (4,'removed','tick')]:
            cid = jobs.change(self.db, 'tick' if actor=='tick' else 'block apply', NOW, actor)
            payload['start'] = (NOW+timedelta(minutes=version)).isoformat()
            self.db.execute('INSERT INTO blocks VALUES (?,?,?,?,?,?)', ('a',version,cid,'test',state,json.dumps(payload)))
        rows = self.report()
        self.assertEqual(rows['calendar_moved']['count'], 1)
        self.assertEqual(rows['calendar_deleted']['count'], 1)

    def test_r1_contract_validation_and_null_never_zero(self):
        def payload(counts):
            return dict(schema='gsd.runs.v1', since='2026-10-19', runs=[dict(run='one',since='2026-10-19T00:00:00Z',until=NOW.isoformat(), counts=counts,steps=['a'],error=None,missing=[])])
        row = self.report(payload(dict(a_changes=10,r1_amended=2)))['r1_amendment_rate']
        self.assertEqual((row['count'],row['total'],row['rate']), (2,10,0.2))
        row = self.report(payload(dict(a_changes=10,r1_amended=None)))['r1_amendment_rate']
        self.assertEqual(row['status'], 'unknown')
        row = self.report({'schema':'wrong'})['r1_amendment_rate']
        self.assertEqual(row['status'], 'unknown')

    def test_cli_is_read_only_and_missing_producer_contract_is_unknown(self):
        from contextlib import redirect_stdout
        import io
        from unittest.mock import patch
        from steward.cli import main
        self.db.commit()
        path = self.store.root / 'state.sqlite'
        # Reads must work on the live schema without triggering schema 14.
        self.db.execute('PRAGMA user_version=13')
        output = io.StringIO()
        with patch('steward.outcomes.r1', return_value=dict(metric='r1_amendment_rate',status='unknown',reason='no contract')), redirect_stdout(output):
            self.assertEqual(main(['journal','--state',str(self.store.root),'--outcomes','--week','2026-10-18','--json']), 0)
        self.assertEqual(json.loads(output.getvalue())['week'], '2026-10-18')
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 13)
        from steward.outcomes import r1
        self.assertEqual(r1('2026-10-19', NOW-timedelta(days=1), NOW, NOW, lambda argv: '{}')['reason'], 'no contract')

    def bundle(self, key, start, closures, demand='grants'):
        from steward import pages
        cid = jobs.change(self.db, 'block demand add', start - timedelta(days=1), 'model')
        self.db.execute('INSERT OR IGNORE INTO demands VALUES (?,?,?,?,?)', (demand, 0, cid, json.dumps(dict(
            title='Grants', minutes=60, kind='other', transparency='transparent', windows={'0': [['09:00', '18:00']]},
            deadline=(start + timedelta(days=3)).isoformat(), source='gsd:a1', justification='x', items=['a1', 'a2'])), 'active'))
        cid = jobs.change(self.db, 'block apply', start - timedelta(hours=20), 'model')
        self.db.execute('INSERT INTO blocks VALUES (?,?,?,?,?,?)', (key, 1, cid, demand, 'applied', json.dumps(dict(
            title='Grants', start=start.isoformat(), end=(start + timedelta(hours=1)).isoformat(), event_key=key))))
        return closures

    def closures_report(self, closures, view=True):
        from steward.outcomes import weekly
        def runner(argv):
            if 'contract' in argv:
                return json.dumps({'$id': 'gsd.runs.v1'})
            if argv[:2] == ['gsd', 'closures']:
                if not view:
                    raise ValueError('external command failed')
                self.assertEqual(argv[2:], ['--since', '2026-10-18T00:00:00+03:00', '--json'])
                return json.dumps(dict(schema='gsd.closures.v1', at=NOW.isoformat(), since=argv[3],
                                       cut=dict(until=NOW.isoformat(), change_set_id='x', committed_at=NOW.isoformat()),
                                       closures=closures))
            return json.dumps(dict(schema='gsd.runs.v1', since='2026-10-19', runs=[]))
        return {r['metric']: r for r in weekly(self.db, '2026-10-18', now=NOW, runner=runner)['rows']}

    def test_bundles_used_ignored_pages_and_items_closed_by_gsd_closure_time(self):
        from steward import pages
        athens = TEST_ZONE
        monday = datetime(2026, 10, 19, 9, tzinfo=athens)
        self.bundle('grants:once:0', monday, [])
        self.bundle('comms:once:0', monday + timedelta(days=1), [], demand='comms')
        self.bundle('later:once:0', monday + timedelta(days=4), [], demand='later')  # not elapsed at NOW (Tue 15:00 Athens)
        closures = [dict(id='a1', title='Item a1', project=None, state='done', at='2026-10-19T14:00:00+00:00'),
                    dict(id='a2', title='Item a2', project=None, state='canceled', at='2026-10-20T07:30:00+00:00'),
                    dict(id='zz', title='Other', project=None, state='done', at='2026-10-20T08:00:00+00:00')]
        page = pages.put(self.db, 'Prep', b'<p>x</p>', 'p.html', slug='prep-grants-20261019', now=monday - timedelta(hours=3))
        self.db.execute('UPDATE pages SET opened_at=? WHERE slug=?', ((monday - timedelta(hours=1)).isoformat(), page['slug']))
        rows = self.closures_report(closures)
        self.assertEqual((rows['blocks_used']['count'], rows['blocks_used']['total']), (1, 2))
        self.assertEqual((rows['blocks_ignored']['count'], rows['blocks_ignored']['total']), (1, 2))
        self.assertEqual((rows['pages_opened_before_block']['count'], rows['pages_opened_before_block']['total']), (1, 2))
        self.assertEqual((rows['items_closed']['count'], rows['items_closed']['status']), (2, 'ok'))
        self.assertEqual(rows['interests_moved']['count'], 0)
        # Owner deletion is a move signal, not ignoring; a closure on another day is not use.
        cid = jobs.change(self.db, 'tick', NOW, 'tick')
        self.db.execute('INSERT INTO blocks VALUES (?,?,?,?,?,?)', ('comms:once:0', 2, cid, 'comms', 'removed',
                        json.dumps(dict(title='Grants', start=(monday + timedelta(days=1)).isoformat(), end=(monday + timedelta(days=1, hours=1)).isoformat()))))
        closures[0]['at'] = '2026-10-18T14:00:00+00:00'
        rows = self.closures_report(closures)
        self.assertEqual((rows['blocks_used']['count'], rows['blocks_ignored']['count'], rows['blocks_ignored']['total']), (0, 1, 1))
        rows = self.closures_report(closures, view=False)
        for metric in ('blocks_used', 'blocks_ignored', 'items_closed'):
            self.assertEqual(rows[metric]['status'], 'unknown', metric)
            self.assertEqual(rows[metric]['reason'], 'gsd closures view unavailable or invalid')
        self.assertEqual(rows['pages_opened_before_block']['count'], 1)

    def test_interests_moved_counts_stage_changes_in_week(self):
        for version, stage, when in ((0, 'new', NOW - timedelta(days=10)), (1, 'emerging', NOW - timedelta(hours=2)),
                                     (2, 'emerging', NOW - timedelta(hours=1)), (3, 'current', NOW)):
            cid = jobs.change(self.db, 'interest set', when, 'model')
            self.db.execute('INSERT INTO interests VALUES (?,?,?,?,?)', ('topic', version, cid, json.dumps(dict(stage=stage)), 'active'))
        self.assertEqual(self.report()['interests_moved']['count'], 2)

    def test_before_archive_start_is_unknown_not_zero(self):
        from steward.outcomes import weekly
        report = weekly(self.db, '2026-07-19', now=NOW, runner=lambda argv: '{}')
        for row in report['rows']:
            self.assertEqual(row['status'], 'unknown')
