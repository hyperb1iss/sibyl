Repair a record-separator channel. Input {packets:[[byte_values]]} form one strict UTF-8 stream. ASCII RS(byte30), not LF, separates JSON records. Whitespace-only segments ignored; JSON records may contain formatting newlines. Parse final nonblank segment. Any invalid UTF-8 or JSON makes return {messages:null}; otherwise {messages:list}. Packet boundaries have no record meaning.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
