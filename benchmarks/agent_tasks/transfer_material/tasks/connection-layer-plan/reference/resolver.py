def combine(layers):
 out={}
 for layer in layers:
  for k,v in layer.items():
   if v is None:out.pop(k,None)
   else:out[k]=v
 if set(out)-{'endpoint','retries','enabled','note'}:return None
 if not isinstance(out.get('endpoint'),str) or not out['endpoint']:return None
 if type(out.get('retries')) is not int or out['retries']<0:return None
 if type(out.get('enabled')) is not bool:return None
 if 'note' in out and not isinstance(out['note'],str):return None
 return out
