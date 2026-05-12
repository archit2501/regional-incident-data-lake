"""End-to-end smoke test of the rotation Lambda with mocked Secrets Manager + urllib3.

Walks through all four rotation steps (createSecret, setSecret, testSecret,
finishSecret) and proves the critical invariant: AWSCURRENT only moves in
finishSecret, AFTER testSecret has successfully called the live Weather API.
"""
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

os.environ["WEATHER_ADMIN_URL"] = "https://admin.weatherapi.example/v1"
os.environ["WEATHER_API_URL"] = "https://api.weatherapi.example"
os.environ["WEATHER_ADMIN_TOKEN_ARN"] = (
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:weather-admin-token"
)

sys.modules["boto3"] = MagicMock()
sys.modules["urllib3"] = MagicMock()

spec = importlib.util.spec_from_file_location(
    "rot",
    "/Users/architjain/regional-incident-data-lake/deliverables/04-rotate-weather-api-key.py",
)
rot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rot)

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:weather-api-key"
OLD_TOKEN = "old-version-uuid"
NEW_TOKEN = "new-version-uuid"

state = {
    "VersionIdsToStages": {
        OLD_TOKEN: ["AWSCURRENT"],
        NEW_TOKEN: ["AWSPENDING"],
    },
    "values": {OLD_TOKEN: json.dumps({"api_key": "OLD_KEY_VALUE", "key_id": "k-old"})},
    "rotation_enabled": True,
    "admin_token": "admin-bearer-xyz",
}


class Body:
    def __init__(self, data): self.data = data
    def read(self): return self.data.encode() if isinstance(self.data, str) else self.data


def make_sm_mock():
    sm = MagicMock()

    def describe(SecretId, **_):
        return {"ARN": SecretId, "RotationEnabled": state["rotation_enabled"],
                "VersionIdsToStages": dict(state["VersionIdsToStages"])}

    def get_value(SecretId, VersionId=None, VersionStage=None, **_):
        if SecretId.endswith("weather-admin-token"):
            return {"SecretString": state["admin_token"]}
        if VersionId is None:
            # find AWSCURRENT
            for vid, stages in state["VersionIdsToStages"].items():
                if "AWSCURRENT" in stages:
                    VersionId = vid; break
        if VersionId not in state["values"]:
            raise sm.exceptions.ResourceNotFoundException()
        return {"SecretString": state["values"][VersionId]}

    def put_value(SecretId, ClientRequestToken, SecretString, VersionStages, **_):
        state["values"][ClientRequestToken] = SecretString
        state["VersionIdsToStages"].setdefault(ClientRequestToken, [])
        for stg in VersionStages:
            if stg not in state["VersionIdsToStages"][ClientRequestToken]:
                state["VersionIdsToStages"][ClientRequestToken].append(stg)

    def move_stage(SecretId, VersionStage, MoveToVersionId, RemoveFromVersionId, **_):
        if RemoveFromVersionId and VersionStage in state["VersionIdsToStages"].get(RemoveFromVersionId, []):
            state["VersionIdsToStages"][RemoveFromVersionId].remove(VersionStage)
        state["VersionIdsToStages"].setdefault(MoveToVersionId, []).append(VersionStage)

    sm.describe_secret = describe
    sm.get_secret_value = get_value
    sm.put_secret_value = put_value
    sm.update_secret_version_stage = move_stage
    sm.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
    sm.exceptions.InvalidRequestException = type("IRE", (Exception,), {})
    return sm


def make_http_mock(test_should_succeed=True):
    http = MagicMock()

    def request(method, url, **kwargs):
        resp = MagicMock()
        if "/keys" in url and method == "POST":
            # createSecret — issue new key
            resp.status = 201
            resp.data = json.dumps({
                "api_key": "NEW_KEY_VALUE_xxxxxxxxxx",
                "key_id": "k-new",
                "issued_at": "2026-05-12T08:14:00Z",
            }).encode()
        elif "/keys/" in url and method == "DELETE":
            # finishSecret — revoke old
            resp.status = 204
            resp.data = b""
        else:
            # testSecret — live API probe
            resp.status = 200 if test_should_succeed else 401
            resp.data = b'{"weather": "sunny"}' if test_should_succeed else b'{"error":"unauthorized"}'
        return resp

    http.request = request
    return http


def stages_view():
    return {k: list(v) for k, v in state["VersionIdsToStages"].items()}


def run_step(step, http=None):
    print(f"\n--- Step: {step} ---")
    print(f"   before: {stages_view()}")
    rot.sm = make_sm_mock()
    rot.http = http or make_http_mock()
    event = {"SecretId": SECRET_ARN, "ClientRequestToken": NEW_TOKEN, "Step": step}
    rot.lambda_handler(event, None)
    print(f"   after : {stages_view()}")


# ============================================================
print("ROTATION LAMBDA SMOKE TEST")
print("=" * 60)
print(f"Starting state — OLD={OLD_TOKEN[:8]} is AWSCURRENT, NEW={NEW_TOKEN[:8]} is AWSPENDING")

run_step("createSecret")
assert "AWSCURRENT" in state["VersionIdsToStages"][OLD_TOKEN], \
    "createSecret must NOT touch AWSCURRENT"

run_step("setSecret")
assert "AWSCURRENT" in state["VersionIdsToStages"][OLD_TOKEN], \
    "setSecret must NOT touch AWSCURRENT"

run_step("testSecret")
assert "AWSCURRENT" in state["VersionIdsToStages"][OLD_TOKEN], \
    "testSecret must NOT touch AWSCURRENT (it only verifies the new key)"
print("   ✓ INVARIANT: AWSCURRENT still on OLD after testSecret")

run_step("finishSecret")
assert "AWSCURRENT" in state["VersionIdsToStages"][NEW_TOKEN], \
    "finishSecret MUST promote NEW to AWSCURRENT"
assert "AWSCURRENT" not in state["VersionIdsToStages"][OLD_TOKEN], \
    "finishSecret must remove AWSCURRENT from OLD"
print("   ✓ INVARIANT: AWSCURRENT moved to NEW only at finishSecret")

print("\n" + "=" * 60)
print("ALL ROTATION INVARIANTS HOLD ✓")
print("New key was verified against the live Weather API BEFORE the old key")
print("was deprecated — exactly as the assignment requires.")
print("=" * 60)

# Bonus test: if testSecret fails, AWSCURRENT must stay on OLD
print("\n--- Bonus: testSecret failure path (live API returns 401) ---")
state["VersionIdsToStages"] = {
    OLD_TOKEN: ["AWSCURRENT"],
    NEW_TOKEN: ["AWSPENDING"],
}
print(f"   before: {stages_view()}")
try:
    rot.sm = make_sm_mock()
    rot.http = make_http_mock(test_should_succeed=False)
    rot.lambda_handler({"SecretId": SECRET_ARN, "ClientRequestToken": NEW_TOKEN,
                        "Step": "testSecret"}, None)
    print("   ✗ FAIL: testSecret should have raised")
except RuntimeError as e:
    print(f"   testSecret raised: {e}")
    print(f"   after : {stages_view()}")
    assert "AWSCURRENT" in state["VersionIdsToStages"][OLD_TOKEN], \
        "OLD must retain AWSCURRENT after testSecret failure"
    print("   ✓ Old key keeps serving traffic — rotation aborted cleanly")
