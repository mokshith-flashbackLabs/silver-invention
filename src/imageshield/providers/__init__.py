"""Provider cost control: budgets, circuit breakers, kill switches (step 8).

Everything that decides **whether to call a provider at all** lives here. The
adapters in :mod:`imageshield.search` know how to talk to a provider; this
package knows whether they are allowed to.

The split matters because the guard has to run *before* dispatch. Checking a
budget after the call means the money is already spent, and the entire point is
the check that prevents the spend.
"""
