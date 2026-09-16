import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'objects': {'a': 7}, 'lookups': ['a', 'b']}, 'expected': {'values': [7, None]}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
