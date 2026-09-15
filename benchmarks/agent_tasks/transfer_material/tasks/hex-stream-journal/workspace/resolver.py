import json

def decode(chunks,separator='\n'):
 try:
  text=''.join(bytes(c).decode('utf-8','strict') for c in chunks)
  return [json.loads(part) for part in text.split(separator) if part.strip()]
 except (UnicodeError,ValueError,TypeError):return None
