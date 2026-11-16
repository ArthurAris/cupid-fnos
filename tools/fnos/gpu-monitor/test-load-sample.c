/* SPDX-License-Identifier: GPL-2.0-only */
#include <assert.h>
#include <stdint.h>
#include "load-sample.h"

int main(void)
{
	struct cupid_gpu_load value = { .busy = 25, .total = 100,
		.observed_ns = 1000, .generation = 1, .valid = 1 };
	assert(cupid_gpu_load_percent(&value, 1200, 500) == 25);
	assert(cupid_gpu_load_percent(&value, 1500, 500) == 25);
	assert(cupid_gpu_load_percent(&value, 1501, 500) == -1);
	assert(cupid_gpu_load_percent(&value, 999, 500) == -1);
	value.valid = 0;
	assert(cupid_gpu_load_percent(&value, 1200, 500) == -1);
	value.valid = 1;
	value.total = 0;
	assert(cupid_gpu_load_percent(&value, 1200, 500) == -1);
	value.total = 100;
	value.busy = 101;
	assert(cupid_gpu_load_percent(&value, 1200, 500) == -1);
	value.busy = 0;
	assert(cupid_gpu_load_percent(&value, 1200, 500) == 0);
	value.busy = value.total = UINT64_MAX;
	assert(cupid_gpu_load_percent(&value, 1200, 500) == 100);
	value.busy = UINT64_MAX / 2;
	assert(cupid_gpu_load_percent(&value, 1200, 500) == 49);
	value.busy = UINT64_MAX - 1;
	assert(cupid_gpu_load_percent(&value, 1200, 500) == 99);
	return 0;
}
