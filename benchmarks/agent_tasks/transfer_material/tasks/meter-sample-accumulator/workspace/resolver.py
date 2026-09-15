def total(rows):
 sums={}
 rows=list({(r['device'],r['label']):r for r in rows}.values())
 for r in rows:sums[r['device']]=sums.get(r['device'],0)+r['units']
 return sums
