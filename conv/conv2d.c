#include <stddef.h>

typedef float CONVFLOAT;
typedef int CONVINT;

/*
 * V2：固定输出分块 + 固定宽度 tail + 可选 kernel 两步展开。
 *
 * 语义：连续行主序 float 矩阵，valid 卷积（与官方相同，不翻转 kernel）。
 * input/kernel 只读；output 不能与它们重叠，每个输出被完整覆盖。
 * 正确性关键：每个输出仍按 jk 从小到大、ik 从小到大进行 float 累加。
 * SIMD lane 对应不同输出，不能对同一输出做 partial sum 后重新求和。
 * benchmark 与本文件都要使用 -ffp-contract=off，且不能使用 fast-math。
 *
 * 本轮只写代码：寄存器驻留、向量指令、速度和 PASS 均待目标机器验证。
 */
#ifdef __FAST_MATH__
#error "Strict reference accumulation requires disabling fast-math"
#endif

#ifndef CONV_BLOCK
#define CONV_BLOCK 32
#endif
#if CONV_BLOCK <= 0
#error "CONV_BLOCK must be positive"
#endif

/*
 * 2：每轮按顺序处理两个 kernel 元素，减少循环比较/跳转。
 * 1：保留单步 kernel 循环，供比赛机器单独比较展开的影响。
 * 两步展开会增加输入/乘积临时值，不保证所有 CPU 都更快。
 */
#ifndef CONV_KERNEL_UNROLL
#define CONV_KERNEL_UNROLL 2
#endif
#if CONV_KERNEL_UNROLL != 1 && CONV_KERNEL_UNROLL != 2
#error "CONV_KERNEL_UNROLL must be 1 or 2"
#endif

/* 内联可暴露固定数组大小；没有此属性仍正确，但优化效果可能不同。 */
#if defined(__GNUC__) || defined(__clang__)
#define CONV_INLINE static inline __attribute__((always_inline))
#else
#define CONV_INLINE static inline
#endif

/*
 * 宏生成固定宽度函数：N 是编译期常量，acc 的大小和 b 循环次数均已知。
 * 这样便于编译器展开 b 循环、把数组拆成独立寄存器，避免动态尾部长度。
 * N=32 在 NEON 上可映射为八个四路向量 accumulator；实际分配要看汇编。
 * acc 的生命期覆盖完整 kernel，output 只写一次，不需要先 memset。
 *
 * 宏中的 _Pragma("omp simd") 等价于普通代码的 #pragma omp simd。
 * b 之间完全独立，而 ik 之间保留依赖，所以这里不能加 reduction 子句。
 *
 * 两步展开的浮点顺序：
 *   acc = round(acc + round(input0 * k0));
 *   acc = round(acc + round(input1 * k1));
 * 不能改成 acc += input0*k0 + input1*k1，那样加法括号会变。
 * CONV_KERNEL_UNROLL 是常量，编译器可以消除对应的 if 分支。
 */
#define DEFINE_CONV_TILE(NAME, N)                                            \
CONV_INLINE void NAME(const float *restrict base, size_t stride,              \
                      const float *restrict kernel, int kh, int kw,          \
                      float *restrict dst)                                   \
{                                                                            \
    float acc[N] = {0.0f};                                                   \
    const float *krow = kernel;                                              \
    for (int jk = 0; jk < kh; ++jk) {                                        \
        const float *row = base + (size_t)jk * stride;                       \
        int ik = 0;                                                         \
        if (CONV_KERNEL_UNROLL == 2) {                                       \
            for (; ik < kw - 1; ik += 2) {                                   \
                const float k0 = krow[ik];                                  \
                const float k1 = krow[ik + 1];                              \
                const float *window = row + ik;                             \
                _Pragma("omp simd")                                        \
                for (int b = 0; b < (N); ++b) {                              \
                    acc[b] += window[b] * k0;                               \
                    acc[b] += window[b + 1] * k1;                           \
                }                                                           \
            }                                                               \
        }                                                                   \
        for (; ik < kw; ++ik) {                                              \
            const float k = krow[ik];                                       \
            const float *window = row + ik;                                 \
            _Pragma("omp simd")                                             \
            for (int b = 0; b < (N); ++b) {                                  \
                acc[b] += window[b] * k;                                    \
            }                                                               \
        }                                                                   \
        krow += kw;                                                         \
    }                                                                       \
    _Pragma("omp simd")                                                      \
    for (int b = 0; b < (N); ++b) {                                          \
        dst[b] = acc[b];                                                     \
    }                                                                       \
}

DEFINE_CONV_TILE(conv_tile_main, CONV_BLOCK)
DEFINE_CONV_TILE(conv_tile_16, 16)
DEFINE_CONV_TILE(conv_tile_8, 8)
DEFINE_CONV_TILE(conv_tile_4, 4)
DEFINE_CONV_TILE(conv_tile_2, 2)
DEFINE_CONV_TILE(conv_tile_1, 1)
#undef DEFINE_CONV_TILE
#undef CONV_INLINE

void conv2d(const CONVFLOAT *input, CONVINT inputHeight, CONVINT inputWidth,
            const CONVFLOAT *kernel, CONVINT kernelHeight, CONVINT kernelWidth,
            CONVFLOAT *output)
{
    /*
     * 在减法之前检查尺寸，避免负 kernel 或极端 int 参数引起有符号溢出。
     * 合法调用仍须提供有效指针和足够的存储空间，接口没有缓冲区长度参数。
     */
    if (kernelHeight <= 0 || kernelWidth <= 0 ||
        inputHeight < kernelHeight || inputWidth < kernelWidth) {
        return;
    }
    const int oh = inputHeight - kernelHeight + 1;
    const int ow = inputWidth - kernelWidth + 1;
    const size_t stride = (size_t)inputWidth;

    /*
     * 每行工作量相同，静态划分使线程写连续区域。acc 是线程局部变量；
     * 不需要原子操作、跨线程 reduction 或计时区内的 malloc。
     * 官方尺寸有足够多的输出行，暂不增加 collapse 和动态调度。
     */
#pragma omp parallel for schedule(static)
    for (int j = 0; j < oh; ++j) {
        const float *base = input + (size_t)j * stride;
        float *dst = output + (size_t)j * (size_t)ow;
        int i = 0;

        /*
         * ow-i >= B 等价于 i+B <= ow，但不会让 i+B 溢出。
         * 输入最后一列：i+N-1+kw-1 <= inputWidth-1。
         * 两步展开仅在 ik+1 < kw 时进入，因此 window[b+1] 也满足边界。
         * jk 最大为 kh-1，最后输入行 j+kh-1 <= inputHeight-1。
         */
        for (; ow - i >= CONV_BLOCK; i += CONV_BLOCK) {
            conv_tile_main(base + i, stride, kernel,
                           kernelHeight, kernelWidth, dst + i);
        }

        /*
         * 例：尾部 27 个输出拆成 16+8+2+1；12 个拆成 8+4。
         * 每块都拥有编译期固定的 acc 和 b 循环，没有运行时长度的累加循环。
         * 16 用 while，兼容大于 32 的自定义主块；后续各块最多执行一次。
         * 代价：多次遍历 kernel 和更大的机器代码，实际收益仍须实测。
         * 所有分支只看剩余宽度，不识别公开 testcase。
         */
        while (ow - i >= 16) {
            conv_tile_16(base + i, stride, kernel, kernelHeight, kernelWidth, dst + i);
            i += 16;
        }
        if (ow - i >= 8) {
            conv_tile_8(base + i, stride, kernel, kernelHeight, kernelWidth, dst + i);
            i += 8;
        }
        if (ow - i >= 4) {
            conv_tile_4(base + i, stride, kernel, kernelHeight, kernelWidth, dst + i);
            i += 4;
        }
        if (ow - i >= 2) {
            conv_tile_2(base + i, stride, kernel, kernelHeight, kernelWidth, dst + i);
            i += 2;
        }
        if (i < ow) {
            conv_tile_1(base + i, stride, kernel, kernelHeight, kernelWidth, dst + i);
        }
    }
}
