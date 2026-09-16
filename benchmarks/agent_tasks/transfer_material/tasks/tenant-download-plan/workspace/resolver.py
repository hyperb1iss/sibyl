import re
from urllib.parse import unquote_to_bytes

def resolve(root, raw):
    if not raw.startswith('/') or '\\' in raw:
        return None
    raw=raw.split('?',1)[0].split('#',1)[0]
    stack=[]
    for segment in raw.split('/'):
        if re.search(r'%(?![0-9a-fA-F]{2})',segment): return None
        try: segment=unquote_to_bytes(unquote_to_bytes(segment).decode('utf-8','strict')).decode('utf-8','strict')
        except UnicodeError: return None
        if any(x in segment for x in ('/', '\\', '\x00')): return None
        if segment in ('','.'): continue
        if segment=='..':
            if not stack: return None
            stack.pop()
        else: stack.append(segment)
    return root.rstrip('/')+('/'+'/'.join(stack) if stack else '')
