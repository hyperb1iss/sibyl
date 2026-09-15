from resolver import sessions
def solve(d):
 r,n=sessions(d['arrivals'],d['break_after'],d['lag']);return {'ranges':r,'dropped':n}
