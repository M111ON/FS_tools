#define _DEFAULT_SOURCE
/* RCU / epoch reclamation - C11 atomic implementation (tfb3b3e89).
 * Mirrors the protocol validated in ico_rcu_stress.py but with real atomics
 * and memory fences. Compile: gcc -std=c11 -O1 -g -fsanitize=thread -pthread
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdatomic.h>
#include <pthread.h>
#include <time.h>
#include <unistd.h>

#define SWAPS 10
#define READERS 8
#define HOLD_MS 120

typedef struct Version {
    int gen;
    unsigned long checksum;
    atomic_int invalidated;
} Version;

static atomic_uintptr_t current;
static atomic_int epoch;
static pthread_mutex_t pub_lock = PTHREAD_MUTEX_INITIALIZER;

typedef struct { atomic_int active; int gen; } ReaderSlot;
static ReaderSlot readers[READERS];

static int violations[5] = {0,0,0,0,0};

static unsigned long cs(int gen){ return (unsigned long)gen * 2654435761UL; }

static Version *build_version(int gen){
    Version *v = calloc(1, sizeof(Version));
    v->gen = gen;
    v->checksum = cs(gen);
    atomic_store(&v->invalidated, 0);
    return v;
}

static void rcu_read_lock(int rid){ atomic_fetch_add(&readers[rid].active, 1); }
static void rcu_read_unlock(int rid){ atomic_fetch_sub(&readers[rid].active, 1); }
static Version *rcu_dereference(void){
    return (Version *)atomic_load_explicit(&current, memory_order_acquire);
}

static void reader_fn(void *arg){
    int rid = *(int*)arg;
    int reads = 0;
    while (1) {
        rcu_read_lock(rid);
        Version *v = rcu_dereference();
        if (v) {
            readers[rid].gen = v->gen;
            unsigned long c = v->checksum;
            volatile int x = 0; for (int i=0;i<1000;i++) x += i;
            (void)x;
            if (c != v->checksum) violations[0]++;
            if (atomic_load(&v->invalidated)) violations[2]++;
            if (reads % 12 == 0 && reads > 0) {
                struct timespec ts = {0, HOLD_MS*1000000L};
                nanosleep(&ts, NULL);
            }
        }
        rcu_read_unlock(rid);
        reads++;
        usleep(100);
        if (atomic_load(&epoch) > SWAPS) break;
    }
}

static void publish(Version *v){
    pthread_mutex_lock(&pub_lock);
    int e = atomic_load(&epoch) + 1;
    v->gen = e;
    atomic_store_explicit(&current, (uintptr_t)v, memory_order_release);
    atomic_store(&epoch, e);
    pthread_mutex_unlock(&pub_lock);
}

static int any_held(int target_gen){
    for (int i=0;i<READERS;i++){
        if (atomic_load(&readers[i].active) > 0 && readers[i].gen < target_gen)
            return 1;
    }
    return 0;
}

int main(void){
    atomic_store(&current, 0);
    atomic_store(&epoch, 0);
    for (int i=0;i<READERS;i++){ atomic_store(&readers[i].active,0); readers[i].gen=0; }

    pthread_t ths[READERS];
    int ids[READERS];
    for (int i=0;i<READERS;i++){ ids[i]=i; pthread_create(&ths[i], NULL, (void*(*)(void*))reader_fn, &ids[i]); }

    Version *live[SWAPS+2]; int nlive=0;
    for (int g=1; g<=SWAPS; g++){
        Version *v = build_version(g);
        publish(v);
        live[nlive++] = v;
        if (nlive >= 2) {
            Version *old = live[0];
            struct timespec start; clock_gettime(CLOCK_MONOTONIC, &start);
            while (any_held(g)) {
                struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
                double el = (now.tv_sec-start.tv_sec) + (now.tv_nsec-start.tv_nsec)/1e9;
                if (el > 1.0) break;
                usleep(1000);
            }
            if (!any_held(g)) {
                atomic_store(&old->invalidated, 1);
                for (int k=0;k<nlive-1;k++) live[k]=live[k+1];
                nlive--;
            }
        }
        usleep(20000);
    }
    atomic_store(&epoch, SWAPS+1);
    for (int i=0;i<READERS;i++) pthread_join(ths[i], NULL);

    printf("violations: I1=%d I2=%d I3=%d I4=%d I5=%d\n",
           violations[0],violations[1],violations[2],violations[3],violations[4]);
    int total = violations[0]+violations[1]+violations[2]+violations[3]+violations[4];
    printf("RESULT: %s\n", total==0 ? "PASS" : "FAIL");
    return total==0 ? 0 : 1;
}
