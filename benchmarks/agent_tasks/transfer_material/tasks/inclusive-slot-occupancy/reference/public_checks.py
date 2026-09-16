import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'limit': 3, 'ranges': [[0, 2, 1]]}, 'expected': {'peak': 1, 'over': []}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
