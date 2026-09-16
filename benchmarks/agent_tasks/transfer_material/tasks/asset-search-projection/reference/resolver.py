def project(events,queries):
 latest={}
 for key,rev,removed,text in events:
  if key not in latest or rev>=latest[key][0]:latest[key]=(rev,removed,text)
 answers=[]
 for q in queries:
  terms=set(q.casefold().split())
  answers.append(sorted(k for k,(_,gone,text) in latest.items() if not gone and terms<=set(text.casefold().split())))
 return answers
