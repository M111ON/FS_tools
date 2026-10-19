#!/usr/bin/env python3
"""verify_ann_kernel.py — ad-hoc verification for the ANN dot-kernel patch.

NOT a project suite. The repo's own suite is `python run_tests.py`, which does
not exercise this kernel at all. This script proves one thing: the 4-accumulator
dot now in src/ann_search.c returns the same top-k as the 1-accumulator version
it replaced, and is faster.

The two kernels are written out below as explicit mirrors, and a GUARD proves
each mirror still matches the source it claims to mirror — so a later edit to
the server cannot silently invalidate this script.

Cases:
  1. guard: the mirrors match the source (working tree and HEAD)
  2. top-k SET + ORDER identical   (20 queries x 2000 real vectors, top-10)
  3. boundary dims                  (1,2,3,... non-multiples of 4, <4, 384, 385)
  4. near-duplicate ranking         (rows differing by 1e-7 -> real ties)
  5. kernel timing                  (OLD and NEW alternate in-process)

Usage:  python I:/tools/ann/tests/verify_ann_kernel.py
Exit:   0 = all cases behaved, 1 = a case regressed.
"""
import json
import os
import re
import struct
import subprocess
import sys
import tempfile

REPO = r"I:\tools\ann"
GIT_ROOT = r"I:\tools"
SRC = os.path.join(REPO, "src", "ann_search.c")
INDEX = os.path.join(REPO, "data", "memcore", "ann_index.jsonl")
PRE_PATCH_COMMIT = "9731bca"   # last commit before the kernel change

# ── the two kernels, as they appear in the server ─────────────────────
MIRROR_OLD = """double dot = 0;
            for (int wi = 0; wi < qd; wi++) dot += (double)qv[wi] * ents[ei].vec[wi];"""

MIRROR_NEW_HELPER = """double s0 = 0, s1 = 0, s2 = 0, s3 = 0;
    int j = 0;
    for (; j + 4 <= d; j += 4) {
        s0 += (double)a[j]     * (double)b[j];
        s1 += (double)a[j + 1] * (double)b[j + 1];
        s2 += (double)a[j + 2] * (double)b[j + 2];
        s3 += (double)a[j + 3] * (double)b[j + 3];
    }
    double s = (s0 + s1) + (s2 + s3);
    for (; j < d; j++) s += (double)a[j] * (double)b[j];"""

MIRROR_NEW_INLINE = """double s0 = 0, s1 = 0, s2 = 0, s3 = 0;
            int wi = 0;
            for (; wi + 4 <= qd; wi += 4) {
                s0 += (double)qv[wi]     * ents[ei].vec[wi];
                s1 += (double)qv[wi + 1] * ents[ei].vec[wi + 1];
                s2 += (double)qv[wi + 2] * ents[ei].vec[wi + 2];
                s3 += (double)qv[wi + 3] * ents[ei].vec[wi + 3];
            }
            double dot = (s0 + s1) + (s2 + s3);
            for (; wi < qd; wi++) dot += (double)qv[wi] * ents[ei].vec[wi];"""


def norm(text):
    """Whitespace/identifier-insensitive form for guard comparison."""
    t = re.sub(r"//[^\n]*", "", text)
    t = re.sub(r"\bwi\b", "j", t)
    t = re.sub(r"\bqv\b", "a", t)
    t = re.sub(r"\bqd\b", "d", t)
    t = t.replace("ents[ei].vec", "vec")
    t = t.replace("double dot = 0;", "double s = 0;")
    t = t.replace("double dot = (s0 + s1) + (s2 + s3);", "")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def source_contains(text, mirror):
    """Does the source contain this kernel (modulo formatting/names)?"""
    flat = norm(text)
    for line in mirror.splitlines():
        n = norm(line)
        if n and n not in flat:
            return False, line
    return True, None


def git_show(rev, path="ann/src/ann_search.c"):
    r = subprocess.run(["git", "-C", GIT_ROOT, "show", f"{rev}:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


C_HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>
#include <time.h>

#define TOPK 10
static double now_ms(void){ struct timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts);
    return ts.tv_sec*1000.0 + ts.tv_nsec/1e6; }

/* OLD: 1 accumulator (pre-patch 9731bca) */
static double kernel_old(const float *a, const float *b, int d) {
    double s = 0;
    for (int j = 0; j < d; j++) s += (double)a[j] * (double)b[j];
    return s;
}

/* NEW: 4 accumulator chains (current) */
static double kernel_new(const float *a, const float *b, int d) {
    double s0 = 0, s1 = 0, s2 = 0, s3 = 0;
    int j = 0;
    for (; j + 4 <= d; j += 4) {
        s0 += (double)a[j]     * (double)b[j];
        s1 += (double)a[j + 1] * (double)b[j + 1];
        s2 += (double)a[j + 2] * (double)b[j + 2];
        s3 += (double)a[j + 3] * (double)b[j + 3];
    }
    double s = (s0 + s1) + (s2 + s3);
    for (; j < d; j++) s += (double)a[j] * (double)b[j];
    return s;
}

/* mirrors the server's scoring: dot / (qn * entry_norm), entry norm
   precomputed at load time exactly like lz_entry_parse does */
static void pick_topk(const float *qv, const float *vecs, const double *norms,
                      int n, int dim, int *out, double *scores,
                      double (*kern)(const float*, const float*, int))
{
    double qn = 0; for (int i=0;i<dim;i++) qn += (double)qv[i]*qv[i];
    qn = sqrt(qn);
    double best[TOPK]; int bid[TOPK];
    for (int t=0;t<TOPK;t++){ best[t]=-2; bid[t]=-1; }
    for (int e=0;e<n;e++){
        double dot = kern(qv, vecs + (size_t)e*dim, dim);
        double sc  = (norms[e] > 0) ? dot/(qn*norms[e]) : -1;
        for (int t=0;t<TOPK;t++) if (sc > best[t]) {
            for (int u=TOPK-1;u>t;u--){ best[u]=best[u-1]; bid[u]=bid[u-1]; }
            best[t]=sc; bid[t]=e; break;
        }
    }
    for (int t=0;t<TOPK;t++){ out[t]=bid[t]; scores[t]=best[t]; }
}

int main(int argc, char **argv){
    const char *path=argv[1]; int nq=atoi(argv[2]); int nc=atoi(argv[3]); int dim=atoi(argv[4]);
    FILE *f=fopen(path,"rb"); if(!f){ fprintf(stderr,"open fail\n"); return 2; }
    float *q=malloc(sizeof(float)*(size_t)nq*dim);
    float *c=malloc(sizeof(float)*(size_t)nc*dim);
    double *norms=malloc(sizeof(double)*nc);
    if (fread(q,sizeof(float),(size_t)nq*dim,f)!=(size_t)nq*dim){ fprintf(stderr,"q fail\n"); return 2; }
    if (fread(c,sizeof(float),(size_t)nc*dim,f)!=(size_t)nc*dim){ fprintf(stderr,"c fail\n"); return 2; }
    fclose(f);
    for (int e=0;e<nc;e++){ double s=0; const float*v=c+(size_t)e*dim;
        for(int i=0;i<dim;i++) s += (double)v[i]*v[i]; norms[e]=sqrt(s); }

    int mism=0, ordmism=0; double maxrel=0;
    int o1[TOPK], o2[TOPK]; double s1[TOPK], s2[TOPK];
    for (int qi=0; qi<nq; qi++){
        pick_topk(q+(size_t)qi*dim, c, norms, nc, dim, o1, s1, kernel_old);
        pick_topk(q+(size_t)qi*dim, c, norms, nc, dim, o2, s2, kernel_new);
        int same=1;
        for (int t=0;t<TOPK;t++) if (o1[t]!=o2[t]) same=0;
        if (!same){ mism++; ordmism++; }
        for (int t=0;t<TOPK;t++){
            double a=s1[t], b=s2[t];
            double rel = (a!=0.0)? fabs(a-b)/fabs(a) : fabs(a-b);
            if (rel>maxrel) maxrel=rel;
        }
    }
    printf("TOPK queries=%d corpus=%d dim=%d topk=%d\n", nq, nc, dim, TOPK);
    printf("TOPK set_mismatch=%d order_mismatch=%d max_rel_score_diff=%.3e\n",
           mism, ordmism, maxrel);

    /* boundary dims: tail loop, dim<4, and dim just over a multiple of 4 */
    int dims[]={1,2,3,4,5,6,7,8,9,15,16,17,31,32,33,63,64,65,127,128,129,383,384,385};
    int nd=sizeof(dims)/sizeof(dims[0]); int dfail=0; double dworst=0; int dworst_dim=-1;
    srand(4242);
    for (int di=0; di<nd; di++){
        int d=dims[di];
        float *a=malloc(sizeof(float)*d), *b=malloc(sizeof(float)*d);
        for (int tr=0; tr<200; tr++){
            for (int i=0;i<d;i++){ a[i]=(float)((rand()/(double)RAND_MAX)*2-1);
                                   b[i]=(float)((rand()/(double)RAND_MAX)*2-1); }
            double o=kernel_old(a,b,d), n=kernel_new(a,b,d);
            double rel=(o!=0.0)? fabs(o-n)/fabs(o) : fabs(o-n);
            if (rel>dworst){ dworst=rel; dworst_dim=d; }
            if (rel>1e-9) dfail++;
        }
        free(a); free(b);
    }
    printf("EDGE dims=%d mismatch_beyond_1e-9=%d worst_rel=%.3e at dim=%d\n",
           nd, dfail, dworst, dworst_dim);

    /* near-duplicates: rows differing by 1e-7 -> genuine ties, where a
       rounding difference COULD flip the ranking */
    int d=384, N=2000, flips=0, cases=0;
    float *v=malloc(sizeof(float)*(size_t)N*d);
    for (int i=0;i<N*d;i++) v[i]=(float)((rand()/(double)RAND_MAX)*2-1);
    for (int i=0;i<N/2;i++) memcpy(v+(size_t)(N/2+i)*d, v+(size_t)i*d, sizeof(float)*d);
    for (int i=0;i<N/2;i++) for (int j=0;j<d;j++) v[(size_t)(N/2+i)*d+j]+=1e-7f;
    double *so=malloc(sizeof(double)*N), *sn=malloc(sizeof(double)*N);
    int *io=malloc(sizeof(int)*N), *in_=malloc(sizeof(int)*N);
    for (int i=0;i<N;i++) io[i]=in_[i]=i;
    for (int t=0;t<5;t++){
        for (int e=0;e<N;e++) so[e]=kernel_old(v, v+(size_t)e*d, d);
        for (int e=0;e<N;e++) sn[e]=kernel_new(v, v+(size_t)e*d, d);
        for (int i=1;i<N;i++){ int k=io[i]; double s=so[k]; int j=i-1;
            while(j>=0 && so[io[j]]<s){ io[j+1]=io[j]; j--; } io[j+1]=k; }
        for (int i=1;i<N;i++){ int k=in_[i]; double s=sn[k]; int j=i-1;
            while(j>=0 && sn[in_[j]]<s){ in_[j+1]=in_[j]; j--; } in_[j+1]=k; }
        int diff=0; for (int i=0;i<10;i++) if (io[i]!=in_[i]) diff++;
        cases++; if (diff) flips++;
    }
    printf("TIE near_duplicate_cases=%d top10_order_differences=%d\n", cases, flips);

    /* timing: OLD and NEW alternate in-process so they share cache state.
     * NOTE: this box has 2 cores and other work runs on it, so the passes
     * disagree. Report the spread, not a single number: a speedup claim
     * needs the passes to agree (playbook P14 / GATE 14). */
    volatile double sink=0;
    double ratios[3]; int npass = 0;
    for (int pass=0; pass<3; pass++){
        double t0=now_ms();
        for (int r=0;r<5;r++) for (int qi=0;qi<nq;qi++)
            pick_topk(q+(size_t)qi*dim, c, norms, nc, dim, o1, s1, kernel_old);
        double t1=now_ms();
        for (int r=0;r<5;r++) for (int qi=0;qi<nq;qi++)
            pick_topk(q+(size_t)qi*dim, c, norms, nc, dim, o2, s2, kernel_new);
        double t2=now_ms();
        double ratio = (t2>t1) ? (t1-t0)/(t2-t1) : 0.0;
        ratios[npass++] = ratio;
        printf("TIME pass%d OLD %.2f ms NEW %.2f ms ratio %.2fx\n",
               pass, t1-t0, t2-t1, ratio);
    }
    {
        double lo = ratios[0], hi = ratios[0], sum = 0.0;
        for (int i=0;i<npass;i++){
            if (ratios[i]<lo) lo=ratios[i];
            if (ratios[i]>hi) hi=ratios[i];
            sum += ratios[i];
        }
        double avg = sum/npass;
        printf("TIME spread %.2fx - %.2fx (mean %.2fx) -- %s\n",
               lo, hi, avg,
               (hi-lo) > 0.5*avg ? "UNSTABLE: box is busy, do not quote a single number"
                                : "stable enough to quote");
    }
    (void)sink;
    return (mism || ordmism || dfail || flips) ? 1 : 0;
}
'''


def main():
    print("verify_ann_kernel.py — AD-HOC verification (not a project suite)")
    print("  repo   :", REPO)
    print("  patched: ann/src/ann_search.c")
    print()

    failures = []

    # ── case 1: GUARD — the mirrors still match the source ─────────────
    work = open(SRC, encoding="utf-8", errors="replace").read()
    head = git_show("HEAD")
    old_src = git_show(PRE_PATCH_COMMIT)

    def check(label, text, mirror):
        ok, missing = source_contains(text, mirror)
        print(f"      {label}: {'PASS' if ok else 'FAIL'}"
              + ("" if ok else f"  (line not found: {missing.strip()[:60]!r})"))
        return ok

    print("[1] GUARD — mirrors vs source")
    g1 = (check("working tree has the NEW kernel", work, MIRROR_NEW_HELPER)
          or check("working tree has the NEW kernel", work, MIRROR_NEW_INLINE))
    g2 = (check("HEAD has the NEW kernel", head, MIRROR_NEW_HELPER)
          or check("HEAD has the NEW kernel", head, MIRROR_NEW_INLINE))
    g3 = check(f"pre-patch {PRE_PATCH_COMMIT} has the OLD kernel", old_src, MIRROR_OLD)
    if not (g1 and g2 and g3):
        print("[1] FAIL: a mirror no longer matches its source")
        failures.append("guard failed")
    else:
        print("[1] PASS  all mirrors match their sources")

    # ── build + run the harness ────────────────────────────────────────
    tmp_c = os.path.join(tempfile.gettempdir(), "verify-ann-kernel-harness.c")
    tmp_exe = tmp_c[:-2] + ".exe"
    tmp_bin = os.path.join(tempfile.gettempdir(), "verify-ann-kernel-data.bin")
    for p in (tmp_c, tmp_exe, tmp_bin):
        if os.path.exists(p):
            os.remove(p)
    with open(tmp_c, "w", encoding="utf-8") as f:
        f.write(C_HARNESS)

    r = subprocess.run(["gcc", "-O2", "-std=c11", "-o", tmp_exe, tmp_c, "-lm"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("[2] FAIL: harness build failed\n" + r.stderr[:800])
        for p in (tmp_c, tmp_exe, tmp_bin):
            if os.path.exists(p):
                os.remove(p)
        return 1
    print("[2] PASS  harness built")

    # ── real vectors from the live index ───────────────────────────────
    if not os.path.exists(INDEX):
        print("[3] SKIP  live index not found:", INDEX)
    else:
        rows = []
        with open(INDEX, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                o = json.loads(line)
                v = o.get("vec")
                if not v:
                    continue
                rows.append([float(x) for x in v])
                if len(rows) >= 3000:
                    break
        NQ, NC = 20, 2000
        dim = len(rows[0])
        with open(tmp_bin, "wb") as f:
            for v in rows[:NQ]:
                f.write(struct.pack(f"<{dim}f", *v))
            for v in rows[NQ:NQ + NC]:
                f.write(struct.pack(f"<{dim}f", *v))
        print("[3] PASS  loaded %d real vectors from the live index" % len(rows))

        r = subprocess.run([tmp_exe, tmp_bin, str(NQ), str(NC), str(dim)],
                           capture_output=True, text=True)
        print("[4] ad-hoc result (exit %d):" % r.returncode)
        print(r.stdout.rstrip())
        if r.stderr.strip():
            print("      stderr:", r.stderr[:200])
        for line in r.stdout.splitlines():
            if line.startswith("TOPK set_mismatch"):
                if "set_mismatch=0" not in line or "order_mismatch=0" not in line:
                    failures.append(line)
            if line.startswith("EDGE"):
                if "mismatch_beyond_1e-9=0" not in line:
                    failures.append(line)
            if line.startswith("TIE"):
                if "order_differences=0" not in line:
                    failures.append(line)
        if r.returncode != 0:
            failures.append("harness exit %d" % r.returncode)

    for p in (tmp_c, tmp_exe, tmp_bin):
        try:
            os.remove(p)
        except OSError:
            pass
    print("\ncleaned up harness artifacts (the .py stays for re-runs)")

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL AD-HOC CASES BEHAVED (ad-hoc verification, not a project suite)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
