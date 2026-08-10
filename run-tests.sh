#!/usr/bin/env bash
# Everything: regenerate fixtures, run the unit suites, then run the harness.
# This is the one command CI runs and the one command to run before handing the
# template to a customer.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

FAILED=0
step() {
  echo
  echo "##############################################################"
  echo "# $1"
  echo "##############################################################"
}

step "Regenerating fixtures"
python3 tests/make_fixtures.py || FAILED=1

step "Unit tests"
python3 -m unittest discover -s tests -p "test_*.py" -v || FAILED=1

step "Harness"
./harness/run_harness.sh || FAILED=1

echo
if [ "$FAILED" -eq 0 ]; then
  echo "ALL GREEN"
else
  echo "FAILURES — see output above"
fi
exit "$FAILED"
