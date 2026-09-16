import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'rooms': [{'name': 'a', 'limit': 5, 'bookings': [{'at': 0, 'until': 2, 'people': 2}]}]}, 'expected': {'reports': {'a': {'peak': 2, 'windows': []}}}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
