/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef CUPID_GPU_LOAD_SAMPLE_H
#define CUPID_GPU_LOAD_SAMPLE_H

#ifdef __KERNEL__
#include <linux/types.h>
#include <linux/math64.h>
typedef u64 cupid_u64;
#else
#include <stdint.h>
typedef uint64_t cupid_u64;
#endif

struct cupid_gpu_load {
	cupid_u64 busy;
	cupid_u64 total;
	cupid_u64 observed_ns;
	cupid_u64 generation;
	int valid;
};

static inline int cupid_gpu_load_percent(const struct cupid_gpu_load *sample,
					cupid_u64 now_ns, cupid_u64 max_age_ns)
{
	if (!sample->valid || !sample->total || sample->busy > sample->total ||
	    now_ns < sample->observed_ns || now_ns - sample->observed_ns > max_age_ns)
		return -1;
#ifdef __KERNEL__
	return (int)mul_u64_u64_div_u64(sample->busy, 100, sample->total);
#else
	return (int)(((__uint128_t)sample->busy * 100) / sample->total);
#endif
}
#endif
