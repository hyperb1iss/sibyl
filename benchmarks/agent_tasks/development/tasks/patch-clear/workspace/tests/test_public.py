import unittest

import app


class PublicContract(unittest.TestCase):
    def test_clear_optional_field(self):
        assert app.apply_patch({"nickname": "Nova", "active": True}, {"nickname": None}) == {
            "active": True
        }

    def test_false_is_a_value(self):
        assert app.apply_patch({"active": True}, {"active": False}) == {"active": False}
