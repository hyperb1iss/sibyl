from resolver import project
def solve(d):
 return {'answers':[project([(e['key'],e['rev'],e['tombstone'],e['body']) for e in d['events'] if e['tenant']==q['tenant']],[q['words']])[0] for q in d['requests']]}
