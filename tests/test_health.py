import os
import tempfile
import unittest

from museum_merch.database import connect


class DatabaseTest(unittest.TestCase):
    def test_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DATABASE_PATH"] = os.path.join(directory, "test.db")
            database = connect()
            try:
                self.assertEqual(database.execute("SELECT 1").fetchone()[0], 1)
            finally:
                database.close()
