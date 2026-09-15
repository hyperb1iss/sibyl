import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'units': 4, 'weights': {'a': 1, 'b': 1}}, 'expected': {'units_by_department': {'a': 2, 'b': 2}}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
