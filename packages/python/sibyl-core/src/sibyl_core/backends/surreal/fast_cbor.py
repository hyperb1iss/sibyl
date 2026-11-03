"""Decode SurrealDB responses with cbor2's C decoder when it is installed.

The SDK decodes every response with a pure-Python copy of cbor2, and on a
large read that costs more than the query itself. cbor2's C build turns the
same bytes into the same values through the SDK's own tag decoder, a few
times faster. SDK 3 transports import ``surrealdb.data.cbor.decode`` by name,
so the replacement is installed in each module that holds that name.

The C decoder reads a CBOR null as ``None``, as SDK 2 did; SDK 3's own decoder
returns its ``Null`` sentinel instead.

Installing is explicit and idempotent. Without cbor2's C build it does
nothing and the SDK keeps its own decoder.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

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
    from surrealdb.data import cbor as sdk_cbor
    from surrealdb.data.types import constants

    tag_type = cbor2.CBORTag
    # Compact datetimes and durations carry two numbers the SDK only indexes;
    # they are most of the tags in a response, so they skip the thaw.
    numeric_pairs = {constants.TAG_DATETIME_COMPACT, constants.TAG_DURATION_COMPACT}
    frozen_map = getattr(cbor2, "frozendict", None) or getattr(cbor2, "FrozenDict", dict)
    c_loads = cbor2.loads
    sdk_tag_decoder = sdk_cbor.tag_decoder

    def thaw(value: Any) -> Any:
        # The C decoder freezes whatever sits inside a tag (arrays become
        # tuples, maps become frozen); the SDK's decoder hands over lists and
        # dicts. CBOR has no tuple, so this restores exactly what was sent.
        if isinstance(value, tuple):
            return [thaw(item) for item in value]
        if isinstance(value, frozen_map):
            return {key: thaw(item) for key, item in value.items()}
        return value

    def hook(*args: Any) -> Any:
        # cbor2 releases disagree on the hook's other arguments; the tag is
        # the only one the SDK's decoder reads.
        tag = next(arg for arg in args if isinstance(arg, tag_type))
        if tag.tag not in numeric_pairs and isinstance(tag.value, (tuple, frozen_map)):
            tag = tag_type(tag.tag, thaw(tag.value))
        return sdk_tag_decoder(None, tag)

    def decode(data: bytes) -> Any:
        return c_loads(data, tag_hook=hook)

    for name in (
        "surrealdb.data.cbor",
        "surrealdb.connections.async_ws",
        "surrealdb.connections.async_embedded",
        "surrealdb.connections.blocking_ws",
        "surrealdb.connections.blocking_embedded",
        "surrealdb.connections.utils_mixin",
    ):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, "decode"):
            # setattr: this deliberately rebinds the SDK module's own function.
            setattr(module, "decode", decode)  # noqa: B010
    _installed = True
    return True


__all__ = ["install_fast_cbor"]
