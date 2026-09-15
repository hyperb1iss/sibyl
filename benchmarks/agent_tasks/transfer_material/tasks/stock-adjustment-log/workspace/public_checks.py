import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'stock': {}, 'changes': [['a', 'x', '1', 3]]}, 'expected': {'stock': {'a/x': 3}, 'verdicts': ['applied']}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
