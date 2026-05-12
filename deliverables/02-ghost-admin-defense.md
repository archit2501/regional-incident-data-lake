# Ghost Admin Defense — Why an Admin Cannot Override the KMS Key Policy

The assignment asks for a KMS Key Policy that denies `kms:Decrypt` to a Lead AWS Administrator unless they meet a specific security condition, and asks how an admin override is prevented. A key policy alone is **not enough** to answer that — an admin with `AdministratorAccess` also holds `kms:PutKeyPolicy`, so they could simply rewrite the key policy and remove the Deny. The real defense spans four layers.

## Layer 1 — Explicit `Deny` inside the key policy

The key policy in `02-kms-key-policy.json` includes two critical statements:

- **`DenyDecryptToEveryoneElse`** — denies `kms:Decrypt` to every principal whose ARN is not the Glue role or the Break-Glass role. Explicit `Deny` evaluates after Allow and overrides any IAM-side `kms:*` grant from `AdministratorAccess`.
- **`DenyKeyPolicyTampering`** — denies `kms:PutKeyPolicy`, `kms:ScheduleKeyDeletion`, `kms:DisableKey`, and grant-related actions to every principal except `KmsKeyAdmin`. **`DenyKeyPolicyTamperingWithoutMFA`** adds an MFA-recency gate on top, so even the `KmsKeyAdmin` role cannot edit the policy without a fresh MFA prompt.

This stops the obvious paths: a normal admin cannot decrypt, and cannot rewrite the key policy from inside the account.

## Layer 2 — Service Control Policy (the real backstop)

Layer 1 is enforced by the key policy *itself*. If an admin could somehow gain `kms:PutKeyPolicy` (for example, via a poorly scoped IAM role or a future policy mistake), they could swap the key policy and unlock Layer 1. The Service Control Policy in `02-scp-kms-guardrail.json` is the durable defense:

- Attached to the OU the workload account belongs to.
- Denies `kms:PutKeyPolicy`, `kms:ScheduleKeyDeletion`, `kms:DisableKey`, and grant lifecycle actions to *any* principal in the account that is not the `KmsKeyAdmin` role.
- Because SCPs evaluate at the Organizations level, **an account admin cannot override them**. Only a principal in the Organizations management account — which is a separate account with separate credentials, separate MFA, and a separate audit trail — can change SCPs. This is what makes the protection real.
- The same SCP also denies `cloudtrail:StopLogging`, `cloudtrail:DeleteTrail`, `cloudtrail:UpdateTrail`, and `cloudtrail:PutEventSelectors`. Without this, an attacker would simply blind the audit trail first and then tamper.

## Layer 3 — Root account isolation

AWS does not let any policy fully deny the account root, so we make root *unusable in practice*:

- Hardware MFA on the root user, credentials in a sealed envelope in a physical safe.
- No programmatic access keys for root — it can only be used interactively.
- The OU SCP includes `DenyRootPrincipalExceptBilling`, which denies all actions when the principal ARN is the workload account root, except a small allowlist for billing, IAM, and Identity Center setup. Root cannot call `kms:Decrypt` or `kms:PutKeyPolicy` because the SCP denies it, and the SCP itself can only be changed from the Organizations management account.
- Root usage is alerted on via CloudTrail (any `userIdentity.type = Root` event triggers a page).

## Layer 4 — Detective controls (the verifiable audit trail the assignment requires)

The Zero-Trust requirement explicitly calls out a *verifiable audit trail*. We add:

- **CloudTrail** — every `kms:Decrypt`, `kms:GenerateDataKey`, `kms:PutKeyPolicy`, and `sts:AssumeRole` is logged to an immutable S3 bucket in a separate logging account with Object Lock and versioning enabled.
- **EventBridge rule** — fires on `kms:PutKeyPolicy`, `kms:ScheduleKeyDeletion`, `kms:DisableKey`, and `sts:AssumeRole` targeting `BreakGlassDataAccess`. Targets SNS (security team) and PagerDuty (oncall). Any policy edit or break-glass assumption pages a human within seconds.
- **AWS Config** — the managed rule `cmk-backing-key-rotation-enabled` plus a custom rule that diffs the current key policy against the canonical one stored in version control. Drift auto-remediates by reverting (running as `KmsKeyAdmin` via Config remediation).

## Why this satisfies "Zero Trust"

- The Glue role is the *only* principal that routinely decrypts. The `kms:ViaService = glue.us-east-1.amazonaws.com` condition means a stolen Glue role credential cannot be replayed by a human through the CLI — the request must arrive through the Glue service.
- The Break-Glass role exists for genuine emergencies and is gated on MFA from the last 15 minutes. Every assumption is logged, paged, and reviewed in the security stand-up.
- A Lead AWS Administrator with `AdministratorAccess` *cannot* decrypt, *cannot* rewrite the key policy, *cannot* stop CloudTrail, *cannot* quietly delete the key, and *cannot* assume the Glue or Break-Glass roles silently (CloudTrail + EventBridge will page).
- The only path to raw location data is through the Break-Glass role, with MFA, with an audit record. That is the verifiable audit trail the assignment requires.

## Summary table — the kill-chain we block

| Attack | Blocked by |
|--------|------------|
| Admin calls `kms:Decrypt` directly | Key policy `DenyDecryptToEveryoneElse` (L1) |
| Admin rewrites key policy to remove Deny | Key policy `DenyKeyPolicyTampering` (L1) + Org SCP (L2) |
| Admin stops CloudTrail then tampers | Org SCP denies `cloudtrail:StopLogging` (L2) |
| Admin schedules key for deletion | Key policy (L1) + Org SCP (L2) |
| Admin assumes Break-Glass without MFA | Key policy condition (L1) — Decrypt still denied |
| Admin uses root account | Hardware MFA + Org SCP `DenyRootPrincipalExceptBilling` (L3) |
| Admin assumes Break-Glass with MFA (legitimate emergency) | Allowed, paged on EventBridge, recorded in CloudTrail (L4) |
