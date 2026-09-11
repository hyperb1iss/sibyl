"""Expression parsing and Kleene truth operations must agree on unknown values."""

from .model import Family, case, source


def three_valued_logic(seed: int) -> Family:
    contract = """# Nullable rule evaluator

Read `tokens`, a JSON array containing boolean literals true/false, null (unknown),
and the strings NOT, AND, OR, (, ). NOT has highest precedence, then AND, then OR.
Binary operators associate left; NOT can repeat. Parentheses override precedence.
Return {"value": true_or_false_or_null}. Reject empty, unbalanced, adjacent-literal,
trailing-operator, or unrecognized-token expressions with {"error": "invalid rule"}.

Use three-valued logic: NOT unknown is unknown. AND is false if either operand is
false, true if both are true, and unknown otherwise. OR is true if either operand
is true, false if both are false, and unknown otherwise. These truth tables apply
to every subtree; unknown is never implicitly converted to false. Valid rules
may contain 1200 nested NOT operators or parentheses; Python call-stack depth
is not a grammar limit.

Mixed AND/OR rules and missing observations produce incorrect decisions. Repair
parsing and truth evaluation. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from parser import parse
        from truth import evaluate

        def dispatch(request):
            try:
                return {"value": evaluate(parse(request["tokens"]))}
            except ValueError:
                return {"error": "invalid rule"}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    parser = source("""
        def parse(tokens):
            precedence = {"NOT": 3, "AND": 2, "OR": 1}
            output, operators = [], []
            operand = True
            for token in tokens:
                if token is None or type(token) is bool:
                    if not operand:
                        raise ValueError("adjacent literals")
                    output.append(token)
                    operand = False
                elif token == "NOT" and operand:
                    operators.append(token)
                elif token == "(" and operand:
                    operators.append(token)
                elif token == ")" and not operand:
                    while operators and operators[-1] != "(":
                        output.append(operators.pop())
                    if not operators:
                        raise ValueError("unmatched close")
                    operators.pop()
                elif token in ("AND", "OR") and not operand:
                    while operators and operators[-1] != "(" and precedence[operators[-1]] >= precedence[token]:
                        output.append(operators.pop())
                    operators.append(token)
                    operand = True
                else:
                    raise ValueError("invalid token")
            if operand:
                raise ValueError("missing operand")
            while operators:
                operator = operators.pop()
                if operator == "(":
                    raise ValueError("missing close")
                output.append(operator)
            return output
    """)
    buggy_parser = parser.replace('"AND": 2', '"AND": 1')
    truth = source("""
        def apply(operator, left, right):
            if operator == "NOT":
                return not left
            return bool(left and right) if operator == "AND" else bool(left or right)

        def evaluate(program):
            values = []
            for token in program:
                if token is None or type(token) is bool:
                    values.append(token)
                elif token == "NOT":
                    values.append(apply(token, values.pop(), None))
                else:
                    right = values.pop()
                    left = values.pop()
                    values.append(apply(token, left, right))
            return values[0]
    """)
    reference_truth = (
        source("""
        def apply(operator, left, right):
            if operator == "NOT":
                return None if left is None else not left
            if operator == "AND":
                if left is False or right is False:
                    return False
                return True if left is True and right is True else None
            if left is True or right is True:
                return True
            return False if left is False and right is False else None

    """)
        + truth[truth.index("def evaluate(") :]
    )
    return Family(
        "three-valued-rule-parser",
        "learning",
        "nullable-rule-expression-v1",
        contract,
        {"app.py": app, "parser.py": buggy_parser, "truth.py": truth},
        {"parser.py": parser, "truth.py": reference_truth},
        {"parser.py": parser},
        [
            case(
                "public-precedence", {"tokens": [True, "OR", False, "AND", False]}, {"value": True}
            ),
            case(
                "public-nested-not",
                {"tokens": ["NOT"] * (2 * (seed % 3 + 1)) + [True]},
                {"value": True},
            ),
        ],
        [
            case("private-not-unknown", {"tokens": ["NOT", None]}, {"value": None}),
            case("private-deep-not", {"tokens": ["NOT"] * 1200 + [None]}, {"value": None}),
            case(
                "private-deep-parentheses",
                {"tokens": ["("] * 1200 + [True] + [")"] * 1200},
                {"value": True},
            ),
            case("private-and-unknown", {"tokens": [None, "AND", True]}, {"value": None}),
            case("private-or-unknown", {"tokens": [False, "OR", None]}, {"value": None}),
            case("private-and-false", {"tokens": [None, "AND", False]}, {"value": False}),
            case("private-or-true", {"tokens": [None, "OR", True]}, {"value": True}),
            case(
                "private-parentheses",
                {"tokens": ["(", True, "OR", False, ")", "AND", False]},
                {"value": False},
            ),
            case("private-empty", {"tokens": []}, {"error": "invalid rule"}),
            case("private-trailing", {"tokens": [True, "AND"]}, {"error": "invalid rule"}),
            case("private-adjacent", {"tokens": [True, False]}, {"error": "invalid rule"}),
            case("private-unbalanced", {"tokens": ["(", True]}, {"error": "invalid rule"}),
            case("private-nonboolean", {"tokens": [1]}, {"error": "invalid rule"}),
        ],
        mechanism_cluster="kleene-logic-expression-precedence",
    )
