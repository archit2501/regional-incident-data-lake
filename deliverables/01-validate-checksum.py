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

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

s3 = boto3.client("s3")

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB — keeps Lambda memory low even for multi-GB zips


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
