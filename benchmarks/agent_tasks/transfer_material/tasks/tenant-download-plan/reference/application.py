from resolver import resolve
def solve(d):
 files=[resolve(d['roots'][t],p) if t in d['roots'] else None for t,p in d['jobs']]
 return {'files':files,'rejected':[i for i,p in enumerate(files) if p is None]}
