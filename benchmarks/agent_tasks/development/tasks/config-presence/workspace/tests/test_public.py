import unittest

import app


class PublicContract(unittest.TestCase):
    def test_explicit_zero(self):
        assert app.resolve_options({"retries": 3}, {"retries": 0}) == {"retries": 0}

    def test_none_inherits(self):
        assert app.resolve_options({"retries": 3}, {"retries": None}) == {"retries": 3}
