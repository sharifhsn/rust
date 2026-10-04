#include <stdint.h>

extern uint64_t compressed_staticlib_value(uint64_t value);

int main(void) {
    return compressed_staticlib_value(5) == 108 ? 0 : 1;
}
