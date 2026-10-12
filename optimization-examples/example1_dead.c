extern int nondet_int(void);

int main(void)
{
  int x = nondet_int();

  if (0) {
    x = 999;
  }

  ;

  if (1) {
    x = x + 1;
  } else {
    x = 666;
  }

  __ESBMC_assert(x != 666, "x never reaches the unreachable else branch");
  return 0;
}
