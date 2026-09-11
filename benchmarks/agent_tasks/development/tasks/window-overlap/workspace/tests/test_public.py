import unittest
from datetime import UTC, datetime, timedelta

import app


class PublicContract(unittest.TestCase):
    def test_touching_windows(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        hour = timedelta(hours=1)
        assert not app.overlaps((start, start + hour), (start + hour, start + 2 * hour))

    def test_identical_windows(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        assert app.overlaps(
            (start, start + timedelta(hours=1)), (start, start + timedelta(hours=1))
        )
