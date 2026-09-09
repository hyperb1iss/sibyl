"""URL decoding order and lexical containment within a fixed document root."""

from .model import Family, case, source


def path_routing(seed: int) -> Family:
    contract = """# Static document router

Read a string `path` beginning with /. Strip the query and fragment before
percent-decoding. Split on literal /, then decode each segment exactly once with
strict UTF-8. Every percent escape must contain two hex digits. Reject decoded /
or backslash within a segment, any literal backslash, and NUL. Empty segments and
. are ignored; .. pops one segment, but attempting to pop an empty stack is an
error. Resolve remaining segments under the fixed root /srv/www and return
{"file": absolute_path}, or {"error": "invalid path"}. This is a lexical router;
no filesystem or symlink resolution occurs. A double-encoded escape is a literal
percent-containing filename after the single decoding pass.

Encoded dot segments and sibling-root paths currently escape containment or route
to the wrong file. Repair decoding and containment. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from paths import resolve

        def dispatch(request):
            try:
                return {"file": resolve(request["path"])}
            except ValueError:
                return {"error": "invalid path"}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    paths = source("""
        import posixpath
        from urllib.parse import unquote
        from segments import raw_path

        def resolve(path):
            normalized = posixpath.normpath("/srv/www/" + raw_path(path).lstrip("/"))
            return unquote(normalized)
    """)
    segments = source("""
        def raw_path(path):
            if not path.startswith("/"):
                raise ValueError("absolute URL path required")
            return path.split("#", 1)[0].split("?", 1)[0]
    """)
    partial = paths.replace(
        'raw_path(path).lstrip("/")', 'unquote(raw_path(path)).lstrip("/")'
    ).replace(
        "    return unquote(normalized)",
        '    if not normalized.startswith("/srv/www"):\n        raise ValueError("outside root")\n    return normalized',
    )
    fixed_paths = source("""
        from segments import decoded_segments

        def resolve(path):
            stack = []
            for segment in decoded_segments(path):
                if segment in ("", "."):
                    continue
                if segment == "..":
                    if not stack:
                        raise ValueError("outside root")
                    stack.pop()
                else:
                    stack.append(segment)
            return "/srv/www" + ("/" + "/".join(stack) if stack else "")
    """)
    fixed_segments = segments + source("""

        import re
        from urllib.parse import unquote

        def decoded_segments(path):
            raw = raw_path(path)
            if "\\\\" in raw or re.search(r"%(?![0-9a-fA-F]{2})", raw):
                raise ValueError("invalid encoding")
            result = [unquote(segment, errors="strict") for segment in raw.split("/")]
            if any("/" in segment or "\\\\" in segment or "\\x00" in segment for segment in result):
                raise ValueError("invalid separator")
            return result
    """)
    return Family(
        "decoded-root-routing",
        "learning",
        "document-root-url-router-v1",
        contract,
        {"app.py": app, "paths.py": paths, "segments.py": segments},
        {"paths.py": fixed_paths, "segments.py": fixed_segments},
        {"paths.py": partial},
        [
            case(
                "public-decoding-order",
                {"path": f"/a/%2e%2e/report-{seed}?ignored=1"},
                {"file": f"/srv/www/report-{seed}"},
            ),
            case("public-unicode", {"path": "/caf%C3%A9"}, {"file": "/srv/www/café"}),
        ],
        [
            case(
                "private-sibling-prefix",
                {"path": "/../../srv/www2/secret"},
                {"error": "invalid path"},
            ),
            case("private-encoded-separator", {"path": "/safe%2fchild"}, {"error": "invalid path"}),
            case("private-backslash", {"path": "/safe%5cchild"}, {"error": "invalid path"}),
            case("private-malformed-escape", {"path": "/bad%ZZ"}, {"error": "invalid path"}),
            case("private-invalid-utf8", {"path": "/bad%ff"}, {"error": "invalid path"}),
            case("private-null", {"path": "/bad%00"}, {"error": "invalid path"}),
            case(
                "private-double-encoding",
                {"path": "/%252e%252e/data"},
                {"file": "/srv/www/%2e%2e/data"},
            ),
            case("private-root", {"path": "/a/..//."}, {"file": "/srv/www"}),
        ],
        mechanism_cluster="decode-before-root-containment",
    )
