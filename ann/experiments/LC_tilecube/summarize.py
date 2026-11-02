import csv
from collections import defaultdict
rows = list(csv.DictReader(open('exp_results.csv')))
agg = defaultdict(lambda: defaultdict(list))
for r in rows:
    key = (r['dataset'], r['proj'])
    agg[key]['probe'].append(float(r['t_probe_ms']))
    agg[key]['fetch'].append(float(r['t_fetch_ms']))
    agg[key]['eval'].append(float(r['t_eval_s']))
for k in sorted(agg):
    a = agg[k]
    probe = sum(a['probe']) / len(a['probe'])
    fetch = sum(a['fetch']) / len(a['fetch'])
    ev = sum(a['eval']) / len(a['eval'])
    print('%-22s proj=%-4s probe=%.4f ms/q  fetch=%.4f ms/bucket  eval=%.2f s' % (k[0], k[1], probe, fetch, ev))