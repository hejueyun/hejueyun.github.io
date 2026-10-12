#!/bin/bash
# Regenerate captured ESBMC dump outputs for Optimization.md inline examples.
# Run from notes/optimization-examples/ or any cwd (paths are absolute).

set -e

ESBMC="${ESBMC:-/home/samson/workspace/esbmc/build/src/esbmc/esbmc}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/outputs"
mkdir -p "$OUT"

echo "Using ESBMC at: $ESBMC"
"$ESBMC" --version | head -3
echo

# Common quieting flags for GOTO/VCC dumps: suppress default safety checks so
# output focuses on user instructions only.
QUIET="--no-bounds-check --no-pointer-check --no-div-by-zero-check --no-align-check --no-standard-checks"

run() {
  local label="$1"; shift
  echo "==> $label : $*"
  "$ESBMC" "$@" >"$OUT/$label.txt" 2>&1 || true
}

# --- Example 1: dead code (no-op + unreachable) ---
run 1_dead_default     "$HERE/example1_dead.c" $QUIET --goto-functions-only
run 1_dead_no_remove   "$HERE/example1_dead.c" $QUIET --goto-functions-only --no-remove-no-op --no-remove-unreachable

# --- Example 2: slicer ---
run 2_slice_default    "$HERE/example2_slice.c" $QUIET --show-vcc
run 2_slice_no_slice   "$HERE/example2_slice.c" $QUIET --show-vcc --no-slice

# --- Example 3: simplifier ---
run 3_simplify_default     "$HERE/example3_simplify.c" $QUIET --show-vcc
run 3_simplify_no_simplify "$HERE/example3_simplify.c" $QUIET --show-vcc --no-simplify

# --- Example 4: interval analysis (use --interval-analysis-dump to see inferred intervals) ---
run 4_interval_default "$HERE/example4_interval.c" $QUIET --unwind 11 --no-unwinding-assertions
run 4_interval_with    "$HERE/example4_interval.c" $QUIET --unwind 11 --no-unwinding-assertions --interval-analysis --interval-analysis-dump

# --- Example 5: loop unwinding (loop has 4 iterations; --unwind N needs N>=5 to fully unwind) ---
run 5_unwind_5_vcc     "$HERE/example5_unwind.c" $QUIET --unwind 5 --show-vcc
run 5_unwind_100_vcc   "$HERE/example5_unwind.c" $QUIET --unwind 100 --show-vcc
run 5_unwind_5_run     "$HERE/example5_unwind.c" $QUIET --unwind 5
run 5_unwind_3_run     "$HERE/example5_unwind.c" $QUIET --unwind 3

# --- Example 6: multi-property ---
run 6_multi_default    "$HERE/example6_multi.c" --color never
run 6_multi_multiprop  "$HERE/example6_multi.c" --color never --multi-property

echo
echo "All outputs written to $OUT"
ls -1 "$OUT"
