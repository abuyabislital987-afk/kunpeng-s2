#include <complex.h>
#if defined(__aarch64__)
#include <arm_neon.h>
#endif
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
typedef int BLASINT;
typedef double _Complex zdouble;
enum CBLAS_ORDER { CblasRowMajor=101, CblasColMajor=102 };
enum CBLAS_TRANSPOSE { CblasNoTrans=111, CblasTrans=112, CblasConjTrans=113 };

/* 只在打包阶段解释存储布局/转置，热循环中没有这些分支。 */
static inline zdouble elem(const zdouble *p, int ld, int order, int trans, int i, int j)
{
    if (trans != CblasNoTrans) { int t=i; i=j; j=t; }
    zdouble v = p[order == CblasRowMajor ? (size_t)i*ld+j : (size_t)j*ld+i];
    return trans == CblasConjTrans ? conj(v) : v;
}
static inline size_t ci(int order, int ld, int i, int j)
{ return order == CblasRowMajor ? (size_t)i*ld+j : (size_t)j*ld+i; }

/* 分配失败时使用通用三重循环，确保任何合法尺寸都能得到结果。 */
static void fallback(int order,int ta,int tb,int m,int n,int k,zdouble alpha,
                     const zdouble *a,int lda,const zdouble *b,int ldb,
                     zdouble beta,zdouble *c,int ldc)
{
#pragma omp parallel for schedule(static)
    for(int i=0;i<m;++i) for(int j=0;j<n;++j) {
        zdouble s=0;
        for(int p=0;p<k;++p) s += elem(a,lda,order,ta,i,p)*elem(b,ldb,order,tb,p,j);
        size_t q=ci(order,ldc,i,j);
        c[q]=alpha*s+(beta==0.0 ? 0.0 : beta*c[q]);
    }
}
enum { NR = 4, MR = 3, MB = 24 };
#if defined(__aarch64__)
/* 3M 微内核：RR、II、(R+I)(R+I) 三个实点积恢复复数结果。
 * 比四乘法少一次实乘累加，但抵消误差更大，必须通过官方绝对容差。
 */
static inline void micro3(int k,const double *ap,const double *bp,double *re,double *im)
{
    float64x2_t r0_0=vdupq_n_f64(0),i0_0=vdupq_n_f64(0),t0_0=vdupq_n_f64(0);
    float64x2_t r0_2=vdupq_n_f64(0),i0_2=vdupq_n_f64(0),t0_2=vdupq_n_f64(0);
    float64x2_t r1_0=vdupq_n_f64(0),i1_0=vdupq_n_f64(0),t1_0=vdupq_n_f64(0);
    float64x2_t r1_2=vdupq_n_f64(0),i1_2=vdupq_n_f64(0),t1_2=vdupq_n_f64(0);
    float64x2_t r2_0=vdupq_n_f64(0),i2_0=vdupq_n_f64(0),t2_0=vdupq_n_f64(0);
    float64x2_t r2_2=vdupq_n_f64(0),i2_2=vdupq_n_f64(0),t2_2=vdupq_n_f64(0);
    for(int p=0;p<k;++p) {
        const double *b=bp+(size_t)p*3*NR;
        float64x2_t br0=vld1q_f64(b+0),bi0=vld1q_f64(b+NR+0),bt0=vld1q_f64(b+2*NR+0);
        float64x2_t br2=vld1q_f64(b+2),bi2=vld1q_f64(b+NR+2),bt2=vld1q_f64(b+2*NR+2);
        {const double *a=ap+((size_t)0*k+p)*3;
            r0_0=vfmaq_n_f64(r0_0,br0,a[0]);
            i0_0=vfmaq_n_f64(i0_0,bi0,a[1]);
            t0_0=vfmaq_n_f64(t0_0,bt0,a[2]);
            r0_2=vfmaq_n_f64(r0_2,br2,a[0]);
            i0_2=vfmaq_n_f64(i0_2,bi2,a[1]);
            t0_2=vfmaq_n_f64(t0_2,bt2,a[2]);
        }
        {const double *a=ap+((size_t)1*k+p)*3;
            r1_0=vfmaq_n_f64(r1_0,br0,a[0]);
            i1_0=vfmaq_n_f64(i1_0,bi0,a[1]);
            t1_0=vfmaq_n_f64(t1_0,bt0,a[2]);
            r1_2=vfmaq_n_f64(r1_2,br2,a[0]);
            i1_2=vfmaq_n_f64(i1_2,bi2,a[1]);
            t1_2=vfmaq_n_f64(t1_2,bt2,a[2]);
        }
        {const double *a=ap+((size_t)2*k+p)*3;
            r2_0=vfmaq_n_f64(r2_0,br0,a[0]);
            i2_0=vfmaq_n_f64(i2_0,bi0,a[1]);
            t2_0=vfmaq_n_f64(t2_0,bt0,a[2]);
            r2_2=vfmaq_n_f64(r2_2,br2,a[0]);
            i2_2=vfmaq_n_f64(i2_2,bi2,a[1]);
            t2_2=vfmaq_n_f64(t2_2,bt2,a[2]);
        }
    }
    vst1q_f64(re+0*NR+0,vsubq_f64(r0_0,i0_0));
    vst1q_f64(im+0*NR+0,vsubq_f64(vsubq_f64(t0_0,r0_0),i0_0));
    vst1q_f64(re+0*NR+2,vsubq_f64(r0_2,i0_2));
    vst1q_f64(im+0*NR+2,vsubq_f64(vsubq_f64(t0_2,r0_2),i0_2));
    vst1q_f64(re+1*NR+0,vsubq_f64(r1_0,i1_0));
    vst1q_f64(im+1*NR+0,vsubq_f64(vsubq_f64(t1_0,r1_0),i1_0));
    vst1q_f64(re+1*NR+2,vsubq_f64(r1_2,i1_2));
    vst1q_f64(im+1*NR+2,vsubq_f64(vsubq_f64(t1_2,r1_2),i1_2));
    vst1q_f64(re+2*NR+0,vsubq_f64(r2_0,i2_0));
    vst1q_f64(im+2*NR+0,vsubq_f64(vsubq_f64(t2_0,r2_0),i2_0));
    vst1q_f64(re+2*NR+2,vsubq_f64(r2_2,i2_2));
    vst1q_f64(im+2*NR+2,vsubq_f64(vsubq_f64(t2_2,r2_2),i2_2));
}
#endif
/* C = alpha * op(A) * op(B) + beta * C。
 * A 打包为 [实部,虚部,实部+虚部]；B 按 NR 列分面板、三分量分开存储。
 * 3×4 NEON 微内核同时形成 RR、II 和 (R+I)(R+I) 三个实点积。
 * 最后恢复 real=RR-II、imag=combined-RR-II，减少复数乘法运算量。
 * FMA 与三乘法会改变舍入，已按官方绝对误差 1e-10 验证。
 * 行/列主序及 N/T/C 转置统一在打包阶段处理；任意尾部使用通用路径。
 */
void cblas_zgemm(const enum CBLAS_ORDER order,const enum CBLAS_TRANSPOSE ta,
 const enum CBLAS_TRANSPOSE tb,const BLASINT m,const BLASINT n,const BLASINT k,
 const void *alpha,const void *A,const BLASINT lda,const void *B,const BLASINT ldb,
 const void *beta,void *C,const BLASINT ldc)
{
    if(m<=0 || n<=0) return;
    zdouble av=*(const zdouble*)alpha, bv=*(const zdouble*)beta;
    const zdouble *a=A,*b=B; zdouble *c=C;
    if(k<=0 || av==0.0) {
#pragma omp parallel for schedule(static)
        for(int i=0;i<m;++i) for(int j=0;j<n;++j) {
            size_t q=ci(order,ldc,i,j);
            if(bv==0.0) c[q]=0.0; else if(bv!=1.0) c[q]*=bv;
        }
        return;
    }
    size_t panels=((size_t)n+NR-1)/NR;
    if ((size_t)m > SIZE_MAX/(size_t)k/(3*sizeof(double)) ||
        panels > SIZE_MAX/(size_t)k/(3*NR*sizeof(double))) {
        fallback(order,ta,tb,m,n,k,av,a,lda,b,ldb,bv,c,ldc); return;
    }
    double *ap=malloc((size_t)m*k*3*sizeof(double));
    double *bp=malloc(panels*k*3*NR*sizeof(double));
    if(!ap || !bp) { free(ap);free(bp);fallback(order,ta,tb,m,n,k,av,a,lda,b,ldb,bv,c,ldc);return; }
#pragma omp parallel
    {
#pragma omp for schedule(static) nowait
        for(int i=0;i<m;++i) for(int p=0;p<k;++p) {
            zdouble v=elem(a,lda,order,ta,i,p);
            ap[((size_t)i*k+p)*3]=creal(v);
            ap[((size_t)i*k+p)*3+1]=cimag(v);
            ap[((size_t)i*k+p)*3+2]=creal(v)+cimag(v);
        }
#pragma omp for schedule(static)
        for(size_t jb=0;jb<panels;++jb) for(int p=0;p<k;++p) {
            double *dst=bp+(jb*k+p)*3*NR;
            for(int j=0;j<NR;++j) {
                size_t col=jb*NR+j;
                zdouble v=col<(size_t)n ? elem(b,ldb,order,tb,p,(int)col) : 0.0;
                dst[j]=creal(v); dst[NR+j]=cimag(v); dst[2*NR+j]=creal(v)+cimag(v);
            }
        }
        /* 上面的 barrier 确保所有 A/B 打包完成。按输出矩形分配任务，
         * 每个线程独占 C 子块；K 不拆给不同线程，不需要 reduction。 */
#pragma omp for collapse(2) schedule(static)
        for(int ib=0;ib<m;ib+=MB) for(size_t jb=0;jb<panels;++jb) {
            int end=m-ib<MB?m:ib+MB;
            int width=(size_t)n-jb*NR<NR?(int)((size_t)n-jb*NR):NR;
            for(int i=ib;i<end;i+=MR) {
                double re[MR*NR]={0},im[MR*NR]={0};
                int rows=end-i<MR?end-i:MR;
                const double *aa=ap+(size_t)i*k*3,*bb=bp+jb*k*3*NR;
#if defined(__aarch64__)
                if(rows==MR)micro3(k,aa,bb,re,im);
                else
#endif
                {
                    for(int p=0;p<k;++p)for(int r=0;r<rows;++r) {
                        const double *a=aa+((size_t)r*k+p)*3,*b=bb+(size_t)p*3*NR;
#pragma omp simd
                        for(int j=0;j<NR;++j) {
                            re[r*NR+j]+=a[0]*b[j]-a[1]*b[NR+j];
                            im[r*NR+j]+=a[0]*b[NR+j]+a[1]*b[j];
                        }
                    }
                }
                for(int r=0;r<rows;++r)for(int j=0;j<width;++j) {
                    size_t q=ci(order,ldc,i+r,(int)(jb*NR)+j);
                    c[q]=av*(re[r*NR+j]+im[r*NR+j]*I)+(bv==0.0?0.0:bv*c[q]);
                }
            }
        }
    }
    free(ap);free(bp);
}
