extern int nondet_int(void);

int main(void)
{
  int x = (1 + 2) * 3 - 4 + 5;
  int y = x * 0;
  int z = (y == 0) ? 100 : 200;

  __ESBMC_assert(z == 100, "z folds to 100");
  return 0;
}
