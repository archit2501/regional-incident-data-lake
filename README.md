# Regional Incident Data Lake — Assignment Deliverables

Submission for the architecture assignment covering a serverless geospatial data lake on AWS.

## Index

| # | Problem | Artifact(s) |
|---|---------|-------------|
| 1 | Automated Ingestion Pipeline (Step Functions) | [`deliverables/01-ingestion-state-machine.asl.json`](deliverables/01-ingestion-state-machine.asl.json), [`deliverables/01-validate-checksum.py`](deliverables/01-validate-checksum.py) |
| 2 | Ghost Admin Defense (KMS + Org SCP) | [`deliverables/02-kms-key-policy.json`](deliverables/02-kms-key-policy.json), [`deliverables/02-ghost-admin-defense.md`](deliverables/02-ghost-admin-defense.md), [`deliverables/02-scp-kms-guardrail.json`](deliverables/02-scp-kms-guardrail.json) |
| 3 | Data Skew Optimization (PySpark / Glue) | [`deliverables/03-data-skew-tech-note.md`](deliverables/03-data-skew-tech-note.md), [`deliverables/03-glue-job-skeleton.py`](deliverables/03-glue-job-skeleton.py) |
| 4 | Secrets Manager Rotation Lambda | [`deliverables/04-rotate-weather-api-key.py`](deliverables/04-rotate-weather-api-key.py) |

## How to review

Open each artifact in the order above. The technical note and the Ghost-Admin write-up are prose; the JSON and Python files are review-ready.

The plan that drove this submission lives at:
- [`docs/superpowers/plans/2026-05-12-aws-data-lake-assignment.md`](docs/superpowers/plans/2026-05-12-aws-data-lake-assignment.md) — full markdown plan
- [`plan/plan.html`](plan/plan.html) — interactive HTML view with diagrams

## Account assumptions

These ARNs are referenced consistently across all artifacts:

| Resource | Value |
|----------|-------|
| Workload account | `123456789012` |
| Region | `us-east-1` |
| Glue service role | `arn:aws:iam::123456789012:role/GlueIngestionRole` |
| Break-glass human role (MFA) | `arn:aws:iam::123456789012:role/BreakGlassDataAccess` |
| KMS key admin (MFA, sole policy editor) | `arn:aws:iam::123456789012:role/KmsKeyAdmin` |
| S3 landing bucket | `incident-landing-prod` |
| S3 quarantine bucket | `incident-quarantine-prod` |
| S3 curated bucket | `incident-curated-prod` |
| SNS alert topic | `arn:aws:sns:us-east-1:123456789012:incident-pipeline-alerts` |
| KMS key alias | `alias/incident-data-lake` |
| Glue job name | `incident-anonymize-and-parquet` |

## Verifying the bundle

```bash
# JSON validity
for f in deliverables/*.json; do python3 -m json.tool "$f" > /dev/null; done

# Python compiles
python3 -m py_compile deliverables/01-validate-checksum.py deliverables/04-rotate-weather-api-key.py
```
