import unittest

import app


class PublicContract(unittest.TestCase):
    def test_zero_cursor(self):
        pages = {None: {"items": ["a"], "next_cursor": 0}, 0: {"items": ["b"], "next_cursor": None}}
        assert app.collect_pages(pages.__getitem__) == ["a", "b"]

    def test_empty_export(self):
        assert app.collect_pages(lambda cursor: {"items": [], "next_cursor": None}) == []
