"""Byte-order normalization and two's-complement interpretation are separate steps."""

from .model import Family, case, source


def bit_fields(seed: int) -> Family:
    contract = """# Packed register decoder

Read `bytes` (integers 0..255), `byte_order` (little or big), and `fields` with
unique string name, nonnegative offset, positive width, and boolean signed.
Interpret the entire byte array as one unsigned integer in byte_order. Bit offset
zero is always the least significant bit of that integer, independent of byte
order. Extract width bits starting at offset. Signed fields use two's complement
within that field's width, not the byte-array width. Fields may overlap and may
span arbitrary byte boundaries; widths greater than 64 are valid. Return
{"values": {name: integer}}. Any field extending beyond the available bits returns
{"error": "invalid field"}; zero/negative widths and negative offsets do too.

Cross-byte registers and signed subfields decode incorrectly. Repair byte-order
handling and field interpretation. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from words import word
        from fields import extract

        def dispatch(request):
            try:
                value = word(request["bytes"], request["byte_order"])
                return {"values": {field["name"]: extract(value, 8 * len(request["bytes"]), field) for field in request["fields"]}}
            except ValueError:
                return {"error": "invalid field"}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    words = source("""
        def word(data, byte_order):
            return int.from_bytes(bytes(data), "big")
    """)
    fields = source("""
        def extract(value, bits, field):
            offset, width = field["offset"], field["width"]
            if offset < 0 or width <= 0 or offset + width > bits:
                raise ValueError("invalid range")
            return (value >> offset) & ((1 << width) - 1)
    """)
    fixed_words = words.replace('"big"', "byte_order")
    fixed_fields = fields.replace(
        "    return (value >> offset) & ((1 << width) - 1)",
        '    extracted = (value >> offset) & ((1 << width) - 1)\n    if field["signed"] and extracted & (1 << (width - 1)):\n        extracted -= 1 << width\n    return extracted',
    )

    def field(offset, width, signed=False):
        return {"name": f"field-{seed}", "offset": offset, "width": width, "signed": signed}

    def request(data, order, selected):
        return {"bytes": data, "byte_order": order, "fields": [selected]}

    def answer(value):
        return {"values": {f"field-{seed}": value}}

    return Family(
        "signed-register-fields",
        "learning",
        "packed-register-decoder-v1",
        contract,
        {"app.py": app, "words.py": words, "fields.py": fields},
        {"words.py": fixed_words, "fields.py": fixed_fields},
        {"words.py": fixed_words},
        [
            case("public-little", request([0x12, 0x34], "little", field(4, 8)), answer(0x41)),
            case("public-big", request([0x12, 0x34], "big", field(4, 8)), answer(0x23)),
        ],
        [
            case(
                "private-signed-subfield",
                request([0xF0, 0], "little", field(4, 4, True)),
                answer(-1),
            ),
            case("private-signed-positive", request([0x70], "big", field(4, 4, True)), answer(7)),
            case("private-sign-bit", request([0x80], "little", field(7, 1, True)), answer(-1)),
            case("private-wide", request([255] * 12, "big", field(0, 96, True)), answer(-1)),
            case(
                "private-out-of-bounds",
                request([0], "little", field(7, 2)),
                {"error": "invalid field"},
            ),
            case(
                "private-empty-width", request([0], "big", field(0, 0)), {"error": "invalid field"}
            ),
            case(
                "private-negative-offset",
                request([0], "little", field(-1, 1)),
                {"error": "invalid field"},
            ),
        ],
        mechanism_cluster="byte-order-signed-bit-extraction",
    )
