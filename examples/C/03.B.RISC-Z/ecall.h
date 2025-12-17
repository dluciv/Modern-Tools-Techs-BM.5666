#ifndef __ECALL_H__
#define __ECALL_H__

#include <stdbool.h>
#include "misc.h"

/**
 * @brief ECALL instruction, implements syscalls from https://github.com/61c-teach/venus/wiki/Environmental-Calls
 *
 * @param regfile pointer to CPU register file
 */
bool rz_userland_ecall(rz_register_t regfile[]);

#endif // ECALL_H__
