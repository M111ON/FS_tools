#include <stdio.h>
#include <assert.h>
#include "chain_addr.h"
#include "chain_exit.h"

int main(void) {
    /* complement: upper↔lower */
    assert(chain_complement(0) == 26);   /* A ↔ a */
    assert(chain_complement(26) == 0);   /* a ↔ A */
    assert(chain_complement(25) == 51);  /* Z ↔ z */
    assert(chain_complement(51) == 25);  /* z ↔ Z */

    /* exit choices = 50 */
    assert(chain_exit_choices() == 50);

    /* count valid exits for any entry = 50 */
    for (int e = 0; e < 52; e++) {
        assert(chain_exit_count(e) == 50);
    }

    /* exit valid: not self, not complement */
    assert(chain_exit_valid(0, 1) == 1);   /* A → B ok */
    assert(chain_exit_valid(0, 0) == 0);   /* A → A invalid (self) */
    assert(chain_exit_valid(0, 26) == 0);  /* A → a invalid (complement) */
    assert(chain_exit_valid(26, 0) == 0);  /* a → A invalid (complement) */
    assert(chain_exit_valid(26, 27) == 1); /* a → b ok */

    /* skeleton validation */
    ChainKey k;
    /* valid 6-letter skeleton: A B C D E F (F=5, not 0 or 26) */
    chain_key(&k, "ABCDEF");
    /* need to check: E=5, complement of A(1)=27, 5 != 1 and 5 != 27 → valid */
    /* but chain_key uses CHAIN_BASE=52, 'A'=1, 'B'=2, ..., 'F'=6 */
    /* entry=1 (A), exit=6 (F), complement(1)=27, 6 != 1 and 6 != 27 → valid */
    assert(chain_skeleton_valid(&k) == 1);

    /* invalid: exit = entry */
    ChainKey k2;
    chain_key(&k2, "ABCDEA");
    /* entry=1, exit=1 → invalid */
    assert(chain_skeleton_valid(&k2) == 0);

    /* invalid: too short */
    ChainKey k3;
    chain_key(&k3, "ABCDE");
    assert(chain_skeleton_valid(&k3) == 0);

    /* invalid letter */
    assert(chain_exit_valid(52, 0) == 0);
    assert(chain_exit_valid(0, 52) == 0);

    printf("ALL PASS: complement, exit_valid, exit_count, skeleton_valid\n");
    printf("exit choices: %d (52 - 2)\n", chain_exit_choices());
    return 0;
}
