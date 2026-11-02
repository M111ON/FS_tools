import csv
from collections import defaultdict
rows = list(csv.DictReader(open('exp_results.csv')))

# group by dataset
by_ds = defaultdict(list)
for r in rows:
    by_ds[r['dataset']].append(r)

def best_recall(ds_rows, proj, L, K, budget):
    vs = [r for r in ds_rows if r['proj']==proj and int(r['L'])==L and int(r['K'])==K and float(r['scan'])*100 <= budget]
    if not vs: return None, None
    r = max(vs, key=lambda x: float(x['recall']))
    return float(r['recall']), float(r['scan'])*100

out = []
out.append('# LC_tilecube experiment report')
out.append('')
out.append('Data: various synthetic + real (sift 128D, beir 384D). Method: lc_label_bucket (Label 3D projection + BCC bucket, 15 doors).')
out.append('Metric: recall@1 = P(true NN in candidate set), scan% = candidate/n, lift = recall/scan.')
out.append('')
# latency summary
out.append('## Latency (per query, vectorized numpy)')
out.append('')
out.append('| dataset | dim | proj | probe ms/q | fetch ms/bucket |')
out.append('|---|---|---|---|---|')
agg = defaultdict(lambda: defaultdict(list))
for r in rows:
    agg[(r['dataset'],r['proj'])]['probe'].append(float(r['t_probe_ms']))
    agg[(r['dataset'],r['proj'])]['fetch'].append(float(r['t_fetch_ms']))
for k in sorted(agg):
    a=agg[k]
    out.append('| %s | %s | %s | %.4f | %.4f |' % (k[0], [r for r in rows if r['dataset']==k[0]][0]['dim'], k[1], sum(a['probe'])/len(a['probe']), sum(a['fetch'])/len(a['fetch'])))

out.append('')
out.append('## Recall by dataset / dim')
out.append('')
for ds in sorted(by_ds):
    dsr = by_ds[ds]
    dim = dsr[0]['dim']
    n = dsr[0]['n']; nq = dsr[0]['nq']
    out.append('### %s  (dim=%s, n=%s, nq=%s)' % (ds, dim, n, nq))
    out.append('')
    out.append('| proj | L | K | best recall @scan<=1% | scan% | best recall @scan<=2% | scan% |')
    out.append('|---|---|---|---|---|---|---|')
    for proj in sorted(set(r['proj'] for r in dsr)):
        for L in (1,4):
            for K in (1,4,8,15):
                r1, s1 = best_recall(dsr, proj, L, K, 1.0)
                r2, s2 = best_recall(dsr, proj, L, K, 2.0)
                if r1 is None and r2 is None: 
                    continue
                out.append('| %s | %d | %d | %s | %s | %s | %s |' % (
                    proj, L, K,
                    ('%.3f'%r1) if r1 is not None else '-',
                    ('%.2f'%s1) if s1 is not None else '-',
                    ('%.3f'%r2) if r2 is not None else '-',
                    ('%.2f'%s2) if s2 is not None else '-'))
    out.append('')

with open('REPORT.md','w') as f:
    f.write('\n'.join(out))
print('wrote REPORT.md')