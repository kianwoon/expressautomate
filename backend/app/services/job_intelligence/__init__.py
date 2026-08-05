"""Job Intelligence Engine — reasoning about a job order, not extraction.

A higher-order layer than `app.services.ingest`: where the extraction pipeline
pulls *fields* verbatim out of an email (a title, a salary, a location), this
module reasons *about* those already-extracted, already-verified `Opportunity`
fields to answer three questions in sequence:

1. **What is the work?** — `understand` produces a `JDUnderstanding`.
2. **Who would do it well?** — `infer_persona` produces a `CandidatePersona`,
   fed the understanding so the persona inherits the work's own framing.
3. **How do we find them?** — `plan_search` produces a `SearchPlan`, fed both
   the understanding and the persona so the queries aim at the right people.

Each stage is independent and testable on its own, per the design doc's core
principle. They share one discipline that the rest of the LLM stack already
enforces and that this module inherits rather than restates: every piece of
opportunity text reaches a model only after `redact()` has stripped the
protected-attribute glossary codes from it (see `input.assemble`).
"""
