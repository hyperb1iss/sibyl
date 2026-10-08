"""Decode SurrealDB responses with cbor2's C decoder when it is installed.

The SDK decodes every response with a pure-Python copy of cbor2, and on a
large read that costs more than the query itself. cbor2's C build turns the
same bytes into the same values through the SDK's own tag decoder, a few
times faster. Every SDK transport decodes through ``surrealdb.data.cbor``,
which resolves ``loads`` at call time, so one replacement covers them all.

Installing is explicit and idempotent. Without cbor2's C build it does
nothing and the SDK keeps its own decoder.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

_installed = False


def install_fast_cbor() -> bool:
    """Route SDK response decoding through cbor2's C decoder; report whether it did."""
    global _installed
    if _installed:
        return True
    try:
        import cbor2
    except ImportError:
        return False
    # A pure-Python cbor2 is the same code the SDK already ships.
    if not inspect.isbuiltin(cbor2.loads):
        return False
    from surrealdb.cbor import CBORDecoder, CBORTag
    from surrealdb.data import cbor as sdk_cbor
    from surrealdb.data.types import constants

    tag_type = cbor2.CBORTag
    # Compact datetimes and durations carry two numbers the SDK only indexes;
    # they are most of the tags in a response, so they skip the thaw.
    numeric_pairs = {constants.TAG_DATETIME_COMPACT, constants.TAG_DURATION_COMPACT}
    frozen_map = getattr(cbor2, "frozendict", None) or getattr(cbor2, "FrozenDict", dict)
    c_loads = cbor2.loads
    sdk_loads = sdk_cbor.loads

    def thaw(value: Any) -> Any:
        # The C decoder freezes whatever sits inside a tag (arrays become
        # tuples, maps become frozen); the SDK's decoder hands over lists and
        # dicts. CBOR has no tuple, so this restores exactly what was sent.
        if isinstance(value, tuple):
            return [thaw(item) for item in value]
        if isinstance(value, frozen_map):
            return {key: thaw(item) for key, item in value.items()}
        return value

    def loads(
        s: bytes | bytearray | memoryview,
        tag_hook: Callable[[CBORDecoder, CBORTag], Any] | None = None,
        object_hook: Callable[[CBORDecoder, Mapping[Any, Any]], Any] | None = None,
        str_errors: Literal["strict", "error", "replace"] = "strict",
    ) -> Any:
        if object_hook is not None:
            # The SDK never sets one; keep its own decoder for that contract.
            return sdk_loads(s, tag_hook=tag_hook, object_hook=object_hook, str_errors=str_errors)
        if tag_hook is None:
            return c_loads(s, str_errors=str_errors)
        # The SDK's decoder reads only .tag and .value, which cbor2's tag carries.
        sdk_tag_hook = cast(Callable[[Any, Any], Any], tag_hook)

        def hook(*args: Any) -> Any:
            # cbor2 releases disagree on the hook's other arguments; the tag is
            # the only one the SDK's decoder reads.
            tag = next(arg for arg in args if isinstance(arg, tag_type))
            if tag.tag not in numeric_pairs and isinstance(tag.value, (tuple, frozen_map)):
                tag = tag_type(tag.tag, thaw(tag.value))
            return sdk_tag_hook(None, tag)

        return c_loads(s, tag_hook=hook, str_errors=str_errors)

    # setattr: this deliberately rebinds the SDK module's own function.
    setattr(sdk_cbor, "loads", loads)  # noqa: B010
    _installed = True
    return True


__all__ = ["install_fast_cbor"]
