/* 测试专用：兼容参考库的标准 CBLAS ABI 声明。
 * 官方 benchmark 原样编译；仅在显式设置 KBLAS_LIB 时使用。
 */
#ifndef CONTEST_KPLBLAS_COMPAT_H
#define CONTEST_KPLBLAS_COMPAT_H
enum CBLAS_ORDER { CblasRowMajor=101, CblasColMajor=102 };
enum CBLAS_TRANSPOSE { CblasNoTrans=111, CblasTrans=112, CblasConjTrans=113 };
void cblas_dgemm(enum CBLAS_ORDER,enum CBLAS_TRANSPOSE,enum CBLAS_TRANSPOSE,
 int,int,int,double,const double*,int,const double*,int,double,double*,int);
void cblas_domatcopy(enum CBLAS_ORDER,enum CBLAS_TRANSPOSE,int,int,double,const double*,int,double*,int);
#endif
