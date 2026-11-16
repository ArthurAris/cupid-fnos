/* SPDX-License-Identifier: GPL-2.0-only */
#include <assert.h>
#include "oracle.h"
int main(void)
{
	uint32_t values[STORAGE_WORDS];
	for (uint32_t i=0;i<STORAGE_WORDS;i++) values[i]=i<ELEMENTS ? input_a(i)+input_b(i) : SENTINEL;
	assert(check_result(values,STORAGE_WORDS)==0);
	assert(check_result(values,STORAGE_WORDS-1)==-1);
	values[0]^=1; assert(check_result(values,STORAGE_WORDS)==1); values[0]^=1;
	values[254]^=1; assert(check_result(values,STORAGE_WORDS)==255); values[254]^=1;
	values[255]=0; assert(check_result(values,STORAGE_WORDS)==256); values[255]=SENTINEL;
	values[319]=0; assert(check_result(values,STORAGE_WORDS)==320);
	assert(input_a(0)==0xfffffffdU);
	assert((uint32_t)(0xfffffffdU+0xaaaaaaaaU)==values[0]);
	return 0;
}
