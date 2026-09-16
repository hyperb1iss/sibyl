Repair a UTF-8 JSON journal. Input {fragments:[hex_strings]} representing one byte stream. Fragments may split Unicode or records. LF delimits JSON values; whitespace-only lines ignored, CRLF supported, final nonblank record parsed without LF. Any invalid/incomplete UTF-8 or malformed JSON invalidates the whole stream. Return {entries:list_or_null}. Hex strings are well formed.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
