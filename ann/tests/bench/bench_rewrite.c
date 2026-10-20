/* bench_rewrite.c — microbench: 62k-line jsonl rewrite via fprintf%%.6g (the dominant regrow rewrite cost; measured ~17.6s at N=62078 on G4400). Build: gcc -O2 -o r.exe bench_rewrite.c */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#define N 62078
#define D 384
int main(void){ float*v=malloc(D*sizeof(float)); for(int i=0;i<D;i++)v[i]=0.001f*i;
 clock_t t0=clock(); FILE*f=fopen("NUL:","w"); if(!f){f=fopen("C:/Users/Administrator.AVENTADOR/AppData/Local/Temp/ann-impl/null.txt","w");}
 for(int i=0;i<N;i++){ fprintf(f,"{\"sid\":\"%064x\",\"file\":\"embedded_document/x\",\"npast\":%d,\"anchor\":%d,\"anchor2\":%d,\"dim\":%d,\"vec\":[",i,i,1,2,D);
   for(int j=0;j<D;j++) fprintf(f,"%s%.6g",j?",":"",(double)v[j]); fprintf(f,"]}\n");}
 fclose(f); double ms=(double)(clock()-t0)*1000.0/CLOCKS_PER_SEC;
 printf("62k-line jsonl rewrite via fprintf%%.6g: %.0f ms\n", ms); free(v); return 0; }
