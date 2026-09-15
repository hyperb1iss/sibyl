Repair independent transport messages. Input {messages:[[byte_values]]}; each item is a complete separate UTF-8 JSON message. Decode and parse each independently; return {values:[parsed_value_or_null]}. A malformed item must not poison neighbors. Never concatenate messages to complete Unicode or JSON across boundaries.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
