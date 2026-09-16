import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'available': 10, 'requests': [{'id': 'a', 'need': 3, 'priority': 1}]}, 'expected': {'granted': {'a': 3}, 'remaining': 7}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
