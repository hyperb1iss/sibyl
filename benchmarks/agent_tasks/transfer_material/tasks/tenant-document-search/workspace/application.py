from resolver import project
def solve(d):
 return {'answers':[project([(e['key'],e['rev'],e['tombstone'],e['body']) for e in d['events'] if True],[q['words']])[0] for q in d['requests']]}
