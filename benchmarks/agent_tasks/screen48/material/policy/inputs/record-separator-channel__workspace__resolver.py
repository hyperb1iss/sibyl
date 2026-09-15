import json

def decode(chunks,separator='\n'):
 try:
  text=b''.join(bytes(c) for c in chunks).decode('utf-8','strict')
  return [json.loads(part) for part in text.split('\n') if part.strip()]
 except (UnicodeError,ValueError,TypeError):return None
