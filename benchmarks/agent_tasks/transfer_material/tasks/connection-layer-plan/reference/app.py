import json,sys
from application import solve
if __name__=='__main__':
 print(json.dumps(solve(json.load(sys.stdin)),ensure_ascii=False,allow_nan=False))
