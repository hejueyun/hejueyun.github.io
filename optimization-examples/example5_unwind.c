int main(void)
{
  int sum = 0;
  for (int i = 0; i < 4; i++)
    sum += i;
  __ESBMC_assert(sum == 6, "sum of 0..3 is 6");
  return 0;
}
