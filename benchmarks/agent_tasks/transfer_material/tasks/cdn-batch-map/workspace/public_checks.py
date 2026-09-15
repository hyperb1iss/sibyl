import json,subprocess,sys
cases=[{'id': 'public-simple', 'input': {'base': '/opt/site', 'requests': [{'tag': 'a', 'url': '/news/item'}]}, 'expected': {'mapped': [{'tag': 'a', 'destination': '/opt/site/news/item'}]}}]
for c in cases:
 p=subprocess.run([sys.executable,'app.py'],input=json.dumps(c['input']),text=True,capture_output=True,check=True)
 assert json.loads(p.stdout)==c['expected']
print('public checks passed')
