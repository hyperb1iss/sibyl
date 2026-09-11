import unittest
from datetime import date

import app


class PublicContract(unittest.TestCase):
    def test_last_day_is_active(self):
        assert app.active_on(date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 3))

    def test_outside_range(self):
        assert not app.active_on(date(2026, 1, 1), date(2026, 1, 3), date(2025, 12, 31))
