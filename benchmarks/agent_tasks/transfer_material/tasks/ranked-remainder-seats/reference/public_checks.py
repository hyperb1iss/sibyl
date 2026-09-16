import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'seats': 4, 'candidates': [{'name': 'a', 'weight': 1}, {'name': 'b', 'weight': 1}], 'tie_order': ['a', 'b']}, 'expected': {'seats': [{'name': 'a', 'count': 2}, {'name': 'b', 'count': 2}]}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
