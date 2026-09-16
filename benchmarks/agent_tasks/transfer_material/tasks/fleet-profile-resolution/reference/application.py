from resolver import combine
def solve(d):
 return {'nodes':{n['id']:combine([d['base']]+[d['profiles'].get(x,{}) for x in n['use']]+[n['patch']]) for n in d['nodes']}}
