from resolver import allocate
def solve(d):
 a=allocate(d['seats'],{x['name']:x['weight'] for x in d['candidates']},d['tie_order'])
 return {'seats':[{'name':x['name'],'count':a[x['name']]} for x in d['candidates']]}
