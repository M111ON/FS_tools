/* ═══════════════════════════════════════════════════════════════════
 * gpu_chain — resident chain: fp + transpose in one binary, one transfer pair
 * chain fp+transpose ไบนารีเดียว transfer คู่เดียว (resident บน device)
 * Pass1 fp_vec8 -> Pass2 transpose reorder. Phased events per stage.
 * ═══════════════════════════════════════════════════════════════════ */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#define CUDA_CHECK(call) do {                                          \
    cudaError_t _err_ = (call);                                        \
    if (_err_ != cudaSuccess) {                                        \
        fprintf(stderr, "CUDA ERROR [%s:%d] %s: %s\n",                 \
                __FILE__, __LINE__, #call, cudaGetErrorString(_err_)); \
        return -1;                                                     \
    }                                                                  \
} while (0)

#define CELL 144
#define ROW 32
#define COL 36
#define TSLOT 1152
#define NLINES 8
#define EV(n_, e_) CUDA_CHECK(cudaEventRecord(e_))

__global__ void fp_kernel(const uint8_t *data, int32_t *out,
                          uint32_t *xor_sink, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= n) return;
    const uint8_t *c = data + (size_t)i * CELL;
    int32_t v[9];
    for (int g = 0; g < 9; g++) {
        int32_t s = 0;
        uint32_t x = 0;
        for (int k = 0; k < 16; k++) {
            s += (int32_t)c[g * 16 + k];
            x ^= (uint32_t)c[g * 16 + k];
        }
        v[g] = s;
        atomicXor(xor_sink, x);
    }
    int32_t c4 = v[4];
    static const int L[8][3] = {
        {0,1,2},{3,4,5},{6,7,8},{0,3,6},
        {1,4,7},{2,5,8},{0,4,8},{2,4,6}
    };
    for (int l = 0; l < NLINES; l++)
        out[(size_t)i * NLINES + l] =
            (v[L[l][0]] - c4) + (v[L[l][1]] - c4) + (v[L[l][2]] - c4);
}

__global__ void tr_kernel(const uint8_t *in, uint8_t *out,
                          uint32_t *xor_sink, int towers) {
    int s = blockIdx.x * blockDim.x + threadIdx.x;
    int total = towers * TSLOT;
    if (s >= total) return;
    int t = s / TSLOT, sl = s % TSLOT;
    int p = (sl % COL) * ROW + sl / COL;
    const uint4 *src = (const uint4 *)(in + ((size_t)t * TSLOT + sl) * CELL);
    uint4 *dst = (uint4 *)(out + ((size_t)t * TSLOT + p) * CELL);
    uint32_t x = 0;
    for (int k = 0; k < 9; k++) {
        uint4 v = src[k];
        dst[k] = v;
        x ^= v.x ^ v.y ^ v.z ^ v.w;
    }
    atomicXor(xor_sink, x);
}

int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "rb");
    if (!f) return 1;
    fseek(f, 0, SEEK_END);
    long nbytes = ftell(f);
    fseek(f, 0, SEEK_SET);
    int towers = (int)(nbytes / (TSLOT * CELL));
    int n = towers * TSLOT;
    size_t outbytes = (size_t)n * NLINES * sizeof(int32_t);
    size_t trbytes = (size_t)nbytes;
    uint8_t *h_in = (uint8_t *)malloc((size_t)nbytes);
    int32_t *h_fp = (int32_t *)malloc(outbytes);
    uint8_t *h_tr = (uint8_t *)malloc(trbytes);
    if (!h_in || !h_fp || !h_tr) return 1;
    if (fread(h_in, 1, (size_t)nbytes, f) != (size_t)nbytes) return 1;
    fclose(f);
    uint8_t *d_in = 0, *d_tr = 0;
    int32_t *d_fp = 0;
    uint32_t *d_xor = 0;
    CUDA_CHECK(cudaMalloc(&d_in, (size_t)nbytes));
    CUDA_CHECK(cudaMalloc(&d_tr, trbytes));
    CUDA_CHECK(cudaMalloc(&d_fp, outbytes));
    CUDA_CHECK(cudaMalloc(&d_xor, sizeof(uint32_t)));
    CUDA_CHECK(cudaMemset(d_xor, 0, sizeof(uint32_t)));
    cudaEvent_t e0, e1, e2, e3, e4;
    CUDA_CHECK(cudaEventCreate(&e0));
    CUDA_CHECK(cudaEventCreate(&e1));
    CUDA_CHECK(cudaEventCreate(&e2));
    CUDA_CHECK(cudaEventCreate(&e3));
    CUDA_CHECK(cudaEventCreate(&e4));
    int blocks = (n + 255) / 256;
    EV(0, e0);
    CUDA_CHECK(cudaMemcpy(d_in, h_in, (size_t)nbytes, cudaMemcpyHostToDevice));
    EV(0, e1);
    fp_kernel<<<blocks, 256>>>(d_in, d_fp, d_xor, n);
    EV(0, e2);
    tr_kernel<<<blocks, 256>>>(d_in, d_tr, d_xor, towers);
    EV(0, e3);
    CUDA_CHECK(cudaMemcpy(h_fp, d_fp, outbytes, cudaMemcpyDeviceToHost));
    CUDA_CHECK(cudaMemcpy(h_tr, d_tr, trbytes, cudaMemcpyDeviceToHost));
    EV(0, e4);
    CUDA_CHECK(cudaEventSynchronize(e4));
    float h2d = 0, k1 = 0, k2 = 0, d2h = 0;
    CUDA_CHECK(cudaEventElapsedTime(&h2d, e0, e1));
    CUDA_CHECK(cudaEventElapsedTime(&k1, e1, e2));
    CUDA_CHECK(cudaEventElapsedTime(&k2, e2, e3));
    CUDA_CHECK(cudaEventElapsedTime(&d2h, e3, e4));
    FILE *o1 = fopen(argv[2], "wb");
    FILE *o2 = fopen(argv[3], "wb");
    if (!o1 || !o2) return 1;
    fwrite(h_fp, 1, outbytes, o1);
    fwrite(h_tr, 1, trbytes, o2);
    fclose(o1);
    fclose(o2);
    printf("CHAIN towers=%d H2D=%.2f K1fp=%.3f K2tr=%.3f D2H=%.2f resident=%.3f full=%.2f\n",
           towers, h2d, k1, k2, d2h, k1 + k2, h2d + k1 + k2 + d2h);
    return 0;
}
