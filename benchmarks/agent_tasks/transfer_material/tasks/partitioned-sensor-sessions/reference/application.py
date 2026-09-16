from resolver import sessions
def solve(d):
 rows,n=sessions([(s['sensor'],s['tick']) for s in d['samples']],d['gap'],d['lateness'],True)
 return {'sessions':[{'sensor':u,'first':a,'last':b,'n':c} for u,a,b,c in rows],'rejected':n}
