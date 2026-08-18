# Pull request writing guide

A pull request is part of the permanent history of a project. Write it for the reviewer who reads it today and for the developer who investigates the change two years later.

The diff shows **how** the code changed. The pull request should explain **what changed and why**.

## Title

Write the title like a good Git commit subject.

* Use the imperative mood.
* Start with a capital letter.
* Do not end with a period.
* Aim for 50 characters or fewer.
* Keep the title below 72 characters.
* Describe the change, not the work you performed.
* Use specific nouns and verbs.

A useful test is:

> If merged, this pull request will **[title]**.

Good:

* `Add retry handling for failed imports`
* `Remove deprecated authentication endpoint`
* `Prevent duplicate webhook processing`
* `Use transaction IDs for request tracing`

Avoid:

* `Fixed import bug`
* `Changes to authentication`
* `Some cleanup`
* `Webhook fixes`
* `PROJ-123`
* `WIP new retry stuff`

The title should make sense without the branch name, ticket title, or pull request description.

## Description

Start with the reason for the change.

Explain the problem or requirement before you describe the solution. Give enough context for a reviewer who did not work on the change.

Then describe the solution at a high level.

Do not reproduce the diff in prose. A reviewer can read the code. Document information that the code cannot explain by itself:

* why the change is necessary
* which behavior changes
* important design decisions
* constraints that influenced the solution
* non-obvious side effects
* compatibility implications
* known limitations
* relevant follow-up work

Prefer:

> Failed imports remained in the running state after the worker exited. This prevented the scheduler from retrying them.
>
> This change records the worker lease separately from the import state. The scheduler can now detect an expired lease and retry the import.

Avoid:

> Changed `import.py`.
>
> Added a new field to `Import`.
>
> Updated `scheduler.py` to check the field.
>
> Added some tests.

The second version only narrates the diff.

## Keep the structure small

Use only the sections that contain useful information.

A normal pull request should usually need no more than:

### Why

Explain the problem, requirement, or motivation.

### What changed

Describe the solution and the significant changes.

### Verification

State how you verified the change.

### Risks

Describe important risks, compatibility changes, migrations, or rollout requirements.

Omit empty sections. Do not add ceremony to a small change.

A trivial pull request can consist of a good title and one short paragraph.

## Write simple technical English

Use direct and unambiguous English.

### Use short sentences

Put one main idea in each sentence.

Prefer:

> The worker stores the lease expiration time in the database. The scheduler checks this value before it starts a retry.

Avoid:

> The worker stores the lease expiration time in the database, which is subsequently checked by the scheduler when determining whether it should potentially start another retry.

### Use active voice

State who or what performs an action.

Prefer:

> The scheduler retries expired jobs.

Avoid:

> Expired jobs are retried by the scheduler.

Passive voice is acceptable when the actor is unknown or irrelevant.

### Use one term for one concept

Do not change terminology only to avoid repetition.

If the code calls something a `job`, call it a job throughout the pull request. Do not alternate between `job`, `task`, `operation`, and `work item` unless they mean different things.

Consistency is more useful than stylistic variety.

### Prefer concrete verbs

Prefer:

* `add`
* `remove`
* `return`
* `store`
* `check`
* `reject`
* `retry`
* `parse`
* `validate`

Avoid unnecessary abstractions such as:

* `facilitate`
* `leverage`
* `utilize`
* `enable the ability to`
* `perform the processing of`

Write:

> The API validates the token.

Not:

> The API performs validation of the token.

### Avoid ambiguous references

Make the subject clear.

Avoid:

> This causes it to fail when it is missing.

Prefer:

> A missing transaction ID causes the request to fail.

Be especially careful with `it`, `this`, `that`, and `they` when several objects could match the pronoun.

### Avoid unnecessary jargon

Use established project terminology when it is precise. Do not replace clear language with fashionable terminology.

Avoid buzzwords, idioms, metaphors, jokes, slang, and culture-specific references.

Write:

> The cache reduces database reads.

Not:

> The cache gives the database some breathing room.

Write for readers who know the technology but might not be native English speakers.

### Do not tell the reader that something is easy

Avoid words such as:

* `simply`
* `obviously`
* `clearly`
* `easy`
* `trivial`
* `just`

Describe the actual requirement instead.

Instead of:

> Simply restart the worker.

Write:

> Restart the worker after the migration completes.

### Put conditions before instructions

When the pull request contains an instruction, state the condition first.

Prefer:

> If the database contains jobs created before version 2.4, run the migration before you deploy the worker.

Avoid:

> Run the migration before you deploy the worker if the database contains jobs created before version 2.4.

### Use lists for multiple independent points

Do not hide several changes in a long sentence.

Prefer:

* Reject expired tokens.
* Log the request ID.
* Return `401` for invalid credentials.

Use numbered lists only when order matters.

## Explain decisions, not mechanics

Record decisions that a future developer cannot reconstruct from the code.

Useful:

> The lease uses database time instead of worker time because worker clocks are not guaranteed to be synchronized.

Not useful:

> Added a `lease_expires_at` column and an `if` statement that compares the current time.

Implementation details belong in the description only when they help reviewers understand a design decision, risk, or non-obvious constraint.

## Describe behavior precisely

Prefer observable statements.

Avoid:

> Improve error handling.

Prefer:

> Return `503` when the upstream service times out.

Avoid:

> Make imports more robust.

Prefer:

> Retry imports up to three times after transient network errors.

Avoid claims such as `better`, `faster`, `safer`, or `more reliable` unless the pull request explains what changed or provides evidence.

## Verification

State what you actually verified.

Good:

* Added unit tests for expired and active leases.
* Ran the import integration test against PostgreSQL 17.
* Verified that an interrupted worker retries the job after the lease expires.

Avoid:

* Tested.
* Works locally.
* Tests pass.

If you did not test an important case, say so.

## Risks and compatibility

Call out changes that reviewers might otherwise miss:

* breaking API changes
* schema migrations
* changed defaults
* new dependencies
* changed configuration
* performance trade-offs
* security implications
* deployment ordering
* rollback limitations

Be explicit.

Prefer:

> This migration adds a nullable column and does not rewrite existing rows. The application remains compatible with the previous schema during rollout.

Avoid:

> Migration should be safe.

## References

Put supporting references after the explanation rather than using them as a substitute for it.

Examples:

* `Fixes #123`
* `Related: #456`
* `Design: ADR-0021`

A ticket link does not explain the pull request. The pull request should remain understandable if the external ticket is unavailable.

## Recommended template

```markdown
# Why

Explain the problem or requirement. Describe the previous behavior when
that context is useful.

# What changed

Describe the solution at a high level.

- List significant changes when a list improves clarity.
- Explain important design decisions.
- Do not narrate the diff.

# Verification

- Describe the tests you ran.
- Describe relevant manual verification.

# Risks

Describe compatibility, migration, deployment, security, or performance
considerations. Omit this section when there are none.

Fixes #123
```

## Final check

Before you submit the pull request, verify that:

* The title states what the pull request does.
* The title uses the imperative mood.
* The title is concise and has no trailing period.
* The first paragraph explains why the change exists.
* The description adds information that is not obvious from the diff.
* Each paragraph covers one topic.
* Sentences are short and direct.
* The text uses active voice where practical.
* The same term always means the same thing.
* Technical terms are necessary and precise.
* There are no unnecessary idioms, buzzwords, or filler words.
* Verification describes what you actually tested.
* Important risks and compatibility changes are explicit.
* Issue links supplement the explanation instead of replacing it.
