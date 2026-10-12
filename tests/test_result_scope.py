"""Do not promote a proven historical CLI error to a current request failure."""
from contextlib import closing
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from test_model_metadata import field, metadata


def error_payload(message):
    return field(1, 17, 0) + field(4, 3, 0) + field(24, field(3, field(2, message.encode())))


class ResultScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cid = str(uuid.uuid4())
        self.path = Path(self.temp.name)/'.gemini/antigravity-cli/conversations'/(self.cid+'.db')
        self.path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('CREATE TABLE steps(idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB)')
            conn.executemany('INSERT INTO steps VALUES (?,?,?,?,?)', [
                (2, 17, 3, field(12, b'old'), error_payload('Old capacity error')),
                (7, 15, 3, field(12, b'current'), b''),
            ])

    def tearDown(self):
        self.temp.cleanup()

    def read(self, observed=frozenset({7}), responses=frozenset({7}), error='Old capacity error'):
        return metadata.read_session_historical_error(self.temp.name, self.cid, observed, responses, error)

    def test_exact_historical_error_is_read_only(self):
        original = self.path.read_bytes()
        self.assertEqual(self.read(), {'step_index':2, 'error':'Old capacity error'})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNone(self.read(error='Different error'))

    def test_error_in_current_turn_is_not_historical_even_after_recovery(self):
        for index in (6, 8):
            with self.subTest(index=index), closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute('INSERT INTO steps VALUES (?,?,?,?,?)', (index,17,3,field(12,b'current'),error_payload('Old capacity error')))
            self.assertIsNone(self.read(observed={6,7}, responses={7}))
            with closing(sqlite3.connect(self.path)) as conn, conn:conn.execute('DELETE FROM steps WHERE idx=?', (index,))

    def test_incomplete_foreign_or_missing_execution_stays_unverified(self):
        for status, kind, execution in ((1,15,b'current'), (3,132,b'current'), (3,15,b''), (3,15,None)):
            with self.subTest(status=status,kind=kind,execution=execution), closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute('UPDATE steps SET status=?,step_type=?,metadata=? WHERE idx=7', (status,kind,field(12,execution) if execution is not None else b''))
            self.assertIsNone(self.read())

    def test_concurrent_later_step_and_mixed_executions_stay_unverified(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('INSERT INTO steps VALUES (?,?,?,?,?)', (9,15,3,field(12,b'other'),b''))
        self.assertIsNone(self.read())
        self.assertIsNone(self.read(observed={7,9}, responses={7,9}))

    def test_unknown_schema_or_invalid_request_never_hides_an_error(self):
        for payload in (b'\x80', field(24,field(3,field(1,b'Old capacity error'))), b'x'*(metadata.MAX_BLOB+1)):
            with self.subTest(size=len(payload)), closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute('UPDATE steps SET step_payload=? WHERE idx=2', (payload,))
            self.assertIsNone(self.read())
        self.assertIsNone(self.read(observed=set()))
        self.assertIsNone(self.read(responses={9}))
        self.assertIsNone(metadata.read_session_historical_error(self.temp.name, '../outside', {7}, {7}, 'Old capacity error'))
