int main(void)
{
  int x = 0;
  for (int i = 0; i < 10; i++) {
    x = x + 1;
  }
  __ESBMC_assert(x >= 0, "x stays non-negative");
  return 0;
}
