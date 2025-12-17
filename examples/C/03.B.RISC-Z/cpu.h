#ifndef __CPU_H__
#define __CPU_H__

#include <stdbool.h>
#include "misc.h"

struct rz_cpu_s;

/**
 * @brief Types to represent instance of RISC-Z CPU and pointer to it
 *
 */
typedef struct rz_cpu_s rz_cpu_t, *rz_cpu_p;

/**
 * @brief Create a RISC-Z CPU
 *
 * @return rz_cpu_p pointer to CPU instance
 */
rz_cpu_p rz_create_cpu(void);

/**
 * @brief Deinitialize RISC-Z CPU instance
 *
 * @param pcpu pointer to CPU instance
 */
void rz_free_cpu(rz_cpu_p pcpu);

/**
 * @brief Get CPU info string
 *
 * @param pcpu pointer to CPU instance
 * @return const char* CPU info string
 */
const char *rz_cpu_info(const rz_cpu_p pcpu);

/**
 * @brief Execute one CPU instruction
 *
 * @param pcpu pointer to CPU instance
 * @return true when it is ready to execute more
 * @return false when it wants to finish
 */
bool rz_cycle(rz_cpu_p pcpu);

#endif // CPU_H__
