import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import writeback
from ledger import Ledger, transaction


class WritebackSendTests(unittest.TestCase):
    def test_send_authorization_is_consumed_until_takeover_reconciles(self):
        self.assertTrue(callable(getattr(writeback, 'authorize_writeback', None)),
                        'The single-send authorization is not implemented')
        identity = {'instance': 'sample', 'project_id': '1', 'kind': 'story', 'id': '1'}
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            with transaction(Path(directory) / 'test.sqlite3') as conn:
                store = Ledger(conn, {'runtime': {'writeback_lease_seconds': 30}}, now)
                conn.execute('''INSERT INTO work_items(instance, project_id, kind, item_id, title,
                    assigned_to, source_status, eligible, observed_at, state, marker)
                    VALUES('sample','1','story','1','title','user','active',1,?,'running','marker')''', (store.time,))
                item = store.find_item(identity)
                conn.execute('''INSERT INTO writebacks(operation_id,item_seq,run_round,operation_type,payload)
                              VALUES('sample-operation', ?, 1, 'comment', '{}')''', (item['seq'],))
                operation = {'operation_id': 'sample-operation', 'owner': 'first'}
                first = writeback.claim_writeback(store, item, operation)
                authorized = {**operation, 'lease_id': first['lease_id']}
                self.assertEqual(writeback.authorize_writeback(store, item, authorized)['status'], 'send_authorized')
                self.assertEqual(writeback.authorize_writeback(store, item, authorized)['status'], 'already_authorized')
                self.assertEqual(writeback.claim_writeback(store, item, operation)['status'], 'leased')
                later = Ledger(conn, store.config, now + timedelta(seconds=31))
                second = writeback.claim_writeback(later, item, {**operation, 'owner': 'second'})
                self.assertTrue(second['reconcile_required'])
                self.assertEqual(writeback.authorize_writeback(later, item, authorized)['status'], 'stale_lease')
                takeover = {**operation, 'lease_id': second['lease_id']}
                self.assertEqual(writeback.authorize_writeback(later, item, takeover)['status'],
                                 'reconciliation_needed')
                writeback.reconcile_writeback(later, item, {**takeover, 'prior_ended': True, 'missing': True})
                self.assertEqual(writeback.authorize_writeback(later, item, takeover)['status'], 'send_authorized')
