import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'checkpoint': {'balances': {}, 'accepted': {}}, 'batch': [{'org': 'a', 'wallet': 'w', 'key': 'k', 'delta': 2}]}, 'expected': {'balances': {'a/w': 2}, 'accepted': {'a/k': ['w', 2]}, 'status': ['applied']}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
