"""Original JSON byte ranges and reversible exact-value sharing for evidence."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

JsonPath = tuple[str | int, ...]
ByteRange = tuple[int, int]


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


@dataclass(frozen=True)
class OriginalJson:
    value: Any
    ranges: dict[JsonPath, ByteRange]


class _Reader:
    def __init__(self, artifact: bytes) -> None:
        self.text = artifact.decode("utf-8")
        self.cursor = 0
        self.byte_cursor = 0
        self.ranges: dict[JsonPath, ByteRange] = {}
        self.decoder = json.JSONDecoder(parse_constant=_reject_constant, parse_float=_finite_float)

    def advance(self, end: int) -> None:
        self.byte_cursor += len(self.text[self.cursor : end].encode("utf-8"))
        self.cursor = end

    def whitespace(self) -> None:
        while self.cursor < len(self.text) and self.text[self.cursor] in " \t\r\n":
            self.advance(self.cursor + 1)

    def take(self, expected: str) -> None:
        self.whitespace()
        if self.text[self.cursor : self.cursor + 1] != expected:
            raise ValueError(f"expected JSON delimiter {expected!r}")
        self.advance(self.cursor + 1)

    def read(self, path: JsonPath = ()) -> Any:
        self.whitespace()
        start = self.byte_cursor
        marker = self.text[self.cursor : self.cursor + 1]
        if marker == "{":
            value = self.object(path)
        elif marker == "[":
            value = self.array(path)
        else:
            value, end = self.decoder.raw_decode(self.text, self.cursor)
            self.advance(end)
        self.ranges[path] = (start, self.byte_cursor)
        return value

    def object(self, path: JsonPath) -> dict[str, Any]:
        self.take("{")
        result: dict[str, Any] = {}
        self.whitespace()
        if self.text[self.cursor : self.cursor + 1] != "}":
            while True:
                key, end = self.decoder.raw_decode(self.text, self.cursor)
                if not isinstance(key, str) or key in result:
                    raise ValueError("JSON object keys must be unique strings")
                self.advance(end)
                self.take(":")
                result[key] = self.read((*path, key))
                self.whitespace()
                if self.text[self.cursor : self.cursor + 1] != ",":
                    break
                self.take(",")
                self.whitespace()
        self.take("}")
        return result

    def array(self, path: JsonPath) -> list[Any]:
        self.take("[")
        result: list[Any] = []
        self.whitespace()
        if self.text[self.cursor : self.cursor + 1] != "]":
            while True:
                result.append(self.read((*path, len(result))))
                self.whitespace()
                if self.text[self.cursor : self.cursor + 1] != ",":
                    break
                self.take(",")
        self.take("]")
        return result


def read_original_json(artifact: bytes) -> OriginalJson:
    """Parse without changing original byte positions or accepting ambiguous keys."""
    reader = _Reader(artifact)
    value = reader.read()
    reader.whitespace()
    if reader.cursor != len(reader.text):
        raise ValueError("trailing data after JSON evidence")
    return OriginalJson(value, reader.ranges)


def share_exact_values(value: Any) -> dict[str, Any]:
    """Share repeated JSON values while escaping literal reference-shaped objects."""
    counts: Counter[str] = Counter()

    def count(item: Any) -> None:
        key = canonical(item)
        if len(key) >= 80:
            counts[key] += 1
        if isinstance(item, dict):
            for child in item.values():
                count(child)
        elif isinstance(item, list):
            for child in item:
                count(child)

    count(value)
    pool: list[Any] = []
    seen: dict[str, int] = {}

    def encode(item: Any) -> Any:
        key = canonical(item)
        repeated = len(key) >= 80 and counts[key] > 1
        if repeated and key in seen:
            return {"$ref": seen[key]}
        if isinstance(item, dict):
            encoded = {name: encode(child) for name, child in item.items()}
            if set(item) in ({"$ref"}, {"$literal"}):
                encoded = {"$literal": list(encoded.items())}
        elif isinstance(item, list):
            encoded = [encode(child) for child in item]
        else:
            encoded = item
        if repeated:
            index = len(pool)
            pool.append(encoded)
            seen[key] = index
            return {"$ref": index}
        return encoded

    encoded = encode(value)
    return {"values": pool, "sources": encoded}
