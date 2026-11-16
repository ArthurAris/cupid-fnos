/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef CUPID_COMPUTE_ORACLE_H
#define CUPID_COMPUTE_ORACLE_H
#include <stdint.h>
#define ELEMENTS 255U
#define STORAGE_WORDS 320U
#define SENTINEL 0x9b7d2e51U
static uint32_t input_a(uint32_t i) { return i * 0xf1234567U + 0xfffffffdU; }
static uint32_t input_b(uint32_t i) { return (i * 0x76543219U) ^ 0xaaaaaaaaU; }
static int check_result(const uint32_t *values, uint32_t count)
{
	if (count != STORAGE_WORDS)
		return -1;
	for (uint32_t i = 0; i < count; i++) {
		uint32_t expected = i < ELEMENTS ? input_a(i) + input_b(i) : SENTINEL;
		if (values[i] != expected)
			return (int)i + 1;
	}
	return 0;
}
#endif
