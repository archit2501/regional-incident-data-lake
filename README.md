# Regional Incident Data Lake — Assignment Deliverables

Submission for the architecture assignment covering a serverless geospatial data lake on AWS.

## Website

Open [`index.html`](index.html) first. It is the full GitHub Pages-ready website that explains every assignment requirement, the architecture, the files produced, the application frontend, and the validation evidence.

Local run:

```bash
python3 -m http.server 8088 --bind 127.0.0.1
```

Then open:

```text
http://127.0.0.1:8088/
```

GitHub Pages deployment: push this repository, then set **Settings -> Pages -> Deploy from branch -> main / root**.

## Index

| # | Problem | Artifact(s) |
|---|---------|-------------|
| Site | Full GitHub Pages website | [`index.html`](index.html) |
| 0 | Reviewer Visual Frontend | [`deliverables/00-reviewer-visual.html`](deliverables/00-reviewer-visual.html) |
| 1 | Automated Ingestion Pipeline (Step Functions) | [`deliverables/01-ingestion-state-machine.asl.json`](deliverables/01-ingestion-state-machine.asl.json), [`deliverables/01-validate-checksum.py`](deliverables/01-validate-checksum.py) |
| 2 | Ghost Admin Defense (KMS + Org SCP) | [`deliverables/02-kms-key-policy.json`](deliverables/02-kms-key-policy.json), [`deliverables/02-ghost-admin-defense.md`](deliverables/02-ghost-admin-defense.md), [`deliverables/02-scp-kms-guardrail.json`](deliverables/02-scp-kms-guardrail.json) |
| 3 | Data Skew Optimization (PySpark / Glue) | [`deliverables/03-data-skew-tech-note.md`](deliverables/03-data-skew-tech-note.md), [`deliverables/03-glue-job-skeleton.py`](deliverables/03-glue-job-skeleton.py) |
| 4 | Secrets Manager Rotation Lambda | [`deliverables/04-rotate-weather-api-key.py`](deliverables/04-rotate-weather-api-key.py) |
| 5 | Operator Console (application UX layer) | [`deliverables/05-operator-console.html`](deliverables/05-operator-console.html) |

## How to review

Start with **`deliverables/00-reviewer-visual.html`**. It is a self-contained frontend for the checker: one visual path through the full architecture, requirement coverage, required artifact links, screenshots, and smoke-test evidence.

Then open each required artifact in the order above. The technical note and the Ghost-Admin write-up are prose; the JSON and Python files are review-ready. **Open `deliverables/05-operator-console.html` directly in a browser** for the application UX layer: a public-safety heatmap for government stakeholders, plus an operator rail showing pipeline status, Glue partition skew, secret rotation, and a KMS access audit.

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
