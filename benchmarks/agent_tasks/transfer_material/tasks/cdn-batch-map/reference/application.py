from resolver import resolve
def solve(d):
 return {'mapped':[{'tag':r['tag'],'destination':resolve(d['base'],r['url'])} for r in d['requests']]}
