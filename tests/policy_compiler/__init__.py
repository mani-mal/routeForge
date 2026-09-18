"""The property-based noninterference harness for `app/policy/compile.py`
(design P1a §7).

`tests/test_policy_golden_table.py` pins the *contract* — the finite table of
combinations that must stay legal or stay rejected. This package pins the
*properties*: statements about `compile_policy` / `apply_signals` as functions,
quantified over inputs no hand-written table enumerates, including the values
an operator is not supposed to be able to write.

The package is a package (not a bare directory) so `strategies` can be imported
as `tests.policy_compiler.strategies` from both test modules.
"""
