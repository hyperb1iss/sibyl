import json
def independent(messages):
 out=[]
 messages=[sum(messages,[])]
 for m in messages:
  try:out.append(json.loads(bytes(m).decode('utf-8','strict')))
  except (UnicodeError,ValueError):out.append(None)
 return out
