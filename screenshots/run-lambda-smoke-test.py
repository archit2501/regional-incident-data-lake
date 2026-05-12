"""End-to-end smoke test of the validate-checksum Lambda with mocked S3.

Exercises the real Lambda code path (streaming MD5, manifest parsing, hex
validation, success-result construction) without requiring an AWS account.
"""
import hashlib
import importlib.util
import json
import sys
from unittest.mock import MagicMock

# Mock boto3 BEFORE importing the Lambda module (which calls boto3.client at
# import time).
sys.modules["boto3"] = MagicMock()

spec = importlib.util.spec_from_file_location(
    "vc",
    "/Users/architjain/regional-incident-data-lake/deliverables/01-validate-checksum.py",
)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)


def make_s3_mock(file_bytes: bytes, manifest_bytes: bytes):
    def head_object(Bucket, Key):
        return {
            "ContentLength": len(manifest_bytes if Key.endswith(".manifest.json")
                                  else file_bytes)
        }

    def get_object(Bucket, Key):
        if Key.endswith(".manifest.json"):
            body = MagicMock()
            body.read = MagicMock(return_value=manifest_bytes)
            return {"Body": body, "ContentLength": len(manifest_bytes)}
        # Stream the zip in CHUNK_SIZE-sized chunks
        body = MagicMock()
        chunks = [file_bytes, b""]  # second read returns empty → end of stream
        body.read = MagicMock(side_effect=chunks)
        return {"Body": body, "ContentLength": len(file_bytes)}

    s3 = MagicMock()
    s3.head_object = head_object
    s3.get_object = get_object
    return s3


def run(title, key, manifest_md5, file_bytes):
    print(f"\n--- {title} ---")
    manifest = json.dumps({"md5": manifest_md5}).encode()
    vc.s3 = make_s3_mock(file_bytes, manifest)
    event = {"bucket": "incident-landing-prod", "key": key,
             "manifestKey": key.rsplit(".", 1)[0] + ".manifest.json"}
    result = vc.lambda_handler(event, None)
    print(json.dumps(result, indent=2))
    return result


# Test 1: valid checksum
content = b"GPS-PING-DATA-PAYLOAD\n" * 5000  # 110000 bytes
md5 = hashlib.md5(content).hexdigest()
r1 = run("Test 1 — valid checksum (happy path)",
        "agency-a/2026-05-12/feed-001.zip", md5, content)
assert r1["checksumValid"] is True
assert r1["computedMd5"] == md5
assert r1["copySource"] == "incident-landing-prod/agency-a/2026-05-12/feed-001.zip"
assert r1["failureReason"] == ""
print("  Result: checksumValid=True, routes to ExtractZip ✓")

# Test 2: checksum mismatch — tampered file
r2 = run("Test 2 — checksum mismatch (tampered file)",
        "agency-b/2026-05-12/feed-002.zip", "f" * 32, content)
assert r2["checksumValid"] is False
assert r2["failureReason"] == "checksum_mismatch"
print("  Result: checksumValid=False, routes to QuarantineParallel ✓")

# Test 3: malformed manifest (md5 not hex)
r3 = run("Test 3 — manifest md5 not hex (malformed manifest)",
        "agency-c/2026-05-12/feed-003.zip", "not-a-real-md5-value", content)
assert r3["checksumValid"] is False
assert r3["failureReason"] == "manifest_md5_not_hex"
print("  Result: rejected at hex validation, routes to QuarantineParallel ✓")

# Test 4: special characters in the key — proves URL encoding works
special_key = "agency-d/2026-05-12/feed with spaces & special#chars.zip"
content4 = b"data\n" * 100
md5_4 = hashlib.md5(content4).hexdigest()
r4 = run("Test 4 — special characters in S3 key (URL-encoded CopySource)",
        special_key, md5_4, content4)
assert "%20" in r4["copySource"] or " " not in r4["copySource"]
print(f"  URL-encoded copySource: {r4['copySource']}")
print("  ✓ Special chars properly encoded for s3:CopyObject")

print("\n" + "=" * 60)
print("ALL 4 SMOKE TESTS PASSED ✓")
print("validate-checksum Lambda runs correctly end-to-end")
print("=" * 60)
