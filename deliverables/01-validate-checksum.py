"""Validate-Checksum Lambda for the Regional Incident Data Lake ingestion pipeline.

Invoked by the IngestionStateMachine immediately after a .zip lands in the
landing bucket. Streams the object in 8 MiB chunks (S3 GetObject is range-read
friendly) so we never hold the whole file in Lambda memory, then compares the
computed MD5 against the manifest's expected MD5.

Input event (from Step Functions):
    {
        "bucket":      "incident-landing-prod",
        "key":         "agency-a/2026-05-12/feed-001.zip",
        "manifestKey": "agency-a/2026-05-12/feed-001.manifest.json"
    }

Manifest file shape (in S3):
    { "md5": "d41d8cd98f00b204e9800998ecf8427e", "sourceAgency": "...", "recordCount": 12345 }

Return value (consumed by the state machine's ChecksumChoice):
    {
        "bucket": "...",
        "key": "...",
        "manifestKey": "...",
        "checksumValid": true,
        "computedMd5": "d41d8cd9...",
        "expectedMd5": "d41d8cd9...",
        "sizeBytes":   1048576
    }
"""
import hashlib
import json
import logging
from typing import Any
from urllib.parse import quote

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

s3 = boto3.client("s3")

CHUNK_SIZE = 8 * 1024 * 1024              # 8 MiB — keeps Lambda memory low for multi-GB zips
MAX_MANIFEST_BYTES = 1 * 1024 * 1024      # 1 MiB hard cap — manifest is JSON, not data


def _stream_md5(bucket: str, key: str) -> tuple[str, int]:
    """Return (md5_hex, total_bytes) by streaming the object body."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    digest = hashlib.md5()  # noqa: S324 — data-integrity check, not a security primitive
    total = 0
    body = obj["Body"]
    for chunk in iter(lambda: body.read(CHUNK_SIZE), b""):
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _load_manifest(bucket: str, key: str) -> dict[str, Any]:
    head = s3.head_object(Bucket=bucket, Key=key)
    size = head["ContentLength"]
    if size > MAX_MANIFEST_BYTES:
        raise ValueError(
            f"Manifest {bucket}/{key} is {size} bytes, exceeds {MAX_MANIFEST_BYTES} byte cap"
        )
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read())


def _fail(bucket: str, key: str, manifest_key: str, reason: str) -> dict[str, Any]:
    """Return a validation-failure result that routes to the quarantine path
    via the state machine's ChecksumChoice, instead of raising and routing to
    FailureNotification — a file that can't be validated is by policy suspect.
    """
    LOG.warning("validate-checksum: routing to quarantine — %s", reason)
    return {
        "bucket": bucket,
        "key": key,
        "manifestKey": manifest_key,
        "checksumValid": False,
        "computedMd5": "",
        "expectedMd5": "",
        "sizeBytes": 0,
        "failureReason": reason,
        # copySource is URL-encoded so the state machine's s3:copyObject call
        # works for keys containing spaces, '+', '#', or non-ASCII characters.
        "copySource": f"{bucket}/{quote(key, safe='/')}",
    }


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    LOG.info("validate-checksum event=%s", json.dumps(event))

    bucket = event["bucket"]
    key = event["key"]
    manifest_key = event["manifestKey"]

    try:
        manifest = _load_manifest(bucket, manifest_key)
    except Exception as exc:  # noqa: BLE001 — any manifest read failure is suspect
        return _fail(bucket, key, manifest_key, f"manifest_read_error: {exc}")

    expected_raw = manifest.get("md5")
    if not isinstance(expected_raw, str) or not expected_raw:
        return _fail(bucket, key, manifest_key, "manifest_missing_md5_field")
    expected = expected_raw.lower()
    if len(expected) != 32 or any(c not in "0123456789abcdef" for c in expected):
        return _fail(bucket, key, manifest_key, "manifest_md5_not_hex")

    try:
        computed, size = _stream_md5(bucket, key)
    except Exception as exc:  # noqa: BLE001 — file unreadable → quarantine
        return _fail(bucket, key, manifest_key, f"object_read_error: {exc}")

    valid = computed == expected
    result = {
        "bucket": bucket,
        "key": key,
        "manifestKey": manifest_key,
        "checksumValid": valid,
        "computedMd5": computed,
        "expectedMd5": expected,
        "sizeBytes": size,
        "copySource": f"{bucket}/{quote(key, safe='/')}",
        "failureReason": "" if valid else "checksum_mismatch",
    }
    LOG.info(
        "validate-checksum result=%s",
        json.dumps({k: v for k, v in result.items() if k != "copySource"}),
    )
    return result
