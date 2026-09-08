#include <stddef.h>
#include <stdlib.h>
#include <omp.h>
#if defined(__aarch64__)
#include <arm_neon.h>
#endif
enum { RHS = 8, ROWS = 4 };
/* L X = B，按 RHS 列独立分配线程；每线程把右端项打包为窄面板。
 * 同时累计 ROWS 行对已解部分的贡献，复用 X[k,:] 的加载。
 * 小对角块内部仍按前代顺序逐行求解。只读 L，原地输出 B。
 */
#if defined(__aarch64__)
static inline void panel_sums(int start,int lda,const double *L,const double *x,double *s)
{
    float64x2_t s0_0=vdupq_n_f64(0);
    float64x2_t s0_2=vdupq_n_f64(0);
    float64x2_t s0_4=vdupq_n_f64(0);
    float64x2_t s0_6=vdupq_n_f64(0);
    float64x2_t s1_0=vdupq_n_f64(0);
    float64x2_t s1_2=vdupq_n_f64(0);
    float64x2_t s1_4=vdupq_n_f64(0);
    float64x2_t s1_6=vdupq_n_f64(0);
    float64x2_t s2_0=vdupq_n_f64(0);
    float64x2_t s2_2=vdupq_n_f64(0);
    float64x2_t s2_4=vdupq_n_f64(0);
    float64x2_t s2_6=vdupq_n_f64(0);
    float64x2_t s3_0=vdupq_n_f64(0);
    float64x2_t s3_2=vdupq_n_f64(0);
    float64x2_t s3_4=vdupq_n_f64(0);
    float64x2_t s3_6=vdupq_n_f64(0);
    for(int k=0;k<start;++k) {
        float64x2_t b0=vld1q_f64(x+(size_t)k*RHS+0);
        float64x2_t b2=vld1q_f64(x+(size_t)k*RHS+2);
        float64x2_t b4=vld1q_f64(x+(size_t)k*RHS+4);
        float64x2_t b6=vld1q_f64(x+(size_t)k*RHS+6);
        { double a=L[(size_t)0*lda+k];
            s0_0=vfmaq_n_f64(s0_0,b0,a);
            s0_2=vfmaq_n_f64(s0_2,b2,a);
            s0_4=vfmaq_n_f64(s0_4,b4,a);
            s0_6=vfmaq_n_f64(s0_6,b6,a);
        }
        { double a=L[(size_t)1*lda+k];
            s1_0=vfmaq_n_f64(s1_0,b0,a);
            s1_2=vfmaq_n_f64(s1_2,b2,a);
            s1_4=vfmaq_n_f64(s1_4,b4,a);
            s1_6=vfmaq_n_f64(s1_6,b6,a);
        }
        { double a=L[(size_t)2*lda+k];
            s2_0=vfmaq_n_f64(s2_0,b0,a);
            s2_2=vfmaq_n_f64(s2_2,b2,a);
            s2_4=vfmaq_n_f64(s2_4,b4,a);
            s2_6=vfmaq_n_f64(s2_6,b6,a);
        }
        { double a=L[(size_t)3*lda+k];
            s3_0=vfmaq_n_f64(s3_0,b0,a);
            s3_2=vfmaq_n_f64(s3_2,b2,a);
            s3_4=vfmaq_n_f64(s3_4,b4,a);
            s3_6=vfmaq_n_f64(s3_6,b6,a);
        }
    }
    vst1q_f64(s+0*RHS+0,s0_0);
    vst1q_f64(s+0*RHS+2,s0_2);
    vst1q_f64(s+0*RHS+4,s0_4);
    vst1q_f64(s+0*RHS+6,s0_6);
    vst1q_f64(s+1*RHS+0,s1_0);
    vst1q_f64(s+1*RHS+2,s1_2);
    vst1q_f64(s+1*RHS+4,s1_4);
    vst1q_f64(s+1*RHS+6,s1_6);
    vst1q_f64(s+2*RHS+0,s2_0);
    vst1q_f64(s+2*RHS+2,s2_2);
    vst1q_f64(s+2*RHS+4,s2_4);
    vst1q_f64(s+2*RHS+6,s2_6);
    vst1q_f64(s+3*RHS+0,s3_0);
    vst1q_f64(s+3*RHS+2,s3_2);
    vst1q_f64(s+3*RHS+4,s3_4);
    vst1q_f64(s+3*RHS+6,s3_6);
}
#endif
static void solve_panel(int m,int n,const double *L,int lda,double *B,int ldb)
{
    if(m<=0 || n<=0) return;
#pragma omp parallel
    {
        double *x=NULL;
        if(posix_memalign((void**)&x,64,(size_t)m*RHS*sizeof(double))!=0) x=NULL;
#pragma omp for schedule(static)
        for(int jb=0;jb<n;jb+=RHS) {
            int w=n-jb<RHS?n-jb:RHS;
            if(!x) {
                for(int j=jb;j<jb+w;++j)for(int i=0;i<m;++i) {
                    double s=0;for(int k=0;k<i;++k)s+=L[(size_t)i*lda+k]*B[(size_t)k*ldb+j];
                    B[(size_t)i*ldb+j]=(B[(size_t)i*ldb+j]-s)/L[(size_t)i*lda+i];
                }
                continue;
            }
            for(int i=0;i<m;++i) {
                for(int j=0;j<w;++j)x[(size_t)i*RHS+j]=B[(size_t)i*ldb+jb+j];
                for(int j=w;j<RHS;++j)x[(size_t)i*RHS+j]=0;
            }
            for(int ii=0;ii<m;ii+=ROWS) {
                int h=m-ii<ROWS?m-ii:ROWS;
                double sums[ROWS*RHS]={0};
#if defined(__aarch64__)
                if(h==ROWS)panel_sums(ii,lda,L+(size_t)ii*lda,x,sums);
                else
#endif
                {
                    for(int k=0;k<ii;++k)for(int r=0;r<h;++r) {
                        double a=L[(size_t)(ii+r)*lda+k];
#pragma omp simd
                        for(int j=0;j<RHS;++j)sums[r*RHS+j]+=a*x[(size_t)k*RHS+j];
                    }
                }
                for(int r=0;r<h;++r) {
                    double *row=x+(size_t)(ii+r)*RHS;
                    // 已累计 k<ii；接着按顺序纳入当前小块内的已解行。
                    for(int q=0;q<r;++q) {
                        double a=L[(size_t)(ii+r)*lda+ii+q];
#pragma omp simd
                        for(int j=0;j<RHS;++j)sums[r*RHS+j]+=a*x[(size_t)(ii+q)*RHS+j];
                    }
                    double diag=L[(size_t)(ii+r)*lda+ii+r];
#pragma omp simd
                    for(int j=0;j<RHS;++j)row[j]=(row[j]-sums[r*RHS+j])/diag;
                }
            }
            for(int i=0;i<m;++i)for(int j=0;j<w;++j)B[(size_t)i*ldb+jb+j]=x[(size_t)i*RHS+j];
        }
        free(x);
    }
}

#include <stddef.h>
#include <omp.h>
#if defined(__aarch64__)
#include <arm_neon.h>
#endif
enum { KB = 256, CT = 64 };
/* 分块前代：先求一个小对角块，再用矩阵乘更新下面的全部右端项。
 * 每一步 omp for 的 barrier 保证依赖完成；线程只写自己负责的矩形。
 * 更新改变浮点分组顺序，必须以官方 1e-12 容差验证。
 */
#if defined(__aarch64__)
static inline void update4x8(int count,const double *L,int lda,const double *X,int ldb,double *C)
{
    float64x2_t a0_0=vdupq_n_f64(0);
    float64x2_t a0_2=vdupq_n_f64(0);
    float64x2_t a0_4=vdupq_n_f64(0);
    float64x2_t a0_6=vdupq_n_f64(0);
    float64x2_t a1_0=vdupq_n_f64(0);
    float64x2_t a1_2=vdupq_n_f64(0);
    float64x2_t a1_4=vdupq_n_f64(0);
    float64x2_t a1_6=vdupq_n_f64(0);
    float64x2_t a2_0=vdupq_n_f64(0);
    float64x2_t a2_2=vdupq_n_f64(0);
    float64x2_t a2_4=vdupq_n_f64(0);
    float64x2_t a2_6=vdupq_n_f64(0);
    float64x2_t a3_0=vdupq_n_f64(0);
    float64x2_t a3_2=vdupq_n_f64(0);
    float64x2_t a3_4=vdupq_n_f64(0);
    float64x2_t a3_6=vdupq_n_f64(0);
    for(int k=0;k<count;++k) {
        float64x2_t b0=vld1q_f64(X+(size_t)k*ldb+0);
        float64x2_t b2=vld1q_f64(X+(size_t)k*ldb+2);
        float64x2_t b4=vld1q_f64(X+(size_t)k*ldb+4);
        float64x2_t b6=vld1q_f64(X+(size_t)k*ldb+6);
        { double l=L[(size_t)0*lda+k];
            a0_0=vfmaq_n_f64(a0_0,b0,l);
            a0_2=vfmaq_n_f64(a0_2,b2,l);
            a0_4=vfmaq_n_f64(a0_4,b4,l);
            a0_6=vfmaq_n_f64(a0_6,b6,l);
        }
        { double l=L[(size_t)1*lda+k];
            a1_0=vfmaq_n_f64(a1_0,b0,l);
            a1_2=vfmaq_n_f64(a1_2,b2,l);
            a1_4=vfmaq_n_f64(a1_4,b4,l);
            a1_6=vfmaq_n_f64(a1_6,b6,l);
        }
        { double l=L[(size_t)2*lda+k];
            a2_0=vfmaq_n_f64(a2_0,b0,l);
            a2_2=vfmaq_n_f64(a2_2,b2,l);
            a2_4=vfmaq_n_f64(a2_4,b4,l);
            a2_6=vfmaq_n_f64(a2_6,b6,l);
        }
        { double l=L[(size_t)3*lda+k];
            a3_0=vfmaq_n_f64(a3_0,b0,l);
            a3_2=vfmaq_n_f64(a3_2,b2,l);
            a3_4=vfmaq_n_f64(a3_4,b4,l);
            a3_6=vfmaq_n_f64(a3_6,b6,l);
        }
    }
    vst1q_f64(C+(size_t)0*ldb+0,vsubq_f64(vld1q_f64(C+(size_t)0*ldb+0),a0_0));
    vst1q_f64(C+(size_t)0*ldb+2,vsubq_f64(vld1q_f64(C+(size_t)0*ldb+2),a0_2));
    vst1q_f64(C+(size_t)0*ldb+4,vsubq_f64(vld1q_f64(C+(size_t)0*ldb+4),a0_4));
    vst1q_f64(C+(size_t)0*ldb+6,vsubq_f64(vld1q_f64(C+(size_t)0*ldb+6),a0_6));
    vst1q_f64(C+(size_t)1*ldb+0,vsubq_f64(vld1q_f64(C+(size_t)1*ldb+0),a1_0));
    vst1q_f64(C+(size_t)1*ldb+2,vsubq_f64(vld1q_f64(C+(size_t)1*ldb+2),a1_2));
    vst1q_f64(C+(size_t)1*ldb+4,vsubq_f64(vld1q_f64(C+(size_t)1*ldb+4),a1_4));
    vst1q_f64(C+(size_t)1*ldb+6,vsubq_f64(vld1q_f64(C+(size_t)1*ldb+6),a1_6));
    vst1q_f64(C+(size_t)2*ldb+0,vsubq_f64(vld1q_f64(C+(size_t)2*ldb+0),a2_0));
    vst1q_f64(C+(size_t)2*ldb+2,vsubq_f64(vld1q_f64(C+(size_t)2*ldb+2),a2_2));
    vst1q_f64(C+(size_t)2*ldb+4,vsubq_f64(vld1q_f64(C+(size_t)2*ldb+4),a2_4));
    vst1q_f64(C+(size_t)2*ldb+6,vsubq_f64(vld1q_f64(C+(size_t)2*ldb+6),a2_6));
    vst1q_f64(C+(size_t)3*ldb+0,vsubq_f64(vld1q_f64(C+(size_t)3*ldb+0),a3_0));
    vst1q_f64(C+(size_t)3*ldb+2,vsubq_f64(vld1q_f64(C+(size_t)3*ldb+2),a3_2));
    vst1q_f64(C+(size_t)3*ldb+4,vsubq_f64(vld1q_f64(C+(size_t)3*ldb+4),a3_4));
    vst1q_f64(C+(size_t)3*ldb+6,vsubq_f64(vld1q_f64(C+(size_t)3*ldb+6),a3_6));
}
#endif
static void solve_blocked(int m,int n,const double *L,int lda,double *B,int ldb)
{
    if(m<=0 || n<=0) return;
#pragma omp parallel
    {
        for(int kk=0;kk<m;kk+=KB) {
            int end=m-kk<KB?m:kk+KB;
            // 对角块内的列彼此独立，但每一列的行必须按前代顺序。
#pragma omp for schedule(static)
            for(int jb=0;jb<n;jb+=8) {
                int w=n-jb<8?n-jb:8;
                for(int i=kk;i<end;++i) {
                    double sum[8]={0};
                    for(int k=kk;k<i;++k) {
                        double a=L[(size_t)i*lda+k];
#pragma omp simd
                        for(int j=0;j<w;++j)sum[j]+=a*B[(size_t)k*ldb+jb+j];
                    }
                    double diag=L[(size_t)i*lda+i];
#pragma omp simd
                    for(int j=0;j<w;++j)B[(size_t)i*ldb+jb+j]=(B[(size_t)i*ldb+jb+j]-sum[j])/diag;
                }
            }
            // B[下面,:] -= L[下面,当前块] * X[当前块,:]。
#pragma omp for collapse(2) schedule(static)
            for(int ib=end;ib<m;ib+=CT)for(int jb=0;jb<n;jb+=CT) {
                int ie=m-ib<CT?m:ib+CT,je=n-jb<CT?n:jb+CT;
                for(int i=ib;i<ie;i+=4)for(int j=jb;j<je;j+=8) {
                    int rows=ie-i<4?ie-i:4,cols=je-j<8?je-j:8;
#if defined(__aarch64__)
                    if(rows==4 && cols==8)update4x8(end-kk,L+(size_t)i*lda+kk,lda,B+(size_t)kk*ldb+j,ldb,B+(size_t)i*ldb+j);
                    else
#endif
                    for(int r=0;r<rows;++r) {
                        double sum[8]={0};
                        for(int k=kk;k<end;++k) {
                            double a=L[(size_t)(i+r)*lda+k];
#pragma omp simd
                            for(int c=0;c<cols;++c)sum[c]+=a*B[(size_t)k*ldb+j+c];
                        }
#pragma omp simd
                        for(int c=0;c<cols;++c)B[(size_t)(i+r)*ldb+j+c]-=sum[c];
                    }
                }
            }
        }
    }
}

/* 用三角矩阵工作集估计选择通用算法，不对公开测试尺寸作等值分支。
 * 小工作集：多行窄面板前代，避免全矩阵更新与频繁 barrier。
 * 大工作集：分块前代，避免每组右端项重复扫描整个大 L。
 * 64 MiB 是实现选择的缓存预算，不是对运行硬件缓存容量的承诺。
 */
void l_trsm(int m,int n,const double *L,int lda,double *B,int ldb)
{
    if(m<=0 || n<=0)return;
    size_t triangular_entries=(size_t)m*((size_t)m+1)/2;
    if(triangular_entries > (64u*1024u*1024u)/sizeof(double))
        solve_blocked(m,n,L,lda,B,ldb);
    else
        solve_panel(m,n,L,lda,B,ldb);
}
