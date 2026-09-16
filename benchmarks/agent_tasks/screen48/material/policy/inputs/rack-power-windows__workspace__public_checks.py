import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'watts': 5, 'reservations': [[0, 2, 3]]}, 'expected': {'peak': 3, 'windows': []}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
