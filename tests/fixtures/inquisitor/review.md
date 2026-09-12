# Synthetic architectural review: shop

Scope: the complete synthetic shop repository (checkout.py, invoices.py,
service.py, README.md). No production repository is reviewed by this fixture.
The test creates an older source commit and a later punctuation-only README
commit. The latest diff contains no product source; it does not determine priority.

Responsibility map: service.preview composes the two public adapters;
checkout.quote and invoices.render each independently convert raw input, validate
quantity/pricing, and compute the same domain total before formatting responses.
There is no storage or network boundary in this small repository. All its source
files were inspected; this does not establish coverage of any real project.

Finding: duplicated order normalization/domain arithmetic boundary (P2).
Evidence: checkout.quote and invoices.render independently implement int coercion,
quantity > 0, unit_cents >= 0, and multiplication. A change to accepted quantities
or rounding would require coordinated changes to both entry points. No observed
production incident or measured speedup is claimed. The README punctuation has
no comparable architectural impact despite being newer.

Direction: centralize normalization and total calculation in one explicit order
value/type boundary, keeping quote's dictionary and render's string interfaces.
Do not split files merely for size. Preserve valid string/integer inputs, zero
price, invalid/missing input exceptions, and public output shape. Review callers
before changing coercion behavior; a stricter parser would be a separate behavior
change. Validate representative accepted and rejected inputs through both entry
points and service.preview before/after the refactor.

Project: shop. Stable problem key: duplicated-order-normalization.
Expected benefit: one place to reason about domain rules and fewer coordinated
edits. Treat as future work; shared finding intake checks project identity and
preserves existing active contracts. A repeated review should add new evidence
to the same underlying finding, not create a daily duplicate. No product code
was edited as part of this review.
