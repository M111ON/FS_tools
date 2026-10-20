/* bench_kmeans.c — microbench: kmeans assign cost 25 iters x N x K (anchor regrow overhead; measured ~10.4s at N=62078 K=8 on G4400). Build: gcc -O2 -o k.exe bench_kmeans.c -lm */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#define N 62078
#define D 384
#define K 8
static double g_sink=0;
static double dist(const float*q,const float*c){double d=0;for(int j=0;j<D;j++){double e=(double)q[j]-c[j];d+=e*e;}return d;}
int main(void){float*X=malloc((size_t)N*D*4);for(size_t i=0;i<(size_t)N*D;i++)X[i]=(float)((i%97)*0.001);
float C[K*D];for(int k=0;k<K;k++)for(int j=0;j<D;j++)C[k*D+j]=X[(size_t)(k*N/K)*D+j];
int lab=0; clock_t t0=clock();
for(int it=0;it<25;it++){for(int i=0;i<N;i++){double bd=1e30;lab=0;for(int k=0;k<K;k++){double d=dist(X+(size_t)i*D,C+k*D);if(d<bd){bd=d;lab=k;}}g_sink+=bd;}}
double ms=(double)(clock()-t0)*1000/CLOCKS_PER_SEC;
printf("kmeans-assign 25 iters x %d rows: %.0f ms (sink %g)\n", N, ms, g_sink);
return 0;}
