import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'base': {'endpoint': 'x', 'retries': 1, 'enabled': True}, 'profiles': {}, 'nodes': [{'id': 'a', 'use': [], 'patch': {}}]}, 'expected': {'nodes': {'a': {'endpoint': 'x', 'retries': 1, 'enabled': True}}}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
