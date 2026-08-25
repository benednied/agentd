# Pull-request review

A review protects correctness, design integrity, maintainability, and future
readers. It is not a line-by-line style exercise and it does not transfer solution
ownership from the author.

## Review order

1. **Readiness:** confirm that the change has enough context, tests, and a clear
   acceptance condition to review.
2. **Scope:** check that the change is necessary, sufficient, and cohesive.
3. **Design:** understand the system-level approach before inspecting individual
   lines. Check ownership boundaries, complexity, and compatibility.
4. **Correctness and risk:** inspect happy paths, failures, retries, cancellation,
   concurrency, persistence, security, and migration behavior.
5. **Maintainability:** check names, interfaces, documentation, and future change
   cost.
6. **Tests:** review test code as production code and look for missing edge cases.

## Comments

Comments should identify the behavior, cause, and consequence. Refer to the code,
not the person. Use questions when they genuinely invite clarification, but state
the requirement directly when a change is necessary. Examples help when they make
the expected behavior concrete.

Separate blocking correctness or security findings from non-blocking suggestions.
Positive feedback is useful when it identifies a decision or invariant worth
preserving.

## Scope and disagreement

Small, coherent pull requests reduce review latency and make failures easier to
attribute. Reviewers must understand the code they approve; authors remain
responsible for the final solution. Resolve disagreements through project
principles, observable behavior, tests, and explicit architecture decisions.

Approve when the scope is understood, correctness and risk are acceptable, tests
cover the relevant behavior, and remaining comments are non-blocking. Record
important unresolved risks rather than hiding them in approval language.
