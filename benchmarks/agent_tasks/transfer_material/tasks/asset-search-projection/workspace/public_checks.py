import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'updates': [['a', 1, False, 'Blue bolt']], 'terms': ['blue']}, 'expected': {'hits': [['a']]}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
