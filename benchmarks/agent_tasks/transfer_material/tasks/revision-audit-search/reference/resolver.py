def audit(records,query):
 terms=set(query.casefold().split())
 return [i for i,row in enumerate(records) if terms<=set(row[3].casefold().split())]
