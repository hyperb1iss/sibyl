"""Index-served `IN` lists on SurrealDB 3.2 and 4.0.

Both planners turn `field IN $list` into one index lookup per value only when
the list holds at most 32 values (`MAX_IN_EXPANSION_SIZE`), and only when the
list is known when the statement is planned. 4.0 plans a whole statement
before it runs, so a list bound by a block `LET`, a `FOR` variable or a
closure argument is not known then, and the read scans the table. A client
parameter is known. So lists are split here, in Python, into client
parameters of at most 32 values, one statement copy per clump.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Sequence

IN_CLUMP = 32


def clumped(template: str, param: str, values: Sequence[Hashable]) -> tuple[str, dict[str, list]]:
    """`array::flatten([...])` over one copy of `template` per clump, and its params.

    `template` is a statement that names `$<param>` once as an `IN` list.
    Duplicates go first: a value split across two clumps would answer twice.
    """
    distinct = list(dict.fromkeys(values))
    clumps = [distinct[start : start + IN_CLUMP] for start in range(0, len(distinct), IN_CLUMP)]
    if not clumps:
        clumps = [[]]
    names = [f"{param}_{index}" for index in range(len(clumps))]
    pattern = re.compile(rf"\${re.escape(param)}\b")
    copies = ", ".join(f"({pattern.sub('$' + name, template)})" for name in names)
    return f"array::flatten([{copies}])", dict(zip(names, clumps, strict=True))
