/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef IRIS_RAM_ENVIRONMENT_H
#define IRIS_RAM_ENVIRONMENT_H

/* Disposable supervised child only, before fixed runner insertion. No commands,
 * shell, endpoint input, module operations or configurable production paths.
 */
int iris_ram_environment_guard(void);
#ifdef IRIS_RAM_ENV_TESTING
/* Offline fixtures only; production never accepts a root or filesystem hook. */
int iris_ram_environment_guard_test(const char *root);
#endif
#endif
