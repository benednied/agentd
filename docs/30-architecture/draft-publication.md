# Trusted result validation and draft publication

`agentd.publication` implements the final boundary after authenticated worker
collection. It never dispatches coding work or modifies job/run execution state.
The controller builds `PublicationIntent` from its durable approved source,
repository profile, pinned base, worker/run ownership, and collected result commit.
Issue/model text must not construct this object or choose its validation commands.

`CollectedCodingResult` comes from authenticated terminal transport collection,
not the model's final answer. `import_coding_bundle` checks that identity, the
source revision, bundle size/digest and Git prerequisites, imports only the exact
result object into a controller-owned cache, verifies base ancestry, and retains
a deterministic result ref. The current bounded transport accepts up to 256 KiB
of bundle data; larger results must remain retained for explicit collection.
Worker filesystem paths and worker Git configuration are never used for import.

The trusted finalizer checks out the exact result commit in a fresh detached
clone and runs only the pinned profile's argument-vector commands. It records
actual return codes and output digests, with the exact commit, in SQLite. Failed,
unavailable, timed-out, or tracked-content-mutating validation blocks publication.
Repository-generated claims such as “tests passed” have no effect. Temporary
validation clones are cleaned up; source object caches and retained result refs
remain available for recovery and operator inspection.

Validation is arbitrary repository code. `TrustedFinalizer` fails closed unless
an administrative `ValidationRunner` is explicitly injected. The supplied
`BubblewrapValidationRunner` requires Linux user namespaces and `bwrap`. It starts
with an empty filesystem, mounts only read-only system runtime directories,
creates private user/process/IPC/network namespaces and a temporary home, and binds the
validation checkout as its sole writable host mount. No host home, publisher
state, credentials, or network are exposed. Additional toolchain/dependency mounts
are explicit administrative policy and must contain no credentials or controller
state. If the sandbox is unavailable, validation fails; there is no fallback to
uncontained execution. An empty `/proc` avoids exposing host process credentials
and supports the existing container seccomp profile; tests requiring procfs must
use a separately configured, equally isolated runner.

A deployment may inject an equivalent container/remote validation runner. Its
implementation belongs to the trusted administrative domain. Environment filtering
alone does not satisfy this contract. The trusted publisher alone owns GitHub
push/PR credentials. No merge operation is exposed.

## Durable state and reconciliation

`PublicationStore` adds `draft_publications` to the existing controller SQLite
file; it does not create a new scheduling system. Every logical job is bound
immutably to its repository, source revision, base/result commits, run/worker,
profile version, and validation policy. Rebinding a job to a different result or
policy fails closed. A host-level file lock serializes publishers without holding
a SQLite write transaction over network calls. One trusted controller domain and
local SQLite storage are required.

The branch is `agentd/job-<full SHA-256 of repository and job identity>`. Publication
uses create-only compare-and-swap push. If push succeeded and its acknowledgement
was lost, the next attempt reads the existing exact branch and proceeds. A branch
pointing elsewhere blocks publication instead of force-overwriting it.

Before PR creation the controller durably records `create_requested`. On restart
it searches all PR states for the deterministic head branch and verifies the
result commit, base, draft flag and ownership marker. An existing intended PR is
reused; conflicting/multiple PRs block publication. If a create request may have
succeeded but lookup still reports no PR, state remains unresolved and retries
only reconcile. There is intentionally no blind replay of a non-idempotent POST.
An operator must establish that no creation occurred before explicitly repairing
that state. Closed or merged PRs are never replaced by a new automatic PR; a
non-draft result requires operator review.

A published record returns the recorded PR without touching the worker. GitHub
failures never remove validated evidence or ask the scheduler to repeat coding.
PR bodies record source issue/revision, exact base/result, job, worker/run, profile
and validation status. A draft is an output artifact for human/CI review, not
acceptance of the implementation.

## Evidence

`tests/unit/test_publication.py` exercises real Git commits, disconnected result
bundle import, actual validation process exits, validation timeout/failure,
credential environment filtering, result identity mismatches, immutable job
binding, branch/PR conflicts, simulated lost push/create acknowledgements,
controller restart, eventual-consistency absence, and independent publication
retry without access to the original worker or checkout.

The Linux runner was additionally exercised inside the existing worker runtime
container on 2026-09-18. The actual isolated Python process returned zero after
checking an external synthetic secret and host process environment were
inaccessible, credential environment variables were absent, outbound networking
was unavailable, and the mounted checkout remained writable. The runtime's
existing seccomp profile requires explicit user/PID/IPC/network namespaces and
an empty proc directory; unrestricted `--unshare-all` and nested proc mounts are
not assumed. `LD_LIBRARY_PATH=/usr/local/lib` selects the mounted trusted Python
runtime without exposing host configuration. Use
`tools/qualify_publication_sandbox.py` under the deployed validation identity to
repeat these checks; pass only credential-free toolchain paths as runtime mounts.
This containment probe does not itself qualify the provider-backed #66 slice.

`MacOSSandboxValidationRunner` provides a deny-default `sandbox-exec` adapter for
local macOS controllers. It grants read-only system libraries/tools and explicit
administrative toolchain roots, plus writes to only the result checkout and a
fresh scratch directory. Network, arbitrary home content, keychain IPC and other
process inspection have no grants. The environment is rebuilt without publisher
credentials. Filesystem grants apply to resolved targets, so a checkout symlink
does not allow access to an external secret. On 2026-09-18 the real macOS runner
passed both the containment probe and exact-result validation using system Python
and `git diff --check`. Repeat with
`tools/qualify_publication_sandbox.py --runner macos --python /usr/bin/python3`.
Missing or denied OS sandbox support fails closed.
