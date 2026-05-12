extern int nondet_int(void);

int main(void)
{
  int x = nondet_int();
  int y = nondet_int();

  int unused1 = x * 100 + y;
  int unused2 = unused1 * 5 + 7;
  int unused3 = unused2 - 13;

  int needed = x + 1;

  __ESBMC_assert(needed > x, "increment is monotone");
  return 0;
}
