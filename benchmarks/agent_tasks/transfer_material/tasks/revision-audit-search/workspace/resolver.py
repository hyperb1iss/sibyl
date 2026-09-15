def audit(records,query):
 terms=set(query.casefold().split())
 latest={row[0]:i for i,row in enumerate(records)}
 return [i for i,row in enumerate(records) if i==latest[row[0]] and not row[2] and terms<=set(row[3].casefold().split())]
