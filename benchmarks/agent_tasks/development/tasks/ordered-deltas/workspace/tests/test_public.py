import unittest

import app


class PublicContract(unittest.TestCase):
    def test_repeated_increments(self):
        assert app.replay(
            {"a": 2},
            [
                {"op": "increment", "key": "a", "value": 3},
                {"op": "increment", "key": "a", "value": 4},
            ],
        ) == {"a": 9}

    def test_remove_absent(self):
        assert app.replay({}, [{"op": "remove", "key": "a"}]) == {}
