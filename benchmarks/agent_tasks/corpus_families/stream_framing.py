"""Byte decoding and complete-record framing at stream boundaries."""

from .model import Family, case, source


def stream_framing(seed: int) -> Family:
    contract = """# JSON line stream importer

Read `chunks`, a list of lists of integers from 0 through 255. Chunks are pieces
of one UTF-8 byte stream and may split a multibyte character or JSON record.
Decode strictly. LF separates JSON records; one CR immediately preceding LF is
part of the line ending. Ignore whitespace-only records. At end of stream parse
any remaining nonblank record even without a trailing LF. Return {"records": [...]}
in input order. Any invalid UTF-8, incomplete final character, or malformed JSON
record makes the entire import return {"error": "invalid stream"}.

Imports fail around network chunk boundaries and occasionally lose the final
record. Repair decoding and record framing. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from decoding import decode
        from framing import records

        def dispatch(request):
            try:
                return {"records": records(decode(request["chunks"]))}
            except (UnicodeError, ValueError):
                return {"error": "invalid stream"}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    decoding = source("""
        def decode(chunks):
            return "".join(bytes(chunk).decode("utf-8") for chunk in chunks)
    """)
    framing = source("""
        import json

        def records(text):
            return [json.loads(line) for line in text.split("\\n")[:-1] if line.strip()]
    """)
    fixed_decoding = source("""
        import codecs

        def decode(chunks):
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            return "".join(decoder.decode(bytes(chunk)) for chunk in chunks) + decoder.decode(b"", final=True)
    """)
    fixed_framing = framing.replace('text.split("\\n")[:-1]', 'text.split("\\n")')

    def request(text):
        return {"chunks": [[byte] for byte in text.encode("utf-8")]}

    return Family(
        "utf8-record-framing",
        "learning",
        "json-line-byte-importer-v1",
        contract,
        {"app.py": app, "decoding.py": decoding, "framing.py": framing},
        {"decoding.py": fixed_decoding, "framing.py": fixed_framing},
        {"decoding.py": fixed_decoding},
        [
            case("public-unicode-chunks", request('"café"\n'), {"records": ["café"]}),
            case("public-record-splits", request(f"{seed}\ntrue\n"), {"records": [seed, True]}),
        ],
        [
            case("private-no-final-lf", request("[1,2]"), {"records": [[1, 2]]}),
            case("private-final-malformed", request("1\n{"), {"error": "invalid stream"}),
            case(
                "private-incomplete-codepoint", {"chunks": [[34, 195]]}, {"error": "invalid stream"}
            ),
            case("private-invalid-byte", {"chunks": [[255]]}, {"error": "invalid stream"}),
            case("private-crlf-blank", request(' \r\n"雪"\r\n\nfalse'), {"records": ["雪", False]}),
            case("private-empty-chunks", {"chunks": [[], [], []]}, {"records": []}),
        ],
        mechanism_cluster="incremental-utf8-record-boundaries",
    )
