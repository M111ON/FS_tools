/* bench_kernel.c — microbench: double+divide (lz_dot path) vs float*inv
 * (the ANN_FLOAT_KERNEL path) on this exact CPU (G4400, no AVX).
 * Synthetic: N=24000 vectors, D=384, realistic float magnitudes.
 * 3 reps each mode; prints per-mode mean/min ns-per-dot and a max_rel_diff
 * over the first 50 pairs (numerical honesty: the two paths are NOT
 * bit-identical).
 * Build:  gcc -O2 -std=c11 -o bench_kernel.exe bench_kernel.c -lm
 */
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <time.h>

#define N 24000
#define D 384
#define REPS 3
#define QN 8      /* simulated query vectors per rep */

static inline double lz_dot(const float *a, const float *b, int d) {
    double s0=0,s1=0,s2=0,s3=0; int j=0;
    for (; j+4<=d; j+=4) {
        s0 += (double)a[j]     * (double)b[j];
        s1 += (double)a[j+1]   * (double)b[j+1];
        s2 += (double)a[j+2]   * (double)b[j+2];
        s3 += (double)a[j+3]   * (double)b[j+3];
    }
    double s = (s0+s1)+(s2+s3);
    for (; j<d; j++) s += (double)a[j] * (double)b[j];
    return s;
}
static inline float lz_dotf(const float *a, const float *b, int d) {
    float s0=0,s1=0,s2=0,s3=0; int j=0;
    for (; j+4<=d; j+=4) {
        s0 += a[j]*b[j]; s1 += a[j+1]*b[j+1];
        s2 += a[j+2]*b[j+2]; s3 += a[j+3]*b[j+3];
    }
    float s = (s0+s1)+(s2+s3);
    for (; j<d; j++) s += a[j]*b[j];
    return s;
}

int main(void) {
    /* deterministic synthetic corpus */
    unsigned seed = 20261020;
    float *A = (float*)malloc((size_t)N * D * sizeof(float));
    float *nrm = (float*)malloc((size_t)N * sizeof(float));
    float *inv = (float*)malloc((size_t)N * sizeof(float));
    if (!A || !nrm || !inv) return 1;
    for (int i = 0; i < N; i++) {
        float n2 = 0;
        for (int d = 0; d < D; d++) {
            seed = seed * 1664525u + 1013904223u;
            float v = (float)((int)seed % 20000 - 10000) / 10000.0f;  /* [-1,1] */
            A[(size_t)i*D + d] = v;
            n2 += v * v;
        }
        nrm[i] = (float)sqrt((double)n2);
        inv[i] = (nrm[i] > 0) ? (float)(1.0 / nrm[i]) : 0.0f;
    }
    float *Q = (float*)malloc((size_t)QN * D * sizeof(float));
    double qn[QN];
    float  qinv[QN];
    for (int q = 0; q < QN; q++) {
        double n2 = 0;
        for (int d = 0; d < D; d++) {
            seed = seed * 1664525u + 1013904223u;
            Q[(size_t)q*D + d] = (float)((int)seed % 20000 - 10000) / 10000.0f;
            n2 += (double)Q[(size_t)q*D + d] * Q[(size_t)q*D + d];
        }
        qn[q] = sqrt(n2);
        qinv[q] = (qn[q] > 0) ? (float)(1.0 / qn[q]) : 0.0f;
    }

    volatile double sink = 0;   /* prevent the compiler from eliding the work */
    volatile float fsink = 0;
    printf("mode,reps,total_ms,ns_per_dot\n");
    double t_double[REPS], t_float[REPS];
    for (int r = 0; r < REPS; r++) {
        struct timespec t0, t1;
        /* double + div (the OLD hot loop) */
        clock_gettime(CLOCK_MONOTONIC, &t0);
        double acc = 0;
        for (int q = 0; q < QN; q++)
            for (int i = 0; i < N; i++) {
                double dot = lz_dot(&Q[(size_t)q*D], &A[(size_t)i*D], D);
                acc += (nrm[i] > 0) ? dot / (qn[q] * nrm[i]) : -1;
            }
        sink = acc;
        clock_gettime(CLOCK_MONOTONIC, &t1);
        t_double[r] = (t1.tv_sec-t0.tv_sec)*1e3 + (t1.tv_nsec-t0.tv_nsec)/1e6;
        /* float * recip (the NEW kernel) */
        clock_gettime(CLOCK_MONOTONIC, &t0);
        float facc = 0;
        for (int q = 0; q < QN; q++)
            for (int i = 0; i < N; i++) {
                float dot = lz_dotf(&Q[(size_t)q*D], &A[(size_t)i*D], D);
                facc += dot * qinv[q] * inv[i];
            }
        fsink = facc;
        clock_gettime(CLOCK_MONOTONIC, &t1);
        t_float[r] = (t1.tv_sec-t0.tv_sec)*1e3 + (t1.tv_nsec-t0.tv_nsec)/1e6;
    }
    double nd = (double)N * QN, total_dots = nd;
    for (int r = 0; r < REPS; r++)
        printf("double+div,%d,%.2f,%.1f\n", REPS, t_double[r], t_double[r]*1e6/total_dots);
    for (int r = 0; r < REPS; r++)
        printf("float*inv,%d,%.2f,%.1f\n", REPS, t_float[r], t_float[r]*1e6/total_dots);

    /* numerical honesty: max relative diff over 50 pairs */
    double max_rel = 0;
    for (int i = 0; i < 50; i++) {
        double d_old = 0;
        float  n2f = 0;
        for (int d = 0; d < D; d++) d_old += (double)Q[d] * (double)A[(size_t)i*D + d];
        /* reassociated double (4 chains, the LZ shape) */
        double s0=0,s1=0,s2=0,s3=0;
        for (int d = 0; d + 4 <= D; d += 4) {
            s0 += (double)Q[d]*A[(size_t)i*D+d];
            s1 += (double)Q[d+1]*A[(size_t)i*D+d+1];
            s2 += (double)Q[d+2]*A[(size_t)i*D+d+2];
            s3 += (double)Q[d+3]*A[(size_t)i*D+d+3];
        }
        d_old = (s0+s1)+(s2+s3);
        for (int d = D - (D%4); d < D; d++) d_old += (double)Q[d]*A[(size_t)i*D+d];
        float d_new = lz_dotf(Q, &A[(size_t)i*D], D);
        double sc_old = (nrm[i] > 0) ? d_old / (qn[0] * nrm[i]) : -1;
        double sc_new = (nrm[i] > 0) ? (double)(d_new * qinv[0] * inv[i]) : -1;
        double rel = (sc_old != 0) ? fabs(sc_old - sc_new) / fabs(sc_old) : fabs(sc_new);
        if (rel > max_rel) max_rel = rel;
    }
    printf("max_rel_diff_50pairs=%.3e\n", max_rel);
    (void)sink; (void)fsink;
    free(A); free(nrm); free(inv); free(Q);
    return 0;
}
