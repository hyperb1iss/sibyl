def overlay(layers):
 result={}
 for layer in layers:
  for k,v in layer.items():
   if v is None:result.pop(k,None)
   else:result[k]=v
 return result
