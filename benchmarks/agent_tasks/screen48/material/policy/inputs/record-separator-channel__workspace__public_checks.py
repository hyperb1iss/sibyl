import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'packets': [[49]]}, 'expected': {'messages': [1]}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
