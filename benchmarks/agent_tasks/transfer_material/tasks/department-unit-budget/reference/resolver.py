def allocate(total,weights,order=None):
 denom=sum(weights.values());shares={k:total*w//denom for k,w in weights.items()}
 ranks={k:i for i,k in enumerate(order)} if order is not None else {k:i for i,k in enumerate(sorted(weights))}
 priority=sorted(weights,key=lambda k:(-(total*weights[k]%denom),ranks[k]))
 for k in priority[:total-sum(shares.values())]:shares[k]+=1
 return shares
