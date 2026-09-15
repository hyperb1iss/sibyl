import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'lag': 2, 'break_after': 3, 'arrivals': [['a', 1], ['a', 2]]}, 'expected': {'ranges': [['a', 1, 2, 2]], 'dropped': 0}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
