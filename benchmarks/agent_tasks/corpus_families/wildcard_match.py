"""Escaped wildcard tokenization and iterative whole-string matching repairs."""

from .model import Family, case, source


def wildcard_match(seed: int) -> Family:
    contract = r"""# Artifact-name wildcard filter

Read `pattern` and `text` strings. Match the whole text, case sensitively, using
Unicode code points (not bytes or grapheme clusters). `?` matches exactly one
code point. `*` matches zero or more code points, including slashes and newlines.
Backslash escapes the next code point, whatever it is; an ending backslash
returns {"error": "dangling escape"}. All other characters, including brackets,
are literals. Consecutive stars have the same semantics as one star. Return
{"match": boolean}. Empty patterns and texts are valid. There is no recursion
or nesting limit; use iterative matching for long artifact names.

Stars currently consume only one character, and escaped wildcard characters
lose their literal meaning. Repair tokenization and matching separately. Run
`python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from tokens import tokenize
        from matcher import matches
        def dispatch(request):
            try:
                return {"match": matches(tokenize(request["pattern"]), request["text"])}
            except ValueError:
                return {"error": "dangling escape"}
        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    tokens = source("""
        def tokenize(pattern):
            return [("star" if char == "*" else "any" if char == "?" else "literal", char) for char in pattern]
    """)
    fixed_tokens = source(r"""
        def tokenize(pattern):
            result, index = [], 0
            while index < len(pattern):
                char = pattern[index]
                if char == "\\":
                    index += 1
                    if index == len(pattern):
                        raise ValueError("dangling escape")
                    result.append(("literal", pattern[index]))
                else:
                    result.append(("star" if char == "*" else "any" if char == "?" else "literal", char))
                index += 1
            return result
    """)
    matcher = source("""
        def matches(tokens, text):
            previous = [True] + [False] * len(text)
            for kind, char in tokens:
                current = [False] * (len(text) + 1)
                for index, actual in enumerate(text, 1):
                    current[index] = previous[index - 1] and (kind in ("star", "any") or actual == char)
                previous = current
            return previous[-1]
    """)
    fixed_matcher = source("""
        def matches(tokens, text):
            previous = [True] + [False] * len(text)
            for kind, char in tokens:
                current = [False] * (len(text) + 1)
                current[0] = kind == "star" and previous[0]
                for index, actual in enumerate(text, 1):
                    if kind == "star":
                        current[index] = previous[index] or current[index - 1]
                    else:
                        current[index] = previous[index - 1] and (kind == "any" or actual == char)
                previous = current
            return previous[-1]
    """)

    def check(label, pattern, text, expected):
        return case(label, {"pattern": pattern, "text": text}, {"match": expected})

    return Family(
        "escaped-artifact-wildcards",
        "learning",
        "artifact-wildcard-dynamic-matcher-v1",
        contract,
        {"app.py": app, "tokens.py": tokens, "matcher.py": matcher},
        {"tokens.py": fixed_tokens, "matcher.py": fixed_matcher},
        {"matcher.py": fixed_matcher},
        [
            check("public-star-empty", "a*b", "ab", True),
            check("public-star-many", "a*b", "a123b", True),
        ],
        [
            check("private-literal-star", r"a\*b", "a*b", True),
            check("private-escaped-question", r"\?", "?", True),
            check("private-escaped-slash", r"\\", "\\", True),
            check("private-escape-ordinary", r"\a", "a", True),
            case("private-dangling", {"pattern": "a\\", "text": "a"}, {"error": "dangling escape"}),
            check("private-unicode-codepoints", "??", "e\u0301", True),
            check("private-single-codepoint", "?", "💜", True),
            check("private-newline-slash", "*", "a/\nb", True),
            check("private-full-match", "a", "ab", False),
            check("private-empty", "", "", True),
            check("private-repeated-stars", "***a**", "a", True),
            check("private-brackets-literal", "[a]", "a", False),
            check("private-deep-sequence", "?" * (1200 + seed), "x" * (1200 + seed), True),
        ],
        mechanism_cluster="escaped-wildcard-dynamic-programming",
    )
