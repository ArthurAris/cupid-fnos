#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compile the actual llbitmap state helpers against a small boundary harness."""
import argparse
from pathlib import Path
import subprocess
import tempfile


def function(source, name):
    start = source.index('static void ' + name + '(')
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        if source[end] == '{':
            depth += 1
        elif source[end] == '}':
            depth -= 1
        end += 1
    return source[start:end]


PREFIX = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#define BITMAP_DATA_OFFSET 1024
#define max_t(type, a, b) ((type)(a) > (type)(b) ? (type)(a) : (type)(b))
#define LLPageDirty 0
#define test_bit(bit, value) (!!(*(value) & (1UL << (bit))))
#define set_bit(bit, value) (*(value) |= 1UL << (bit))
static bool test_and_set_bit(unsigned int bit, unsigned long *value) {
    bool old = test_bit(bit, value);
    set_bit(bit, value);
    return old;
}
enum { BitUnwritten=0, BitClean=1, BitDirty=2, BitNeedSync=3,
       BitSyncing=4, BitNeedSyncUnwritten=5, BitSyncingUnwritten=6,
       BitCleanUnwritten=7 };
struct mddev { bool degraded; bool level_456; };
struct llbitmap_page_ctl { unsigned char state[4096]; unsigned long flags, dirty[1]; };
struct llbitmap { struct mddev *mddev; unsigned int io_size; struct llbitmap_page_ctl *pctl[2]; };
static bool raid_is_456(struct mddev *mddev) { return mddev->level_456; }
'''

HARNESS = r'''
int main(void) {
    const unsigned int sizes[] = {512, 1024, 4096};
    unsigned int count = 0;
    for (unsigned int s=0; s<3; s++)
    for (int idx=0; idx<2; idx++)
    for (int branch=0; branch<4; branch++)
    for (int level=0; level<2; level++) {
        struct llbitmap_page_ctl pages[2] = {0};
        struct mddev mddev = {.degraded=branch==2, .level_456=level};
        struct llbitmap bitmap = {.mddev=&mddev, .io_size=sizes[s], .pctl={&pages[0], &pages[1]}};
        unsigned char original[4096];
        unsigned int offset = idx==0 ? 1024 : 0;
        unsigned int block = offset / bitmap.io_size;
        unsigned int first = block * bitmap.io_size;
        unsigned int end = (block+1) * bitmap.io_size;
        struct llbitmap_page_ctl *page = &pages[idx];
        for (unsigned int p=0; p<4096; p++) page->state[p] = p%8;
        if (idx==0) {
            for (unsigned int p=0; p<1024; p++) page->state[p] = p%5;
            page->state[4] = 6;
            page->state[5] = page->state[6] = page->state[7] = 0;
            page->state[24] = 2;
            memset(page->state+60, 0, 4);
            if (first<1024) first = 1024;
        }
        if (branch==0) set_bit(block, page->dirty);
        if (branch==1) page->state[offset+1] = BitDirty;
        memcpy(original, page->state, sizeof(original));
        llbitmap_set_page_dirty(&bitmap, idx, offset, branch!=3);
        for (unsigned int p=0; p<4096; p++) {
            unsigned char expected = original[p];
            if (branch<2 && p>=first && p<end) {
                if (expected==BitUnwritten) expected=level ? BitNeedSync : BitDirty;
                else if (expected==BitClean || expected==BitCleanUnwritten) expected=BitDirty;
            }
            if (page->state[p] != expected) {
                fprintf(stderr, "FAIL io=%u page=%d branch=%d raid456=%d byte=%u actual=%u expected=%u\n",
                        sizes[s], idx, branch, level, p, page->state[p], expected);
                return 1;
            }
        }
        assert(test_bit(block, page->dirty));
        count++;
    }
    /* A dirty-looking header alone must not trigger infection of clean data. */
    {
        struct llbitmap_page_ctl page = {0};
        struct mddev mddev = {0};
        struct llbitmap bitmap = {.mddev=&mddev, .io_size=4096, .pctl={&page, NULL}};
        memset(page.state, BitClean, sizeof(page.state));
        page.state[24] = BitDirty;
        unsigned char original[4096];
        memcpy(original, page.state, sizeof(original));
        llbitmap_set_page_dirty(&bitmap, 0, 1024, true);
        if (memcmp(original, page.state, sizeof(original))) {
            fprintf(stderr, "FAIL header bytes were scanned as data states\n");
            return 1;
        }
        count++;
    }
    printf("LLBITMAP_HEADER_BOUNDARY_TEST_OK cases=%u\n", count);
    return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    source = args.source.read_text()
    helpers = '\n'.join(function(source, name) for name in (
        'llbitmap_infect_dirty_bits', 'llbitmap_set_page_dirty'))
    with tempfile.TemporaryDirectory(prefix='cupid-llbitmap-test-') as directory:
        directory = Path(directory)
        harness = directory / 'test.c'
        binary = directory / 'test'
        harness.write_text(PREFIX + helpers + HARNESS)
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
                        '-fsanitize=address,undefined', '-o', str(binary), str(harness)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
