#include <stdio.h>
#include "ecall.h"
// #include "memory.h"

bool rz_userland_ecall(rz_register_t regfile[])
{
    rz_register_t a0 = regfile[10]; // x10, a0
    switch (a0) {
        case 1: //print int in a1
            printf("%d\n", (int)regfile[11]);
            return true;
        case 10: //exit
            puts("Program finished.");
            return false;
        default:
            fprintf(stderr, "Invalid ECALL %u (0x%08X)\n", a0, a0);
            return false;
    }
}
