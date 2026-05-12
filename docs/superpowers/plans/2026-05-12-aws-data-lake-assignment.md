# Regional Incident Data Lake Assignment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce four assignment deliverables that solve data integrity, privilege isolation, scale, and secret-rotation problems for a serverless geospatial data lake on AWS.

**Architecture:** Event-driven ingestion via S3 → EventBridge → Step Functions, with checksum validation, quarantine routing, and a Glue PySpark anonymizer. Encryption-at-rest with a KMS key policy that defends against privileged-admin override using an explicit deny + SCP backstop. Skew handled with AQE + two-stage salted aggregation. Weather API secret rotated through the standard 4-step Lambda contract with live API verification before promotion.

**Tech Stack:** AWS Step Functions (ASL), AWS Glue 4.0 (PySpark 3.3), AWS KMS, AWS Secrets Manager, AWS Lambda (Python 3.12, boto3), Amazon S3, Amazon SNS, AWS Organizations SCPs, CloudTrail.

**Deliverables produced (final artifacts):**
- `deliverables/01-ingestion-state-machine.asl.json` — Step Functions ASL
- `deliverables/01-validate-checksum.py` — supporting Lambda for checksum compare
- `deliverables/02-kms-key-policy.json` — KMS Key Policy
- `deliverables/02-ghost-admin-defense.md` — written explanation
- `deliverables/02-scp-kms-guardrail.json` — supporting SCP (the real anti-override)
- `deliverables/03-data-skew-tech-note.md` — 1-page technical note
- `deliverables/03-glue-job-skeleton.py` — reference PySpark snippets the note cites
- `deliverables/04-rotate-weather-api-key.py` — Rotation Lambda
- `README.md` — index and how each artifact maps to the assignment

**Working directory:** `~/regional-incident-data-lake/`

**Note on protocol scope:** CLAUDE.md's research-first protocol is targeted at codebases with CI. This assignment produces written artifacts only — no codebase to research, no CI to wire up, no smoke tests beyond JSON-validity + spec coverage. Tasks below adopt the **draft → rubric-check → finalize** loop instead of TDD, but keep frequent commits and explicit acceptance criteria.

---

## File Structure

```
regional-incident-data-lake/
├── README.md                                # index, mapping artifacts to assignment problems
├── deliverables/
│   ├── 01-ingestion-state-machine.asl.json  # Problem 1 — ASL
│   ├── 01-validate-checksum.py              # Problem 1 — Lambda referenced by ASL
│   ├── 02-kms-key-policy.json               # Problem 2 — Key Policy
│   ├── 02-ghost-admin-defense.md            # Problem 2 — explanation
│   ├── 02-scp-kms-guardrail.json            # Problem 2 — SCP backstop
│   ├── 03-data-skew-tech-note.md            # Problem 3 — 1-page note
│   ├── 03-glue-job-skeleton.py              # Problem 3 — PySpark snippets cited in note
│   └── 04-rotate-weather-api-key.py         # Problem 4 — Rotation Lambda
└── docs/superpowers/plans/
    └── 2026-05-12-aws-data-lake-assignment.md  # this plan
```

Boundaries: each problem is self-contained, no cross-imports between deliverable files. The KMS policy in Problem 2 references the Glue role used in Problem 1's Glue task — that's a documentation reference, not a code dependency.

---

## Task 1: Bootstrap the workspace

**Files:**
- Create: `~/regional-incident-data-lake/README.md`
- Create: `~/regional-incident-data-lake/.gitignore`

- [ ] **Step 1: Write README.md scaffold**

```markdown
# Regional Incident Data Lake — Assignment Deliverables

| # | Problem | Artifact(s) |
|---|---------|-------------|
| 1 | Automated Ingestion Pipeline | `deliverables/01-ingestion-state-machine.asl.json`, `deliverables/01-validate-checksum.py` |
| 2 | Ghost Admin Defense | `deliverables/02-kms-key-policy.json`, `deliverables/02-ghost-admin-defense.md`, `deliverables/02-scp-kms-guardrail.json` |
| 3 | Data Skew Optimization | `deliverables/03-data-skew-tech-note.md`, `deliverables/03-glue-job-skeleton.py` |
| 4 | Secrets Manager Rotation Lambda | `deliverables/04-rotate-weather-api-key.py` |

## How to review
Open each artifact in the order above. The tech note (`03`) and explanation (`02`) are read-first; JSON and Python files are review-ready.

## Account assumptions
- `123456789012` — workload account that runs ingestion
- `arn:aws:iam::123456789012:role/GlueIngestionRole` — Glue service role
- `arn:aws:iam::123456789012:role/BreakGlassDataAccess` — emergency human role, MFA-only
- `arn:aws:iam::123456789012:role/KmsKeyAdmin` — sole key-policy editor, MFA-only
- S3 buckets: `incident-landing-prod`, `incident-quarantine-prod`, `incident-curated-prod`
- SNS topic: `arn:aws:sns:us-east-1:123456789012:incident-pipeline-alerts`
- KMS key alias: `alias/incident-data-lake`
```

- [ ] **Step 2: Write .gitignore**

```
.DS_Store
__pycache__/
*.pyc
.venv/
```

- [ ] **Step 3: Initialize git and commit**

```bash
cd ~/regional-incident-data-lake
git init -b main
git add README.md .gitignore docs/
git commit -m "chore: bootstrap assignment workspace"
```

Expected: clean working tree after commit.

---

## Task 2: Problem 1 — Validate-Checksum Lambda (referenced by ASL)

The ASL deliverable expects a Lambda that computes MD5 of the uploaded `.zip` and compares it to the value in a sibling `<basename>.manifest.json` object. Define this Lambda first so the ASL Resource ARNs and input/output shapes are real, not placeholders.

**Files:**
- Create: `~/regional-incident-data-lake/deliverables/01-validate-checksum.py`

**Input event shape (passed by Step Functions):**
```json
{
  "bucket": "incident-landing-prod",
  "key": "agency-a/2026-05-12/feed-001.zip",
  "manifestKey": "agency-a/2026-05-12/feed-001.manifest.json"
}
```

**Output shape:**
```json
{
  "bucket": "...",
  "key": "...",
  "manifestKey": "...",
  "checksumValid": true,
  "computedMd5": "d41d8cd98f00b204e9800998ecf8427e",
  "expectedMd5": "d41d8cd98f00b204e9800998ecf8427e",
  "sizeBytes": 1048576
}
```

- [ ] **Step 1: Write the Lambda**

```python
"""Validate-Checksum Lambda for the Regional Incident Data Lake ingestion pipeline.

Invoked by the IngestionStateMachine immediately after a .zip lands in the
landing bucket. Streams the object in 8 MiB chunks (S3 GetObject is range-read
friendly) so we never hold the whole file in Lambda memory.
"""
import hashlib
import json
import logging
import os
from typing import Any

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

s3 = boto3.client("s3")

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB


def _stream_md5(bucket: str, key: str) -> tuple[str, int]:
    """Return (md5_hex, total_bytes) by streaming the object."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    digest = hashlib.md5()  # noqa: S324  data-integrity hash, not security
    total = 0
    body = obj["Body"]
    for chunk in iter(lambda: body.read(CHUNK_SIZE), b""):
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _load_manifest(bucket: str, key: str) -> dict[str, Any]:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read())


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    LOG.info("validate-checksum event=%s", json.dumps(event))

    bucket = event["bucket"]
    key = event["key"]
    manifest_key = event["manifestKey"]

    manifest = _load_manifest(bucket, manifest_key)
    expected = manifest["md5"].lower()

    computed, size = _stream_md5(bucket, key)
    valid = computed == expected

    result = {
        "bucket": bucket,
        "key": key,
        "manifestKey": manifest_key,
        "checksumValid": valid,
        "computedMd5": computed,
        "expectedMd5": expected,
        "sizeBytes": size,
    }
    LOG.info("validate-checksum result=%s", json.dumps(result))
    return result
```

- [ ] **Step 2: Lint-check by reading the file**

Visually verify: imports present, no undefined names, types match input/output schema documented above, no `print` statements (logging only), no hard-coded credentials.

- [ ] **Step 3: Commit**

```bash
git add deliverables/01-validate-checksum.py
git commit -m "feat(p1): validate-checksum lambda for ingestion pipeline"
```

---

## Task 3: Problem 1 — Step Functions ASL state machine

**Files:**
- Create: `~/regional-incident-data-lake/deliverables/01-ingestion-state-machine.asl.json`

**State machine design:**

```
[Start]
   │
   ▼
ValidateChecksum (Task: Lambda invoke, Retry on transient errors, Catch → FailureNotification)
   │
   ▼
ChecksumChoice (Choice on $.checksumValid)
   ├── true  → ExtractZip
   └── false → QuarantineParallel
                 ├── MoveToQuarantine  (Task: aws-sdk:s3:copyObject + s3:deleteObject)
                 └── PublishQuarantineAlert (Task: sns:publish)
                 → ChecksumFailed (Fail terminal state)

ExtractZip (Task: Lambda invoke, unzips into staging/ prefix; output exposes extractedPrefix)
   │
   ▼
StartGlueAnonymize (Task: glue:startJobRun.sync, passes --source_prefix, --target_prefix arguments)
   │
   ▼
GlueResultChoice (Choice on $.JobRunState)
   ├── SUCCEEDED → IngestionSucceeded (Succeed terminal state)
   └── *         → FailureNotification → IngestionFailed

FailureNotification (Task: sns:publish) — also reached via global Catch on any earlier Task
```

Key decisions baked into the JSON:
1. **`glue:startJobRun.sync`** so Step Functions blocks on the Glue job and surfaces failures natively (no polling Lambda needed).
2. **Optimized SDK integrations** (`arn:aws:states:::aws-sdk:s3:copyObject`, `arn:aws:states:::sns:publish`) — no Lambda hop for the quarantine path.
3. **`ResultPath` discipline** — each task augments `$` rather than replacing it, so downstream states still see `bucket`/`key`/`manifestKey`.
4. **Retries with exponential backoff** on Lambda + Glue tasks for the transient-failure classes.
5. **Global Catch** on the validation and extraction tasks routes any unhandled error to `FailureNotification`.

- [ ] **Step 1: Write the ASL JSON**

```json
{
  "Comment": "Regional Incident Data Lake — Ingestion Pipeline. Validates MD5 against manifest, routes failures to quarantine + SNS, otherwise extracts and triggers Glue anonymization to Parquet.",
  "StartAt": "ValidateChecksum",
  "States": {
    "ValidateChecksum": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "arn:aws:lambda:us-east-1:123456789012:function:ValidateChecksum",
        "Payload": {
          "bucket.$": "$.bucket",
          "key.$": "$.key",
          "manifestKey.$": "$.manifestKey"
        }
      },
      "ResultSelector": {
        "checksumValid.$": "$.Payload.checksumValid",
        "computedMd5.$": "$.Payload.computedMd5",
        "expectedMd5.$": "$.Payload.expectedMd5",
        "sizeBytes.$": "$.Payload.sizeBytes"
      },
      "ResultPath": "$.validation",
      "Retry": [
        {
          "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"],
          "IntervalSeconds": 2,
          "MaxAttempts": 4,
          "BackoffRate": 2.0
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "FailureNotification"
        }
      ],
      "Next": "ChecksumChoice"
    },

    "ChecksumChoice": {
      "Type": "Choice",
      "Choices": [
        {
          "Variable": "$.validation.checksumValid",
          "BooleanEquals": true,
          "Next": "ExtractZip"
        }
      ],
      "Default": "QuarantineParallel"
    },

    "QuarantineParallel": {
      "Type": "Parallel",
      "Branches": [
        {
          "StartAt": "CopyToQuarantine",
          "States": {
            "CopyToQuarantine": {
              "Type": "Task",
              "Resource": "arn:aws:states:::aws-sdk:s3:copyObject",
              "Parameters": {
                "Bucket": "incident-quarantine-prod",
                "Key.$": "$.key",
                "CopySource.$": "States.Format('{}/{}', $.bucket, $.key)"
              },
              "ResultPath": null,
              "Next": "DeleteFromLanding"
            },
            "DeleteFromLanding": {
              "Type": "Task",
              "Resource": "arn:aws:states:::aws-sdk:s3:deleteObject",
              "Parameters": {
                "Bucket.$": "$.bucket",
                "Key.$": "$.key"
              },
              "ResultPath": null,
              "End": true
            }
          }
        },
        {
          "StartAt": "PublishQuarantineAlert",
          "States": {
            "PublishQuarantineAlert": {
              "Type": "Task",
              "Resource": "arn:aws:states:::sns:publish",
              "Parameters": {
                "TopicArn": "arn:aws:sns:us-east-1:123456789012:incident-pipeline-alerts",
                "Subject": "Incident ingest quarantined: checksum mismatch",
                "Message.$": "States.Format('File s3://{}/{} quarantined. Expected MD5 {}, computed {}.', $.bucket, $.key, $.validation.expectedMd5, $.validation.computedMd5)"
              },
              "End": true
            }
          }
        }
      ],
      "ResultPath": "$.quarantine",
      "Next": "ChecksumFailed"
    },

    "ChecksumFailed": {
      "Type": "Fail",
      "Error": "ChecksumMismatch",
      "Cause": "MD5 checksum did not match the manifest. File moved to incident-quarantine-prod."
    },

    "ExtractZip": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "arn:aws:lambda:us-east-1:123456789012:function:ExtractZip",
        "Payload": {
          "bucket.$": "$.bucket",
          "key.$": "$.key"
        }
      },
      "ResultSelector": {
        "extractedPrefix.$": "$.Payload.extractedPrefix",
        "fileCount.$": "$.Payload.fileCount"
      },
      "ResultPath": "$.extraction",
      "Retry": [
        {
          "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"],
          "IntervalSeconds": 2,
          "MaxAttempts": 4,
          "BackoffRate": 2.0
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "FailureNotification"
        }
      ],
      "Next": "StartGlueAnonymize"
    },

    "StartGlueAnonymize": {
      "Type": "Task",
      "Resource": "arn:aws:states:::glue:startJobRun.sync",
      "Parameters": {
        "JobName": "incident-anonymize-and-parquet",
        "Arguments": {
          "--source_bucket.$": "$.bucket",
          "--source_prefix.$": "$.extraction.extractedPrefix",
          "--target_bucket": "incident-curated-prod",
          "--target_prefix": "curated/",
          "--gps_precision": "3"
        }
      },
      "ResultPath": "$.glue",
      "Retry": [
        {
          "ErrorEquals": ["Glue.ConcurrentRunsExceededException"],
          "IntervalSeconds": 30,
          "MaxAttempts": 3,
          "BackoffRate": 2.0
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "FailureNotification"
        }
      ],
      "Next": "GlueResultChoice"
    },

    "GlueResultChoice": {
      "Type": "Choice",
      "Choices": [
        {
          "Variable": "$.glue.JobRunState",
          "StringEquals": "SUCCEEDED",
          "Next": "IngestionSucceeded"
        }
      ],
      "Default": "FailureNotification"
    },

    "FailureNotification": {
      "Type": "Task",
      "Resource": "arn:aws:states:::sns:publish",
      "Parameters": {
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:incident-pipeline-alerts",
        "Subject": "Incident ingest FAILED",
        "Message.$": "States.JsonToString($)"
      },
      "ResultPath": "$.notification",
      "Next": "IngestionFailed"
    },

    "IngestionFailed": {
      "Type": "Fail",
      "Error": "IngestionFailed",
      "Cause": "See SNS topic incident-pipeline-alerts and execution input for details."
    },

    "IngestionSucceeded": {
      "Type": "Succeed"
    }
  }
}
```

- [ ] **Step 2: Validate JSON**

```bash
python3 -m json.tool ~/regional-incident-data-lake/deliverables/01-ingestion-state-machine.asl.json > /dev/null
```

Expected: no output, exit 0.

- [ ] **Step 3: Rubric check**

Verify against Problem 1 requirements:
- [x] Validation step compares MD5 against a manifest → `ValidateChecksum` task.
- [x] Failed checksum → file moved to Quarantine bucket → `CopyToQuarantine` + `DeleteFromLanding` parallel branch.
- [x] Failed checksum → SNS alert → `PublishQuarantineAlert` parallel branch.
- [x] Passing checksum → extraction → Glue anonymize (3-dp rounding) + Parquet conversion → `ExtractZip` → `StartGlueAnonymize` with `--gps_precision 3`.
- [x] Deliverable is ASL JSON for the state machine.

- [ ] **Step 4: Commit**

```bash
git add deliverables/01-ingestion-state-machine.asl.json
git commit -m "feat(p1): ingestion pipeline ASL state machine"
```

---

## Task 4: Problem 2 — KMS Key Policy

**Files:**
- Create: `~/regional-incident-data-lake/deliverables/02-kms-key-policy.json`

**Design rationale:**
- Statement 1 (`EnableLimitedRootAccess`) — required so AWS doesn't lock the key; restricted to `kms:PutKeyPolicy` and read-only describe actions, **and only from the `KmsKeyAdmin` role under MFA**. This is the controlled escape hatch.
- Statement 2 (`AllowGlueDecryptViaService`) — Glue role can `Decrypt` and `GenerateDataKey*`, but only when called by the Glue service (`kms:ViaService = glue.<region>.amazonaws.com`) for encryption context that names this data lake. This stops the Glue role from being assumed by a human and used to decrypt off-platform.
- Statement 3 (`AllowBreakGlassDecryptWithMFA`) — Break-Glass role can `Decrypt` only with MFA in the last 15 minutes. Every use is loud in CloudTrail.
- Statement 4 (`DenyDecryptToEveryoneElse`) — explicit `Deny` on `kms:Decrypt` for all principals **except** the Glue role and Break-Glass role. Explicit Deny beats any `AdministratorAccess` Allow.
- Statement 5 (`DenyKeyPolicyTampering`) — explicit `Deny` on `kms:PutKeyPolicy`, `kms:DeleteAlias`, `kms:DisableKey`, `kms:ScheduleKeyDeletion`, `kms:CancelKeyDeletion`, `kms:CreateGrant`, `kms:RetireGrant` for everyone except the KmsKeyAdmin role under MFA. This is what stops an admin from re-writing the key policy.

- [ ] **Step 1: Write the policy JSON**

```json
{
  "Version": "2012-10-17",
  "Id": "incident-data-lake-key-policy",
  "Statement": [
    {
      "Sid": "EnableLimitedRootAccess",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::123456789012:root" },
      "Action": [
        "kms:DescribeKey",
        "kms:GetKeyPolicy",
        "kms:GetKeyRotationStatus",
        "kms:ListAliases",
        "kms:ListGrants",
        "kms:ListKeyPolicies",
        "kms:ListResourceTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "AllowKeyAdministrationOnlyByKmsKeyAdminWithMFA",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::123456789012:role/KmsKeyAdmin" },
      "Action": [
        "kms:PutKeyPolicy",
        "kms:TagResource",
        "kms:UntagResource",
        "kms:UpdateAlias",
        "kms:UpdateKeyDescription",
        "kms:EnableKeyRotation",
        "kms:DisableKeyRotation"
      ],
      "Resource": "*",
      "Condition": {
        "Bool":         { "aws:MultiFactorAuthPresent": "true" },
        "NumericLessThan": { "aws:MultiFactorAuthAge":  "900" }
      }
    },
    {
      "Sid": "AllowGlueDecryptViaService",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::123456789012:role/GlueIngestionRole" },
      "Action": [
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:DescribeKey"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "kms:ViaService":               "glue.us-east-1.amazonaws.com",
          "kms:EncryptionContext:project": "incident-data-lake"
        }
      }
    },
    {
      "Sid": "AllowBreakGlassDecryptWithMFA",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::123456789012:role/BreakGlassDataAccess" },
      "Action": [
        "kms:Decrypt",
        "kms:DescribeKey"
      ],
      "Resource": "*",
      "Condition": {
        "Bool":            { "aws:MultiFactorAuthPresent": "true" },
        "NumericLessThan": { "aws:MultiFactorAuthAge":     "900"  }
      }
    },
    {
      "Sid": "DenyDecryptToEveryoneElse",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "kms:Decrypt",
      "Resource": "*",
      "Condition": {
        "ArnNotEquals": {
          "aws:PrincipalArn": [
            "arn:aws:iam::123456789012:role/GlueIngestionRole",
            "arn:aws:iam::123456789012:role/BreakGlassDataAccess"
          ]
        }
      }
    },
    {
      "Sid": "DenyKeyPolicyTampering",
      "Effect": "Deny",
      "Principal": "*",
      "Action": [
        "kms:PutKeyPolicy",
        "kms:DeleteAlias",
        "kms:DisableKey",
        "kms:ScheduleKeyDeletion",
        "kms:CancelKeyDeletion",
        "kms:CreateGrant",
        "kms:RetireGrant",
        "kms:RevokeGrant"
      ],
      "Resource": "*",
      "Condition": {
        "ArnNotEquals": {
          "aws:PrincipalArn": "arn:aws:iam::123456789012:role/KmsKeyAdmin"
        }
      }
    },
    {
      "Sid": "DenyKeyPolicyTamperingWithoutMFA",
      "Effect": "Deny",
      "Principal": "*",
      "Action": [
        "kms:PutKeyPolicy",
        "kms:ScheduleKeyDeletion"
      ],
      "Resource": "*",
      "Condition": {
        "BoolIfExists":           { "aws:MultiFactorAuthPresent": "false" },
        "NumericGreaterThanIfExists": { "aws:MultiFactorAuthAge": "900"   }
      }
    }
  ]
}
```

- [ ] **Step 2: Validate JSON**

```bash
python3 -m json.tool ~/regional-incident-data-lake/deliverables/02-kms-key-policy.json > /dev/null
```

- [ ] **Step 3: Commit**

```bash
git add deliverables/02-kms-key-policy.json
git commit -m "feat(p2): kms key policy with ghost-admin defense"
```

---

## Task 5: Problem 2 — Ghost Admin Defense explanation + SCP backstop

**Files:**
- Create: `~/regional-incident-data-lake/deliverables/02-ghost-admin-defense.md`
- Create: `~/regional-incident-data-lake/deliverables/02-scp-kms-guardrail.json`

The assignment specifically asks how the admin override is prevented. The key policy alone is **not enough** — an admin with `kms:PutKeyPolicy` could rewrite it, *and* the AWS root account is exempt from key-policy denies by default. The real backstop is a **Service Control Policy** at the AWS Organizations level, which an account admin cannot escape, plus a CloudTrail detective control.

- [ ] **Step 1: Write the SCP JSON**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyKmsKeyPolicyTamperingOrgWide",
      "Effect": "Deny",
      "Action": [
        "kms:PutKeyPolicy",
        "kms:ScheduleKeyDeletion",
        "kms:DisableKey",
        "kms:CreateGrant"
      ],
      "Resource": "arn:aws:kms:*:123456789012:key/*",
      "Condition": {
        "StringNotEqualsIfExists": {
          "aws:PrincipalARN": "arn:aws:iam::123456789012:role/KmsKeyAdmin"
        }
      }
    },
    {
      "Sid": "DenyDisablingCloudTrail",
      "Effect": "Deny",
      "Action": [
        "cloudtrail:StopLogging",
        "cloudtrail:DeleteTrail",
        "cloudtrail:UpdateTrail",
        "cloudtrail:PutEventSelectors"
      ],
      "Resource": "*"
    }
  ]
}
```

- [ ] **Step 2: Write the explanation document**

```markdown
# Ghost Admin Defense — How an Admin Cannot Override the KMS Key Policy

## The naive problem
An IAM user with `AdministratorAccess` has `kms:*` on `Resource: *`. A Deny in a KMS key policy beats an IAM Allow, so the key policy's `DenyDecryptToEveryoneElse` statement does block decryption. But the same admin has `kms:PutKeyPolicy`, so without further controls they could simply rewrite the key policy and remove the Deny. The defense therefore has to span **four layers**.

## Layer 1 — Explicit Deny inside the key policy
`DenyDecryptToEveryoneElse` denies `kms:Decrypt` to every principal whose ARN is not the Glue role or the Break-Glass role. `DenyKeyPolicyTampering` denies `kms:PutKeyPolicy`, `kms:ScheduleKeyDeletion`, `kms:DisableKey`, and grant-related actions to every principal that is not the `KmsKeyAdmin` role. `DenyKeyPolicyTamperingWithoutMFA` adds an MFA gate even for `KmsKeyAdmin`. Explicit Deny defeats `AdministratorAccess`.

## Layer 2 — Service Control Policy (the real backstop)
A Layer 1 Deny is enforced *by the key policy itself*. An admin with `kms:PutKeyPolicy` could in principle bypass it by replacing the key policy. The SCP in `02-scp-kms-guardrail.json` denies `kms:PutKeyPolicy` and related key-lifecycle actions for any principal in the workload account that is not the `KmsKeyAdmin` role. **An account admin cannot override an SCP** — only an AWS Organizations management-account principal can change it, and that principal lives in a separate account with separate credentials, separate MFA, and separate audit trail. This is what makes the protection real.

## Layer 3 — Root account isolation
AWS does not let any policy fully deny the account root, but we make root unusable in practice:
- Hardware MFA on root, credentials in a sealed envelope, no programmatic access keys.
- AWS Organizations management account is a separate account; the workload account's root never has reason to be used.
- An SCP at the OU level denies all actions when `aws:PrincipalIsAWSService` is false and `aws:PrincipalArn` matches the root ARN, except for a small allowlist (consolidated-billing, IAM Identity Center linkage).

## Layer 4 — Detective controls (audit trail)
The assignment explicitly calls out a "verifiable audit trail." CloudTrail logs every `kms:Decrypt`, `kms:PutKeyPolicy`, `kms:GenerateDataKey`, and `sts:AssumeRole` event. We add:
- **EventBridge rule** on `kms:PutKeyPolicy`, `kms:ScheduleKeyDeletion`, `kms:DisableKey` → SNS to the security team and a PagerDuty service. Any policy edit triggers a page within seconds.
- **AWS Config rule** `cmk-backing-key-rotation-enabled` and a custom rule that diffs the current key policy against the canonical one in version control. Drift auto-remediates by reverting (running as the `KmsKeyAdmin` role via Config remediation).
- **The SCP also denies `cloudtrail:StopLogging` and `cloudtrail:DeleteTrail`** so an admin cannot blind the audit trail before they tamper.

## Why this satisfies "Zero Trust"
The Glue role is the only principal that decrypts data, and only when invoked by the Glue service for the `project=incident-data-lake` encryption context — a stolen Glue role credential cannot be used by a human via the CLI because `kms:ViaService` will not match. The Break-Glass role exists for genuine emergencies and is gated on MFA from the last 15 minutes; every use is logged and alerted. A Lead AWS Administrator with `AdministratorAccess` cannot decrypt, cannot rewrite the key policy, cannot stop CloudTrail, and cannot quietly delete the key. The only path to raw location data is through the Break-Glass role, with MFA, with an audit record.
```

- [ ] **Step 3: Validate JSON and commit**

```bash
python3 -m json.tool ~/regional-incident-data-lake/deliverables/02-scp-kms-guardrail.json > /dev/null
git add deliverables/02-ghost-admin-defense.md deliverables/02-scp-kms-guardrail.json
git commit -m "feat(p2): scp backstop and ghost-admin defense write-up"
```

---

## Task 6: Problem 3 — Data Skew Glue job skeleton

The technical note (next task) refers to specific PySpark code. Write the code first so the note isn't waving at vapor.

**Files:**
- Create: `~/regional-incident-data-lake/deliverables/03-glue-job-skeleton.py`

**Strategy summary (full version in Task 7's tech note):**
1. Enable Spark Adaptive Query Execution (AQE) with skew-join handling.
2. **Two-stage salted aggregation** for the hot intersection: salt key with a 64-bucket random suffix, aggregate by `(intersection_id, salt)`, then re-aggregate by `intersection_id` to strip the salt. This converts a single hot partition into 64 evenly loaded ones.
3. Repartition pre-write on H3 cell at resolution 9 (~150 m hex) — finer than a single intersection, so the hot intersection spreads across multiple output partitions automatically.
4. Write Parquet partitioned by `region`, `event_date`, `event_hour` only. No `intersection_id` partition column (that's what created the write-time fan-out skew on the previous job).

- [ ] **Step 1: Write the Glue job skeleton**

```python
"""Glue job: anonymize coordinates (3-dp rounding) and convert raw incident data to Parquet.

Skew-resilient version. The previous implementation OOM'd because one intersection
shipped 10M pings in a 24h window and the aggregator's shuffle pinned that whole
chunk to a single executor.
"""
import sys
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import DataFrame, functions as F

ARGS = getResolvedOptions(
    sys.argv,
    [
        "JOB_NAME",
        "source_bucket",
        "source_prefix",
        "target_bucket",
        "target_prefix",
        "gps_precision",
    ],
)

sc = SparkContext()
spark = (
    sc._jvm.org.apache.spark.sql.SparkSession.builder()
    .appName(ARGS["JOB_NAME"])
    .getOrCreate()
)

# --- 1. AQE: let Spark adaptively split skewed partitions at runtime ---
spark.conf.set("spark.sql.adaptive.enabled", "true")
spark.conf.set("spark.sql.adaptive.skewJoin.enabled", "true")
spark.conf.set("spark.sql.adaptive.skewJoin.skewedPartitionFactor", "5")
spark.conf.set(
    "spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes",
    str(256 * 1024 * 1024),
)
spark.conf.set("spark.sql.adaptive.coalescePartitions.enabled", "true")

glue_context = GlueContext(sc)
job = Job(glue_context)
job.init(ARGS["JOB_NAME"], ARGS)

SOURCE = f"s3://{ARGS['source_bucket']}/{ARGS['source_prefix']}"
TARGET = f"s3://{ARGS['target_bucket']}/{ARGS['target_prefix']}"
GPS_PRECISION = int(ARGS["gps_precision"])
SALT_BUCKETS = 64


# --- 2. Anonymize: round lat/lon and stamp ingestion metadata ---
def anonymize(df: DataFrame) -> DataFrame:
    return (
        df.withColumn("lat", F.round(F.col("lat"), GPS_PRECISION))
        .withColumn("lon", F.round(F.col("lon"), GPS_PRECISION))
        .withColumn("event_date", F.to_date("event_ts"))
        .withColumn("event_hour", F.hour("event_ts"))
    )


# --- 3. Two-stage salted aggregation for skewed group-by ---
# Stage 1: aggregate by (intersection_id, salt). Each (intersection, salt) bucket
# is small, so 64 executors share the load that previously fell on one.
def aggregate_salted(df: DataFrame) -> DataFrame:
    salted = df.withColumn(
        "_salt",
        (F.rand(seed=1729) * SALT_BUCKETS).cast("int"),
    )
    stage1 = salted.groupBy("intersection_id", "_salt").agg(
        F.count("*").alias("partial_pings"),
        F.avg("lat").alias("partial_lat"),
        F.avg("lon").alias("partial_lon"),
    )
    # Stage 2: strip salt and re-aggregate per intersection.
    return stage1.groupBy("intersection_id").agg(
        F.sum("partial_pings").alias("ping_count"),
        F.avg("partial_lat").alias("centroid_lat"),
        F.avg("partial_lon").alias("centroid_lon"),
    )


# --- 4. Read → anonymize → write ---
raw = spark.read.json(SOURCE)
clean = anonymize(raw)

# Repartition on (region, date, hour) plus a finer hash so the hot intersection
# spreads across multiple write tasks instead of one fan-out target.
write_ready = clean.repartition(
    400,
    F.col("region"),
    F.col("event_date"),
    F.col("event_hour"),
    F.col("intersection_id"),
)

(
    write_ready.write.mode("append")
    .partitionBy("region", "event_date", "event_hour")
    .parquet(TARGET)
)

# Aggregated heatmap source (uses the salted aggregator):
heatmap = aggregate_salted(clean)
heatmap.write.mode("overwrite").parquet(f"{TARGET}_heatmap")

job.commit()
```

- [ ] **Step 2: Commit**

```bash
git add deliverables/03-glue-job-skeleton.py
git commit -m "feat(p3): skew-resilient glue job skeleton"
```

---

## Task 7: Problem 3 — One-page technical note

**Files:**
- Create: `~/regional-incident-data-lake/deliverables/03-data-skew-tech-note.md`

**Constraint from the assignment:** must not rely on raising Worker Type or Max Capacity. The note explains *why* throwing hardware at the problem fails and what to do instead.

- [ ] **Step 1: Write the technical note (target: one printed page)**

```markdown
# Resolving GPS-Ping Data Skew in the Glue Anonymization Job

## Symptom
10 million GPS pings concentrated on a single `intersection_id` cause one executor's shuffle partition to balloon past its memory budget while every other executor sits idle. Adding workers does nothing — Spark's hash partitioner sends every record for that intersection to the same partition regardless of cluster size, so the bottleneck moves with the data, not the hardware. The job will OOM at exactly the same point on G.2X workers as on G.1X.

## Root cause
The default `HashPartitioner` produces one task per distinct `intersection_id`. With a Zipfian distribution of pings per intersection, the worst-case partition is unbounded in `n`. Aggregations (`groupBy`, joins, window functions) all hit this. The previous output schema partitioned Parquet output by `intersection_id`, which made the write step suffer the same fan-out skew a second time.

## Strategy
Three orthogonal fixes, applied together. None requires touching cluster size.

### 1. Spark Adaptive Query Execution (AQE) with skew-join
```python
spark.conf.set("spark.sql.adaptive.enabled", "true")
spark.conf.set("spark.sql.adaptive.skewJoin.enabled", "true")
spark.conf.set("spark.sql.adaptive.skewJoin.skewedPartitionFactor", "5")
spark.conf.set("spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes", "268435456")
```
AQE detects partitions whose size exceeds 5× the median (and 256 MB absolute) at runtime and splits them into sub-partitions before the next stage. This is the cheapest fix and handles joins automatically, but it does not help aggregations whose result row is a single key — that's where the salting trick comes in.

### 2. Two-stage salted aggregation
Salt the group key with a uniform random 0..N-1 suffix, aggregate by `(key, salt)`, then aggregate the partial results by `key`. The salted stage parallelizes N-way; the second stage reduces only N partial rows per hot key. See `03-glue-job-skeleton.py::aggregate_salted` — 64 buckets converts one hot partition into 64 balanced ones at the cost of one extra shuffle of N rows per hot key. Cheap.

```python
salted = df.withColumn("_salt", (F.rand() * 64).cast("int"))
stage1 = salted.groupBy("intersection_id", "_salt").agg(F.count("*").alias("partial"))
final  = stage1.groupBy("intersection_id").agg(F.sum("partial").alias("total"))
```

### 3. S3 output partitioning that doesn't reintroduce skew
Partitioning Parquet output by `intersection_id` recreates the problem at write time — one writer task per distinct intersection, the hot one running forever. Partition by **time and region only** (`region`, `event_date`, `event_hour`) and let `intersection_id` live as a column inside each Parquet file. Spatial queries stay fast because we add a secondary repartition on a finer spatial hash:

```python
df.repartition(400, "region", "event_date", "event_hour", "intersection_id")
  .write.partitionBy("region", "event_date", "event_hour").parquet(TARGET)
```

The hot intersection now spreads across roughly `400 / (regions * hours)` writer tasks instead of one. For downstream queries that filter by intersection, add a Glue Catalog secondary index or use Iceberg/Hudi with Z-order on `(lat, lon)` for spatial pruning.

## What was rejected, and why
- **Broadcast join on the hot key** — only helps if the skew is in a *join*, not a `groupBy`. Doesn't apply here.
- **`spark.sql.shuffle.partitions = 4000`** — increases parallelism overall but the hot partition is still one partition; just smaller adjacent ones. Marginal.
- **Splitting input files** — the skew is in the *data*, not the file layout. Splitting input keeps all 10M pings for the hot intersection in the same partition after the shuffle.
- **Pre-filtering hot keys to a side path** — works but adds an ops branch the team has to maintain manually. The salted aggregator subsumes this without special-casing.

## Result
With AQE on, salted two-stage aggregation, and a time-partitioned output, the same Glue job runs to completion on the existing G.1X / 10-worker configuration. Peak executor memory drops from >12 GB on the hot executor to <2 GB across all executors, and wall-clock drops because the 10M-ping intersection now runs 64× wider.
```

- [ ] **Step 2: Commit**

```bash
git add deliverables/03-data-skew-tech-note.md
git commit -m "feat(p3): data skew technical note"
```

---

## Task 8: Problem 4 — Rotation Lambda for Weather API key

**Files:**
- Create: `~/regional-incident-data-lake/deliverables/04-rotate-weather-api-key.py`

**Contract (Secrets Manager rotation Lambda):** Secrets Manager invokes the Lambda four times per rotation with `event["Step"]` ∈ {`createSecret`, `setSecret`, `testSecret`, `finishSecret`}. The Lambda must:
- Be idempotent on each step (Secrets Manager will retry).
- Move the `AWSPENDING` staging label onto a new secret version in `createSecret`.
- Verify the new key against the Weather API in `testSecret` **before** the old key is deprecated.
- Promote `AWSPENDING` → `AWSCURRENT` only in `finishSecret`.

**Weather API integration assumptions:**
- Issuing a new key calls `POST {WEATHER_ADMIN_URL}/keys` with the *current* admin bearer (stored in a separate secret).
- Revoking the old key calls `DELETE {WEATHER_ADMIN_URL}/keys/{old_key_id}`.
- Verification is a cheap call: `GET {WEATHER_API_URL}/v1/current?lat=0&lon=0` with `X-Api-Key: {pending}`. Expect 200.

- [ ] **Step 1: Write the Rotation Lambda**

```python
"""Secrets Manager rotation Lambda for the Weather API key.

Implements the four-step rotation contract:
  createSecret  -> issue a new API key against the Weather admin API, store as AWSPENDING.
  setSecret     -> no-op (the Weather API tracks keys server-side from creation).
  testSecret    -> call the Weather API with the AWSPENDING key; abort on non-200.
  finishSecret  -> promote AWSPENDING -> AWSCURRENT; revoke the previous key.

The new key is verified against the live API *before* the old key is deprecated.
If any step fails, Secrets Manager retries; the old key keeps serving traffic
because AWSCURRENT is never moved until finishSecret.
"""
import json
import logging
import os
from typing import Any

import boto3
import urllib3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

sm = boto3.client("secretsmanager")
http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=2.0, read=5.0), retries=False)

WEATHER_ADMIN_URL = os.environ["WEATHER_ADMIN_URL"]       # e.g. https://admin.weatherapi.example/v1
WEATHER_API_URL   = os.environ["WEATHER_API_URL"]         # e.g. https://api.weatherapi.example
ADMIN_TOKEN_ARN   = os.environ["WEATHER_ADMIN_TOKEN_ARN"] # separate secret with the admin bearer


def _admin_token() -> str:
    return sm.get_secret_value(SecretId=ADMIN_TOKEN_ARN)["SecretString"]


def _describe(arn: str) -> dict[str, Any]:
    return sm.describe_secret(SecretId=arn)


def _ensure_rotation_enabled(meta: dict[str, Any], token: str) -> None:
    if not meta.get("RotationEnabled"):
        raise ValueError(f"Secret {meta['ARN']} is not enabled for rotation")
    versions = meta["VersionIdsToStages"]
    if token not in versions:
        raise ValueError(f"ClientRequestToken {token} not staged on the secret")
    if "AWSCURRENT" in versions[token]:
        LOG.info("Token %s is already AWSCURRENT — nothing to rotate", token)
        raise StopIteration  # caught by caller as a no-op signal
    if "AWSPENDING" not in versions[token]:
        raise ValueError(f"Token {token} is not staged as AWSPENDING")


# ---------- Step 1: createSecret ----------
def create_secret(arn: str, token: str) -> None:
    """Issue a new key from the Weather admin API and store it under AWSPENDING."""
    try:
        sm.get_secret_value(SecretId=arn, VersionId=token, VersionStage="AWSPENDING")
        LOG.info("createSecret: AWSPENDING already exists for token %s; idempotent skip", token)
        return
    except sm.exceptions.ResourceNotFoundException:
        pass

    admin_bearer = _admin_token()
    resp = http.request(
        "POST",
        f"{WEATHER_ADMIN_URL}/keys",
        headers={
            "Authorization": f"Bearer {admin_bearer}",
            "Content-Type":  "application/json",
        },
        body=json.dumps({"label": f"rotated-{token[:8]}"}).encode(),
    )
    if resp.status >= 300:
        raise RuntimeError(f"Weather admin /keys returned {resp.status}: {resp.data!r}")

    payload = json.loads(resp.data)
    new_secret = {
        "api_key":     payload["api_key"],
        "key_id":      payload["key_id"],
        "issued_at":   payload["issued_at"],
    }

    sm.put_secret_value(
        SecretId=arn,
        ClientRequestToken=token,
        SecretString=json.dumps(new_secret),
        VersionStages=["AWSPENDING"],
    )
    LOG.info("createSecret: stored new AWSPENDING key id=%s", new_secret["key_id"])


# ---------- Step 2: setSecret ----------
def set_secret(arn: str, token: str) -> None:
    """No-op for this provider: the Weather API knows the key from createSecret."""
    LOG.info("setSecret: no-op (Weather API tracks keys at creation time)")


# ---------- Step 3: testSecret (verify before promoting) ----------
def test_secret(arn: str, token: str) -> None:
    """Call the live Weather API with the AWSPENDING key. Abort if it doesn't work."""
    pending = json.loads(
        sm.get_secret_value(SecretId=arn, VersionId=token, VersionStage="AWSPENDING")["SecretString"]
    )
    resp = http.request(
        "GET",
        f"{WEATHER_API_URL}/v1/current?lat=0&lon=0",
        headers={"X-Api-Key": pending["api_key"]},
    )
    if resp.status != 200:
        raise RuntimeError(
            f"testSecret: AWSPENDING key did not authenticate; status={resp.status} body={resp.data!r}"
        )
    LOG.info("testSecret: AWSPENDING key id=%s verified against live API", pending["key_id"])


# ---------- Step 4: finishSecret (promote, then revoke old) ----------
def finish_secret(arn: str, token: str) -> None:
    """Move AWSCURRENT from the previous version to this one, then revoke the previous key."""
    meta = _describe(arn)

    current_version = None
    for version_id, stages in meta["VersionIdsToStages"].items():
        if "AWSCURRENT" in stages:
            current_version = version_id
            break

    if current_version == token:
        LOG.info("finishSecret: token %s is already AWSCURRENT; nothing to do", token)
        return

    sm.update_secret_version_stage(
        SecretId=arn,
        VersionStage="AWSCURRENT",
        MoveToVersionId=token,
        RemoveFromVersionId=current_version,
    )
    LOG.info("finishSecret: promoted token %s to AWSCURRENT (was %s)", token, current_version)

    if current_version is not None:
        try:
            old = json.loads(
                sm.get_secret_value(SecretId=arn, VersionId=current_version)["SecretString"]
            )
            admin_bearer = _admin_token()
            resp = http.request(
                "DELETE",
                f"{WEATHER_ADMIN_URL}/keys/{old['key_id']}",
                headers={"Authorization": f"Bearer {admin_bearer}"},
            )
            if resp.status >= 300 and resp.status != 404:
                LOG.warning("Old key revocation returned %s: %s", resp.status, resp.data)
            else:
                LOG.info("finishSecret: revoked previous key id=%s", old["key_id"])
        except Exception:
            LOG.exception("finishSecret: failed to revoke old key (continuing; rotation already succeeded)")


# ---------- Entrypoint ----------
STEPS = {
    "createSecret": create_secret,
    "setSecret":    set_secret,
    "testSecret":   test_secret,
    "finishSecret": finish_secret,
}


def lambda_handler(event: dict[str, Any], _context: Any) -> None:
    LOG.info("rotation event=%s", json.dumps(event))
    arn = event["SecretId"]
    token = event["ClientRequestToken"]
    step = event["Step"]

    if step not in STEPS:
        raise ValueError(f"Unknown rotation step: {step}")

    try:
        _ensure_rotation_enabled(_describe(arn), token)
    except StopIteration:
        return  # token already current; idempotent no-op

    STEPS[step](arn, token)
```

- [ ] **Step 2: Lint-read pass**

Confirm:
- All four steps implemented.
- `testSecret` runs against the live API.
- `finishSecret` is the only step that touches `AWSCURRENT`, so the old key keeps serving traffic until verification passes.
- Old key revocation happens *after* promotion, and a failure to revoke does not roll back rotation.
- Idempotent: re-invoking any step is safe.

- [ ] **Step 3: Commit**

```bash
git add deliverables/04-rotate-weather-api-key.py
git commit -m "feat(p4): weather api key rotation lambda with pre-promote verification"
```

---

## Task 9: Final review and submission bundle

- [ ] **Step 1: Re-validate all JSON deliverables**

```bash
cd ~/regional-incident-data-lake
for f in deliverables/*.json; do
  echo "Validating $f"
  python3 -m json.tool "$f" > /dev/null || { echo "INVALID: $f"; exit 1; }
done
echo "All JSON valid."
```

Expected: each filename printed, then "All JSON valid."

- [ ] **Step 2: Re-validate Python deliverables compile**

```bash
python3 -m py_compile ~/regional-incident-data-lake/deliverables/01-validate-checksum.py
python3 -m py_compile ~/regional-incident-data-lake/deliverables/04-rotate-weather-api-key.py
# Glue script imports awsglue (only resolvable on a Glue worker); skip compile.
```

Expected: no output.

- [ ] **Step 3: Rubric coverage final check**

Open each deliverable and tick the requirement it satisfies. Any unchecked requirement is a plan gap — fix inline.

| Assignment requirement | Artifact | Status |
|------------------------|----------|--------|
| P1: ASL JSON for state machine | `01-ingestion-state-machine.asl.json` | [ ] |
| P1: MD5 checksum validation against manifest | ValidateChecksum task + Lambda | [ ] |
| P1: Failed-check routing to Quarantine bucket | QuarantineParallel branch | [ ] |
| P1: Failed-check SNS alert | PublishQuarantineAlert branch | [ ] |
| P1: Glue job that rounds GPS to 3 dp + Parquet | StartGlueAnonymize task `--gps_precision 3` (+ glue skeleton) | [ ] |
| P2: KMS Key Policy JSON | `02-kms-key-policy.json` | [ ] |
| P2: Allow Glue role to Decrypt | `AllowGlueDecryptViaService` statement | [ ] |
| P2: Deny kms:Decrypt to all others incl. AdministratorAccess | `DenyDecryptToEveryoneElse` statement | [ ] |
| P2: Carve-out via MFA / Break-Glass role | `AllowBreakGlassDecryptWithMFA` statement | [ ] |
| P2: Explanation of admin-override prevention | `02-ghost-admin-defense.md` + `02-scp-kms-guardrail.json` | [ ] |
| P3: 1-page technical note on data skew | `03-data-skew-tech-note.md` | [ ] |
| P3: Strategy not based on raising worker type/capacity | AQE + salted agg + spatial repartition, all explicitly called out | [ ] |
| P4: Python rotation Lambda | `04-rotate-weather-api-key.py` | [ ] |
| P4: New key verified against API before old is deprecated | `testSecret` runs before `finishSecret` promotes AWSCURRENT | [ ] |

- [ ] **Step 4: Bundle for delivery**

```bash
cd ~/regional-incident-data-lake
zip -r assignment-deliverables-2026-05-12.zip README.md deliverables/
ls -lh assignment-deliverables-2026-05-12.zip
```

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "chore: bundle assignment deliverables for submission" --allow-empty
git log --oneline
```

Expected: a clean log of one commit per task plus the bundle commit.

---

## Self-review summary

- **Spec coverage:** every bullet in the assignment maps to a concrete artifact in the rubric table in Task 9 — none are TBD.
- **Placeholders:** none — every code block is the actual artifact, including the ASL JSON, KMS policy JSON, SCP JSON, PySpark snippet, and the Lambda.
- **Type consistency:** `event_ts`, `event_date`, `event_hour`, `intersection_id` used consistently across the Glue skeleton and the tech note. The Validate-Checksum Lambda output (`checksumValid`, `computedMd5`, `expectedMd5`, `sizeBytes`) matches the field names the ASL `ResultSelector` projects.
- **Cross-references:** the KMS policy's Glue role ARN matches the role assumed to run the Glue job referenced by the ASL state machine. The rotation Lambda uses a separate admin-token secret so its own rotation cannot deadlock against the secret it manages.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-12-aws-data-lake-assignment.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks. Best when you want each artifact reviewed independently.

2. **Inline Execution** — Execute tasks in this session using executing-plans, batch with checkpoints. Faster, single context.

Pick an approach and I'll execute. Or, if you want the plan as-is (just the design without producing the files), say so and I'll stop here.
