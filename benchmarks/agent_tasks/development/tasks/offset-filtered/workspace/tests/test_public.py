import unittest

import app


class PublicContract(unittest.TestCase):
    def test_empty_middle_window(self):
        pages = {
            0: {"items": ["a"], "total": 5},
            2: {"items": [], "total": 5},
            4: {"items": ["b"], "total": 5},
        }
        assert app.collect_pages(lambda offset, size: pages[offset], 2) == ["a", "b"]

    def test_positive_page_size(self):
        with self.assertRaises(ValueError):  # noqa: PT027 - standalone stdlib fixture
            app.collect_pages(lambda offset, size: None, 0)
