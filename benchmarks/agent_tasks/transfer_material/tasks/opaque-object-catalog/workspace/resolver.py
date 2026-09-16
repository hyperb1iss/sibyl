def lookup(objects,key):
 from urllib.parse import unquote
 return objects.get(unquote(key))
