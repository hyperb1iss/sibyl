import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'roots': {'red': '/srv/red'}, 'jobs': [['red', '/a']]}, 'expected': {'files': ['/srv/red/a'], 'rejected': []}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
