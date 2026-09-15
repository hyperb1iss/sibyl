import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'gap': 2, 'ticks': [1, 2, 5]}, 'expected': {'bursts': [[1, 2], [5]]}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
