# Validation

Prepare the environment initially and after dependency or packaging changes:

```sh
scripts/prepare-check
```

Run the complete repository gate with:

```sh
scripts/check
```

It checks formatting, strict types, and focused tests. The normal budget is 30
seconds and the hard deadline is 55 seconds. The check performs no dependency
installation, provider/model calls, real CI waits, scheduled delays, or live
Desktop experiments.

Tests concentrate on durable request/action replay, authorization, ready selection
without Marshal, stale curation, repair limits, broker-held waits, source
reevaluation, hook denial, transcript gaps, bootstrap reruns, exact schedule
binding, report-key conflicts, Tollgate safety, and the public CLI cut. Fake clocks,
small record stores, sockets, and recorded adapter results replace long waits.

Focused live acceptance is established once for the replacement and repeated only
when an affected boundary changes. It must cover standing titles/IDs, native tool
and model support, hook trust and isolation, transcript/accounting evidence, a
pending Steward wait, five-minute CI delivery to the same Warden turn, one Marshal
heartbeat, same-Steward recovery, one Justiciar slot, Desktop/broker restart
uncertainty, and operation-boundary source freshness. Record unsupported behavior
and gaps honestly; unit tests do not prove native compatibility.

The former deterministic provider, App Server/runtime, continuous-controller,
plan-review, curated-memory, fleet-replacement, and live role-suite tests were
removed with those products. Do not revive them as an optional or nightly harness.
