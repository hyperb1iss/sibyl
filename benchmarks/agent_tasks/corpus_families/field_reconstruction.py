"""Finite-field interpolation and duplicate sample normalization repairs."""

from .model import Family, case, source


def field_reconstruction(seed: int) -> Family:
    contract = """# Shard sample reconstruction

Read a prime integer `modulus` >= 2, `samples` as [x,y] integer pairs, and integer
`queries`. The caller guarantees a prime modulus. Normalize every x, y, and query
modulo that prime. Repeated normalized x values with equal normalized y count as
one sample. Conflicting y values at the same normalized x return
{"error": "conflicting sample"}. No samples returns {"error": "no samples"}.
For n distinct samples, evaluate the unique polynomial of degree less than n
through those samples at each query, in query order. Return {"values": [integers
in 0..modulus-1]}. Negative and arbitrarily large integers are valid. One sample
specifies a constant polynomial. Use exact modular arithmetic, not floating point.

Shard reconstruction currently divides integers before reducing, and repeated
sample coordinates create zero denominators. Repair sample normalization and
interpolation. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from samples import normalize
        from interpolate import evaluate
        def dispatch(request):
            try:
                modulus = request["modulus"]
                points = normalize(request["samples"], modulus)
                if not points:
                    return {"error": "no samples"}
                return {"values": [evaluate(points, x % modulus, modulus) for x in request["queries"]]}
            except ValueError:
                return {"error": "conflicting sample"}
        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    samples = source("""
        def normalize(samples, modulus):
            return [(x % modulus, y % modulus) for x, y in samples]
    """)
    fixed_samples = source("""
        def normalize(samples, modulus):
            unique = {}
            for x, y in samples:
                x, y = x % modulus, y % modulus
                if x in unique and unique[x] != y:
                    raise ValueError("conflicting sample")
                unique[x] = y
            return list(unique.items())
    """)
    interpolate = source("""
        def evaluate(points, x, modulus):
            total = 0
            for i, (xi, yi) in enumerate(points):
                numerator = denominator = 1
                for j, (xj, _) in enumerate(points):
                    if i != j:
                        numerator = numerator * (x - xj) % modulus
                        denominator = denominator * (xi - xj) % modulus
                total = (total + yi * (numerator // denominator)) % modulus
            return total
    """)
    fixed_interpolate = interpolate.replace(
        "(numerator // denominator)", "(numerator * pow(denominator, -1, modulus))"
    )

    def check(label, modulus, samples, queries, expected):
        return case(label, {"modulus": modulus, "samples": samples, "queries": queries}, expected)

    return Family(
        "finite-field-shard-reconstruction",
        "learning",
        "prime-field-interpolation-v1",
        contract,
        {"app.py": app, "samples.py": samples, "interpolate.py": interpolate},
        {"samples.py": fixed_samples, "interpolate.py": fixed_interpolate},
        {"interpolate.py": fixed_interpolate},
        [check("public-modular-inverse", 7, [[0, 1], [2, 5]], [1, 3], {"values": [3, 0]})],
        [
            check(
                "private-duplicate-coordinate",
                7,
                [[0, 1], [7, 8], [2, 5]],
                [1, 3],
                {"values": [3, 0]},
            ),
            check(
                "private-conflicting-coordinate",
                7,
                [[0, 1], [7, 2]],
                [0],
                {"error": "conflicting sample"},
            ),
            check(
                "private-negative-residues", 7, [[-7, -6], [-5, -2]], [-6, -4], {"values": [3, 0]}
            ),
            check(
                "private-quadratic",
                101,
                [[0, seed], [1, seed + 1], [2, seed + 4]],
                [3, 10, -1],
                {"values": [(seed + 9) % 101, (seed + 100) % 101, (seed + 1) % 101]},
            ),
            check(
                "private-large-prime",
                2**61 - 1,
                [[0, 4], [2, 10]],
                [10**40],
                {"values": [(3 * 10**40 + 4) % (2**61 - 1)]},
            ),
            check("private-constant", 7, [[3, -2]], [0, 1, 100], {"values": [5, 5, 5]}),
            check("private-prime-two", 2, [[0, 1], [1, 0]], [0, 1, 2], {"values": [1, 0, 1]}),
            check("private-empty-samples", 7, [], [1], {"error": "no samples"}),
            check("private-empty-queries", 7, [[1, 2]], [], {"values": []}),
        ],
        mechanism_cluster="prime-field-lagrange-reconstruction",
    )
