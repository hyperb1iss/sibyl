import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'lateness': 2, 'gap': 2, 'samples': [{'sensor': 'a', 'tick': 1}, {'sensor': 'a', 'tick': 2}]}, 'expected': {'sessions': [{'sensor': 'a', 'first': 1, 'last': 2, 'n': 2}], 'rejected': 0}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
