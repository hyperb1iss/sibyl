"""Deterministic Huffman lengths and canonical prefix-code assignment repairs."""

from .model import Family, case, source


def canonical_huffman(seed: int) -> Family:
    contract = """# Canonical telemetry token codec

Read `frequencies`, an object mapping nonempty string symbols to positive integer
weights, `text` as an array of those symbols, and a `bits` string to decode.
Symbols are tokens, not individual characters. Construct Huffman lengths by
repeatedly merging the two lowest-weight active trees. Break equal-weight ties
by each tree's lexicographically smallest symbol (Python Unicode string order).
A merged tree's weight is the sum and its tie key is the minimum of its leaves.
Assign one-bit code "0" to a singleton alphabet. For larger alphabets, a symbol's
length is its leaf depth. Empty alphabets are valid only for empty streams.

Assign canonical codes in ascending (length, symbol) order: the first code is
zero at its length; increment the previous code and left-shift by the increase
in length. Pad to exactly the declared length. Return {"codes": symbol-to-bit
strings, "encoded": concatenation of codes for text, "decoded": symbols from
bits}. Any unknown text symbol, nonbinary bit, impossible prefix, or unfinished
codeword returns {"error": "invalid stream"}. Never silently discard trailing
bits. Integer weights and alphabet sizes have no fixed cap; use iterative tree
traversal and decoding, without a call-stack depth limit.

Codec tie ordering incorrectly ignores letter case, and its lengths waste bits. Repair tree
selection and canonical ordering. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from lengths import lengths
        from canonical import codes
        from streams import decode
        def dispatch(request):
            table = codes(lengths(request["frequencies"]), request["frequencies"])
            try:
                encoded = "".join(table[symbol] for symbol in request["text"])
                return {"codes": table, "encoded": encoded, "decoded": decode(request["bits"], table)}
            except (KeyError, ValueError):
                return {"error": "invalid stream"}
        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    lengths = source("""
        import heapq
        def lengths(frequencies):
            heap = [(-weight, symbol, symbol) for symbol, weight in frequencies.items()]
            heapq.heapify(heap)
            while len(heap) > 1:
                wa, ka, a = heapq.heappop(heap)
                wb, kb, b = heapq.heappop(heap)
                heapq.heappush(heap, (wa + wb, min(ka, kb), (a, b)))
            result = {}
            pending = [(heap[0][2], 0)] if heap else []
            while pending:
                node, depth = pending.pop()
                if isinstance(node, str):
                    result[node] = max(1, depth)
                else:
                    pending.extend((child, depth + 1) for child in node)
            return result
    """)
    fixed_lengths = lengths.replace("(-weight, symbol, symbol)", "(weight, symbol, symbol)")
    canonical = source("""
        def codes(lengths, frequencies):
            ordered = sorted(frequencies, key=lambda symbol: (lengths[symbol], symbol.casefold()))
            result, value, previous = {}, 0, 0
            for symbol in ordered:
                width = lengths[symbol]
                value <<= width - previous
                result[symbol] = format(value, "0" + str(width) + "b")
                value += 1
                previous = width
            return result
    """)
    fixed_canonical = canonical.replace(
        "key=lambda symbol: (lengths[symbol], symbol.casefold())",
        "key=lambda symbol: (lengths[symbol], symbol)",
    )
    streams = source("""
        def decode(bits, table):
            trie = {}
            for symbol, code in table.items():
                node = trie
                for bit in code:
                    node = node.setdefault(bit, {})
                node["symbol"] = symbol
            result, node = [], trie
            for bit in bits:
                if bit not in ("0", "1") or bit not in node:
                    raise ValueError("invalid prefix")
                node = node[bit]
                if "symbol" in node:
                    result.append(node["symbol"])
                    node = trie
            if node is not trie:
                raise ValueError("unfinished codeword")
            return result
    """)

    def check(label, frequencies, text, bits, expected):
        return case(label, {"frequencies": frequencies, "text": text, "bits": bits}, expected)

    table = {"a": "0", "b": "10", "c": "11"}
    return Family(
        "canonical-telemetry-codec",
        "learning",
        "huffman-canonical-token-codec-v1",
        contract,
        {"app.py": app, "lengths.py": lengths, "canonical.py": canonical, "streams.py": streams},
        {"lengths.py": fixed_lengths, "canonical.py": fixed_canonical},
        {"lengths.py": fixed_lengths},
        [
            check(
                "public-optimal-lengths",
                {"a": 3, "b": 2, "c": 1},
                ["a", "b", "c"],
                "01011",
                {"codes": table, "encoded": "01011", "decoded": ["a", "b", "c"]},
            )
        ],
        [
            check(
                "private-case-sensitive-order",
                {"B": 1, "a": 1},
                ["B", "a"],
                "01",
                {"codes": {"B": "0", "a": "1"}, "encoded": "01", "decoded": ["B", "a"]},
            ),
            check(
                "private-unicode-order",
                {"Z": 1, "a": 1, "ß": 1, "é": 1},
                ["Z", "a", "ß", "é"],
                "00011011",
                {
                    "codes": {"Z": "00", "a": "01", "ß": "10", "é": "11"},
                    "encoded": "00011011",
                    "decoded": ["Z", "a", "ß", "é"],
                },
            ),
            check(
                "private-input-order",
                {"c": 1, "b": 2, "a": 3},
                ["a", "b", "c"],
                "11100",
                {"codes": table, "encoded": "01011", "decoded": ["c", "b", "a"]},
            ),
            check(
                "private-tree-weight-tie",
                {"a": 1, "b": 1, "c": 2},
                ["a", "c", "b"],
                "10011",
                {
                    "codes": {"c": "0", "a": "10", "b": "11"},
                    "encoded": "10011",
                    "decoded": ["a", "c", "b"],
                },
            ),
            check(
                "private-equal-weights",
                {"d": 1, "c": 1, "b": 1, "a": 1},
                ["d", "a"],
                "1001",
                {
                    "codes": {"a": "00", "b": "01", "c": "10", "d": "11"},
                    "encoded": "1100",
                    "decoded": ["c", "b"],
                },
            ),
            check(
                "private-singleton-token",
                {"token": 10**30 + seed},
                ["token", "token"],
                "000",
                {"codes": {"token": "0"}, "encoded": "00", "decoded": ["token"] * 3},
            ),
            check(
                "private-empty-alphabet", {}, [], "", {"codes": {}, "encoded": "", "decoded": []}
            ),
            check("private-impossible-prefix", {"a": 1}, [], "1", {"error": "invalid stream"}),
            check(
                "private-unfinished", {"a": 3, "b": 2, "c": 1}, [], "1", {"error": "invalid stream"}
            ),
            check("private-nonbinary", {"a": 1}, [], "x", {"error": "invalid stream"}),
            check("private-unknown-token", {"a": 1}, ["b"], "", {"error": "invalid stream"}),
        ],
        mechanism_cluster="huffman-lengths-canonical-prefix-codes",
    )
