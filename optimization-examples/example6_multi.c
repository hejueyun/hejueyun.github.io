extern int nondet_int(void);

int main(void)
{
  int x = nondet_int();
  __ESBMC_assert(x != 1, "x is not 1");
  __ESBMC_assert(x != 2, "x is not 2");
  return 0;
}
