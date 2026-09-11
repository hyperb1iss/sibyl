import unittest

import app


class PublicContract(unittest.TestCase):
    def test_old_live_event_cannot_undo_delete(self):
        events = [
            {"key": "a", "version": 2, "deleted": True, "value": None},
            {"key": "a", "version": 1, "deleted": False, "value": "old"},
        ]
        assert app.materialize(events) == {}

    def test_live_false(self):
        assert app.materialize([{"key": "a", "version": 1, "deleted": False, "value": False}]) == {
            "a": False
        }
