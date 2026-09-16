/* ═══════════════════════════════════════════════════════════════════
 * gpu_fp_vec8 — fp_vec8 batch kernel (tower cells -> 8-vector fingerprints)
 * fp_vec8 batch kernel — คำนวณ fingerprint 8 ค่าทีละ chunk บน GPU
 *
 * 1 thread = 1 chunk (144B -> 9x16B sums, center v4, 8 line sums).
 * Matches tower.py fp_vec8 + chat_tower.h ct_fp_vec8 byte-for-byte.
 * XOR sink forces real HBM reads (NVCC eliminates pure pulls — pitfall 16).
 * ═══════════════════════════════════════════════════════════════════ */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* ── CUDA error-checking macro ──────────────────────────────────
 * ตรวจสอบรหัสผิดพลาดจาก CUDA API ทุกครั้ง — แสดงบรรทัด + ชื่อ error
 * Check every CUDA API return — prints line + error name on failure
 */
#define CUDA_CHECK(call) do {                                          \
    cudaError_t _err_ = (call);                                        \
    if (_err_ != cudaSuccess) {                                        \
        fprintf(stderr, "CUDA ERROR [%s:%d] %s: %s\n",                 \
                __FILE__, __LINE__, #call, cudaGetErrorString(_err_)); \
        return -1;                                                     \
    }                                                                  \
} while (0)

#define CHUNK 144
#define NLINES 8

/* ── Kernel: one thread per 144B chunk ── */
__global__ void fp_vec8_kernel(const uint8_t *data, int32_t *out,
                               uint32_t *xor_sink, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= n) return;
    const uint8_t *c = data + (size_t)i * CHUNK;
    int32_t v[9];
    for (int g = 0; g < 9; g++) {
        int32_t s = 0;
        uint32_t x = 0;
        for (int k = 0; k < 16; k++) {
            s += (int32_t)c[g * 16 + k];
            x ^= (uint32_t)c[g * 16 + k];
        }
        v[g] = s;
        atomicXor(xor_sink, x);  /* force real reads — ห้าม NVCC ตัดทิ้ง */
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

/* ── Host: persistent buffers allocated once, reused per dispatch ── */
int main(int argc, char **argv) {
    const char *bin = argv[1];
    const char *outpath = argv[2];
    FILE *f = fopen(bin, "rb");
    if (!f) { fprintf(stderr, "open %s\n", bin); return 1; }
    fseek(f, 0, SEEK_END);
    long nbytes = ftell(f);
    fseek(f, 0, SEEK_SET);
    int n = (int)(nbytes / CHUNK);
    uint8_t *h_data = (uint8_t *)malloc((size_t)nbytes);
    if (!h_data) return 1;
    if (fread(h_data, 1, (size_t)nbytes, f) != (size_t)nbytes) return 1;
    fclose(f);

    uint8_t *d_data = 0;
    int32_t *d_out = 0;
    uint32_t *d_xor = 0;
    size_t outbytes = (size_t)n * NLINES * sizeof(int32_t);
    int32_t *h_out = (int32_t *)malloc(outbytes);
    uint32_t h_xor = 0;
    if (!h_out) return 1;
    CUDA_CHECK(cudaMalloc(&d_data, (size_t)nbytes));
    CUDA_CHECK(cudaMalloc(&d_out, outbytes));
    CUDA_CHECK(cudaMalloc(&d_xor, sizeof(uint32_t)));
    CUDA_CHECK(cudaMemcpy(d_data, h_data, (size_t)nbytes,
                          cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemset(d_xor, 0, sizeof(uint32_t)));

    /* warmup then timed dispatch — วัดรอบจับเวลารอบเดียว */
    int blocks = (n + 255) / 256;
    fp_vec8_kernel<<<blocks, 256>>>(d_data, d_out, d_xor, n);
    CUDA_CHECK(cudaDeviceSynchronize());
    cudaEvent_t t0, t1;
    CUDA_CHECK(cudaEventCreate(&t0));
    CUDA_CHECK(cudaEventCreate(&t1));
    CUDA_CHECK(cudaEventRecord(t0));
    fp_vec8_kernel<<<blocks, 256>>>(d_data, d_out, d_xor, n);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    float ms = 0;
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));

    CUDA_CHECK(cudaMemcpy(h_out, d_out, outbytes, cudaMemcpyDeviceToHost));
    CUDA_CHECK(cudaMemcpy(&h_xor, d_xor, sizeof(uint32_t),
                          cudaMemcpyDeviceToHost));
    FILE *o = fopen(outpath, "wb");
    if (!o) return 1;
    fwrite(h_out, 1, outbytes, o);
    fclose(o);
    printf("GPU-FP: chunks=%d ms=%.3f xor_sink=0x%08x\n", n, ms,
           (unsigned)h_xor);
    cudaFree(d_data);
    cudaFree(d_out);
    cudaFree(d_xor);
    free(h_data);
    free(h_out);
    return 0;
}
