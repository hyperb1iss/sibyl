import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'layers': [{'a': 1}, {'a': 2}]}, 'expected': {'record': {'a': 2}}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
