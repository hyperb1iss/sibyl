"""Sparse coordinate aggregation and rectangular matrix multiplication repairs."""

from .model import Family, case, source


def sparse_product(seed: int) -> Family:
    contract = """# Sparse feature transform

Read `left` and `right` matrices, each {"shape": [rows, columns], "entries":
[[row, column, integer_value], ...]}. Shape dimensions are nonnegative integers.
Coordinates must be in bounds. Repeated coordinates SUM, including cancellation;
explicit zeros are legal. Multiply left by right with ordinary integer arithmetic.
Return {"shape": [left rows, right columns], "entries": nonzero coordinates},
ordered by row then column. Rectangular and empty dimensions are valid when the
inner dimensions agree. Invalid coordinates, negative dimensions, or mismatched
inner dimensions return {"error": "invalid matrix"}. Integer magnitudes and
shape dimensions have no fixed cap; never allocate a dense shape-sized matrix.

Feature transforms overwrite repeated contributions and confuse shared-axis
coordinates. Repair normalization and sparse multiplication. Run
`python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from coordinates import normalize
        from multiply import product
        def dispatch(request):
            try:
                left, right = request["left"], request["right"]
                a, b = normalize(left), normalize(right)
                if left["shape"][1] != right["shape"][0]:
                    raise ValueError("shape")
                result = product(a, b)
                return {"shape": [left["shape"][0], right["shape"][1]], "entries": [[r, c, value] for (r, c), value in sorted(result.items()) if value]}
            except ValueError:
                return {"error": "invalid matrix"}
        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    coordinates = source("""
        def normalize(matrix):
            rows, columns = matrix["shape"]
            if rows < 0 or columns < 0:
                raise ValueError("shape")
            result = {}
            for row, column, value in matrix["entries"]:
                if not (0 <= row < rows and 0 <= column < columns):
                    raise ValueError("coordinate")
                result[row, column] = value
            return result
    """)
    multiply = source("""
        def product(left, right):
            result = {}
            for (row, shared), value in left.items():
                for (other, column), factor in right.items():
                    if row == other:
                        result[row, column] = result.get((row, column), 0) + value * factor
            return result
    """)
    fixed_coordinates = coordinates.replace(
        "result[row, column] = value", "result[row, column] = result.get((row, column), 0) + value"
    )
    fixed_multiply = multiply.replace("if row == other:", "if shared == other:")

    def check(label, a_shape, a, b_shape, b, expected):
        return case(
            label,
            {"left": {"shape": a_shape, "entries": a}, "right": {"shape": b_shape, "entries": b}},
            expected,
        )

    return Family(
        "sparse-feature-product",
        "learning",
        "coordinate-sparse-matrix-product-v1",
        contract,
        {"app.py": app, "coordinates.py": coordinates, "multiply.py": multiply},
        {"coordinates.py": fixed_coordinates, "multiply.py": fixed_multiply},
        {"multiply.py": fixed_multiply},
        [
            check(
                "public-shared-axis",
                [1, 2],
                [[0, 1, 3]],
                [2, 1],
                [[1, 0, 4]],
                {"shape": [1, 1], "entries": [[0, 0, 12]]},
            )
        ],
        [
            check(
                "private-duplicate-contributions",
                [1, 1],
                [[0, 0, seed + 2], [0, 0, 3]],
                [1, 1],
                [[0, 0, 2], [0, 0, 4]],
                {"shape": [1, 1], "entries": [[0, 0, (seed + 5) * 6]]},
            ),
            check(
                "private-cancellation",
                [1, 1],
                [[0, 0, 4], [0, 0, -4]],
                [1, 1],
                [[0, 0, 8]],
                {"shape": [1, 1], "entries": []},
            ),
            check(
                "private-sorted-sums",
                [2, 2],
                [[1, 1, 2], [0, 1, 3], [0, 0, 1]],
                [2, 2],
                [[1, 1, 4], [0, 1, -12], [1, 0, 5]],
                {"shape": [2, 2], "entries": [[0, 0, 15], [1, 0, 10], [1, 1, 8]]},
            ),
            check(
                "private-large-sparse-shape",
                [10**12, 10**12],
                [[10**12 - 1, 9, 10**30]],
                [10**12, 2],
                [[9, 1, -2]],
                {"shape": [10**12, 2], "entries": [[10**12 - 1, 1, -2 * 10**30]]},
            ),
            check("private-empty-inner", [2, 0], [], [0, 3], [], {"shape": [2, 3], "entries": []}),
            check("private-mismatch", [1, 2], [], [3, 1], [], {"error": "invalid matrix"}),
            check(
                "private-invalid-coordinate",
                [1, 1],
                [[0, 1, 0]],
                [1, 1],
                [],
                {"error": "invalid matrix"},
            ),
            check("private-negative-shape", [-1, 1], [], [1, 1], [], {"error": "invalid matrix"}),
        ],
        mechanism_cluster="sparse-coordinate-aggregation-product",
    )
