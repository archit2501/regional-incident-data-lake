"""Secrets Manager rotation Lambda for the Weather API enrichment key.

Implements the four-step rotation contract:

    createSecret  -> issue a new API key against the Weather admin API,
                     store it under staging label AWSPENDING.
    setSecret     -> no-op for this provider — the Weather API tracks keys
                     server-side from issuance time.
    testSecret    -> call the live Weather API with the AWSPENDING key.
                     Abort the rotation on any non-200 response.
    finishSecret  -> promote AWSPENDING -> AWSCURRENT, then revoke the
                     previous key on the admin API.

The new key is verified against the live API **before** the old key is
deprecated, so a bad new key never takes production traffic — AWSCURRENT
does not move until testSecret succeeds.

Environment variables:
    WEATHER_ADMIN_URL          e.g. https://admin.weatherapi.example/v1
    WEATHER_API_URL            e.g. https://api.weatherapi.example
    WEATHER_ADMIN_TOKEN_ARN    ARN of a separate Secrets Manager secret
                               holding the admin bearer token (so this
                               Lambda's own rotation does not deadlock).
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
http = urllib3.PoolManager(
    timeout=urllib3.Timeout(connect=2.0, read=5.0),
    retries=False,
)

WEATHER_ADMIN_URL = os.environ["WEATHER_ADMIN_URL"]
WEATHER_API_URL = os.environ["WEATHER_API_URL"]
ADMIN_TOKEN_ARN = os.environ["WEATHER_ADMIN_TOKEN_ARN"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _admin_token() -> str:
    """Fetch the Weather admin API bearer token from a separate secret."""
    return sm.get_secret_value(SecretId=ADMIN_TOKEN_ARN)["SecretString"]


def _describe(arn: str) -> dict[str, Any]:
    return sm.describe_secret(SecretId=arn)


def _ensure_rotation_ready(meta: dict[str, Any], token: str) -> bool:
    """Validate that rotation is enabled and this token is the pending one.

    Returns False when the token is already AWSCURRENT — that means the
    rotation already finished on a previous attempt and we should no-op.
    """
    if not meta.get("RotationEnabled"):
        raise ValueError(f"Secret {meta['ARN']} is not enabled for rotation")
    versions = meta["VersionIdsToStages"]
    if token not in versions:
        raise ValueError(f"ClientRequestToken {token} not staged on the secret")
    if "AWSCURRENT" in versions[token]:
        LOG.info("Token %s is already AWSCURRENT — idempotent no-op", token)
        return False
    if "AWSPENDING" not in versions[token]:
        raise ValueError(f"Token {token} is not staged as AWSPENDING")
    return True


# ---------------------------------------------------------------------------
# Step 1: createSecret — issue a new key, store under AWSPENDING
# ---------------------------------------------------------------------------
def create_secret(arn: str, token: str) -> None:
    """Issue a new key from the Weather admin API and store under AWSPENDING.

    Idempotent: if AWSPENDING is already populated for this token we return
    immediately, so a Secrets Manager retry of this step is safe.
    """
    try:
        sm.get_secret_value(
            SecretId=arn, VersionId=token, VersionStage="AWSPENDING"
        )
        LOG.info(
            "createSecret: AWSPENDING already exists for token %s; idempotent skip",
            token,
        )
        return
    except sm.exceptions.ResourceNotFoundException:
        pass

    admin_bearer = _admin_token()
    resp = http.request(
        "POST",
        f"{WEATHER_ADMIN_URL}/keys",
        headers={
            "Authorization": f"Bearer {admin_bearer}",
            "Content-Type": "application/json",
        },
        body=json.dumps({"label": f"rotated-{token[:8]}"}).encode(),
    )
    if resp.status >= 300:
        raise RuntimeError(
            f"Weather admin POST /keys returned {resp.status}: {resp.data!r}"
        )

    payload = json.loads(resp.data)
    new_secret = {
        "api_key": payload["api_key"],
        "key_id": payload["key_id"],
        "issued_at": payload["issued_at"],
    }

    sm.put_secret_value(
        SecretId=arn,
        ClientRequestToken=token,
        SecretString=json.dumps(new_secret),
        VersionStages=["AWSPENDING"],
    )
    LOG.info("createSecret: stored new AWSPENDING key id=%s", new_secret["key_id"])


# ---------------------------------------------------------------------------
# Step 2: setSecret — no-op for this provider
# ---------------------------------------------------------------------------
def set_secret(arn: str, token: str) -> None:
    """The Weather API knows the key from createSecret; nothing to push."""
    LOG.info("setSecret: no-op (Weather API tracks keys at issuance time)")


# ---------------------------------------------------------------------------
# Step 3: testSecret — verify the AWSPENDING key against the live API
# ---------------------------------------------------------------------------
def test_secret(arn: str, token: str) -> None:
    """Call the Weather API with the AWSPENDING key. Abort on any non-200.

    This runs BEFORE finishSecret moves AWSCURRENT, so a key that fails
    verification never takes production traffic.
    """
    pending = json.loads(
        sm.get_secret_value(
            SecretId=arn, VersionId=token, VersionStage="AWSPENDING"
        )["SecretString"]
    )
    resp = http.request(
        "GET",
        f"{WEATHER_API_URL}/v1/current?lat=0&lon=0",
        headers={"X-Api-Key": pending["api_key"]},
    )
    if resp.status != 200:
        raise RuntimeError(
            "testSecret: AWSPENDING key did not authenticate; "
            f"status={resp.status} body={resp.data!r}"
        )
    LOG.info(
        "testSecret: AWSPENDING key id=%s verified against live API",
        pending["key_id"],
    )


# ---------------------------------------------------------------------------
# Step 4: finishSecret — promote AWSCURRENT to the new version, revoke old
# ---------------------------------------------------------------------------
def finish_secret(arn: str, token: str) -> None:
    """Move AWSCURRENT from the previous version to this one, then revoke
    the previous key on the admin API.

    If the revoke call fails, we log and continue — the rotation already
    succeeded and the old key being briefly live is acceptable; it will be
    revoked on the next maintenance pass.
    """
    meta = _describe(arn)

    current_version: str | None = None
    for version_id, stages in meta["VersionIdsToStages"].items():
        if "AWSCURRENT" in stages:
            current_version = version_id
            break

    if current_version == token:
        LOG.info(
            "finishSecret: token %s is already AWSCURRENT; nothing to do", token
        )
        return

    sm.update_secret_version_stage(
        SecretId=arn,
        VersionStage="AWSCURRENT",
        MoveToVersionId=token,
        RemoveFromVersionId=current_version,
    )
    LOG.info(
        "finishSecret: promoted token %s to AWSCURRENT (was %s)",
        token,
        current_version,
    )

    if current_version is None:
        return

    try:
        old = json.loads(
            sm.get_secret_value(SecretId=arn, VersionId=current_version)[
                "SecretString"
            ]
        )
        admin_bearer = _admin_token()
        resp = http.request(
            "DELETE",
            f"{WEATHER_ADMIN_URL}/keys/{old['key_id']}",
            headers={"Authorization": f"Bearer {admin_bearer}"},
        )
        if resp.status >= 300 and resp.status != 404:
            LOG.warning(
                "finishSecret: old key revocation returned %s: %s",
                resp.status,
                resp.data,
            )
        else:
            LOG.info(
                "finishSecret: revoked previous key id=%s on admin API",
                old["key_id"],
            )
    except Exception:  # noqa: BLE001 — rotation already succeeded; do not unwind
        LOG.exception(
            "finishSecret: failed to revoke old key (continuing; rotation already succeeded)"
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
STEPS = {
    "createSecret": create_secret,
    "setSecret": set_secret,
    "testSecret": test_secret,
    "finishSecret": finish_secret,
}


def lambda_handler(event: dict[str, Any], _context: Any) -> None:
    LOG.info("rotation event=%s", json.dumps(event))
    arn = event["SecretId"]
    token = event["ClientRequestToken"]
    step = event["Step"]

    if step not in STEPS:
        raise ValueError(f"Unknown rotation step: {step}")

    if not _ensure_rotation_ready(_describe(arn), token):
        return  # token already current; idempotent no-op

    STEPS[step](arn, token)
