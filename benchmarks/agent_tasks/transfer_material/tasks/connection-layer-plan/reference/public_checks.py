import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'layers': [{'endpoint': 'host', 'retries': 2, 'enabled': True}]}, 'expected': {'settings': {'endpoint': 'host', 'retries': 2, 'enabled': True}}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
