# Issue #66: worker feasibility evidence

On 2026-09-18 an isolated, bounded smoke established that the existing deployed
agentd runtime can perform provider-backed coding with its existing credential
boundary. This is **not** evidence that the complete GitHub issue-to-draft-PR
qualification passed. No GitHub publication was attempted by this smoke.

## Trusted process evidence

1. The existing `deploy/security/runtime_sandbox_probe.py` completed with exit 0:
   `audited bwrap compatibility and pinned Codex permission-profile invariants passed`.
   It exercised the actual pinned SDK command sandbox, credential/state read denial,
   writes outside the leased workspace, and network isolation.
2. The initial provider observation returned expired-authentication HTTP 401.
   The SDK's ordinary managed `account/read` call with `refreshToken: true`
   refreshed the existing credential without copying or exposing tokens.
   A subsequent fresh provider observation reported 52% weekly quota used.
   No rate-limit reset credits were consumed.
3. An actual `gpt-5.6-terra` turn used the existing `agentd-workspace` permission
   profile in a dedicated temporary Git repository. The task created only
   `hello.py`, implementing `add(a, b)`. The turn completed in 6,735 ms within
   a 90-second bound. Trusted provider telemetry reported 28,686 cumulative
   tokens, including 14,080 cached input tokens.
4. The resulting commit is `a52012f859dd5eb4167fc5b5923b274a61dd920f`.
   Its exact source was `def add(a, b):` followed by `return a + b`.
   A trusted SDK `command/exec` call under the same credential-isolated profile
   imported the function and checked positive and negative addition cases;
   its process exit code was 0 with empty stderr. Model claims were not used
   as validation evidence.

The runtime was already configured with SDK 0.144.4, a read-only container root,
non-root UID, no-new-privileges, constrained AppArmor/seccomp, a dedicated auth
home denied to model commands, and an audited bubblewrap compatibility shim.
The host's standalone older CLI and bare bubblewrap were **not** suitable:
its bare network-namespace probe failed. Use the pinned deployed SDK and actual
preflight rather than treating an installed `bwrap` binary as isolation proof.

## Reproduction helper

`tools/qualify_provider_sandbox.py` packages the smoke procedure with a fresh
quota read, ordinary managed token refresh, finite runtime/token ceilings, and
validation inside the permission profile. It takes operator-selected preflight
and workspace-root paths; no host aliases, credentials, addresses, or private
paths are embedded in the helper. It retains the dedicated test repository and
prints JSON identities for recovery. Run it inside the approved runtime only:

```sh
python tools/qualify_provider_sandbox.py \
  --preflight "$TRUSTED_RUNTIME_PREFLIGHT" \
  --workspace-root "$ISOLATED_WORKSPACE_ROOT"
```

The original live evidence above was collected with equivalent ad hoc probes;
the packaged helper has syntax/lint validation, not a second provider run.

## Remaining qualification

The full acceptance run still requires a real explicitly approved GitHub issue,
one durable agentd job, account reservation, compatible remote worker selection,
restart/acknowledgement recovery, trusted collected Git evidence, sandboxed
validation, idempotent draft publication, and tests showing publication failure
never reruns coding. This feasibility probe intentionally does not mark those
acceptance criteria complete.
