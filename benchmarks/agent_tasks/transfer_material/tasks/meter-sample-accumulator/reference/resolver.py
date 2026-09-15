def total(rows):
 sums={}
 for r in rows:sums[r['device']]=sums.get(r['device'],0)+r['units']
 return sums
