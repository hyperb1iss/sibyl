import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'events': [{'tenant': 'a', 'key': 'x', 'rev': 1, 'tombstone': False, 'body': 'red'}], 'requests': [{'tenant': 'a', 'words': 'red'}]}, 'expected': {'answers': [['x']]}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
