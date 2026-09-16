/* ═══════════════════════════════════════════════════════════════════
 * gpu_transpose — B-twinview reorder kernel (A 32x36 view -> B 36x32 view)
 * reorder kernel มุมมอง A (32x36) -> มุมมอง B (36x32) สำหรับ twinview
 *
 * 1 thread = 1 slot (144B cell). Slot s=(r,c) [r*36+c] -> position p=c*32+r.
 * uint4 vectorized moves (144B = 9x uint4). Serves B-view from A-stored data.
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

/* ── Kernel: reorder one tower (1152 slots) per block-range ── */
__global__ void transpose_kernel(const uint8_t *in, uint8_t *out,
                                 uint32_t *xor_sink, int towers) {
    int s = blockIdx.x * blockDim.x + threadIdx.x;
    int total = towers * TSLOT;
    if (s >= total) return;
    int t = s / TSLOT, sl = s % TSLOT;
    int r = sl / COL, c = sl % COL;
    int p = c * ROW + r;  /* B-view position — ตำแหน่งมุมมอง B */
    const uint4 *src = (const uint4 *)(in + ((size_t)t * TSLOT + sl) * CELL);
    uint4 *dst = (uint4 *)(out + ((size_t)t * TSLOT + p) * CELL);
    uint32_t x = 0;
    for (int k = 0; k < 9; k++) {
        uint4 v = src[k];
        dst[k] = v;
        x ^= v.x ^ v.y ^ v.z ^ v.w;  /* force real reads — กัน NVCC ตัด */
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
    size_t total = (size_t)towers * TSLOT * CELL;
    uint8_t *h_in = (uint8_t *)malloc(total);
    uint8_t *h_out = (uint8_t *)malloc(total);
    uint32_t h_xor = 0;
    if (!h_in || !h_out) return 1;
    if (fread(h_in, 1, total, f) != total) return 1;
    fclose(f);
    uint8_t *d_in = 0, *d_out = 0;
    uint32_t *d_xor = 0;
    CUDA_CHECK(cudaMalloc(&d_in, total));
    CUDA_CHECK(cudaMalloc(&d_out, total));
    CUDA_CHECK(cudaMalloc(&d_xor, sizeof(uint32_t)));
    CUDA_CHECK(cudaMemcpy(d_in, h_in, total, cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemset(d_xor, 0, sizeof(uint32_t)));
    int slots = towers * TSLOT;
    int blocks = (slots + 255) / 256;
    transpose_kernel<<<blocks, 256>>>(d_in, d_out, d_xor, towers);
    CUDA_CHECK(cudaDeviceSynchronize());
    cudaEvent_t t0, t1;
    CUDA_CHECK(cudaEventCreate(&t0));
    CUDA_CHECK(cudaEventCreate(&t1));
    CUDA_CHECK(cudaEventRecord(t0));
    transpose_kernel<<<blocks, 256>>>(d_in, d_out, d_xor, towers);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    float ms = 0;
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    CUDA_CHECK(cudaMemcpy(h_out, d_out, total, cudaMemcpyDeviceToHost));
    CUDA_CHECK(cudaMemcpy(&h_xor, d_xor, sizeof(uint32_t),
                          cudaMemcpyDeviceToHost));
    FILE *o = fopen(argv[2], "wb");
    if (!o) return 1;
    fwrite(h_out, 1, total, o);
    fclose(o);
    printf("GPU-TR: towers=%d ms=%.3f xor=0x%08x\n", towers, ms,
           (unsigned)h_xor);
    cudaFree(d_in);
    cudaFree(d_out);
    cudaFree(d_xor);
    free(h_in);
    free(h_out);
    return 0;
}
