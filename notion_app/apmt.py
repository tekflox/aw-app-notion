"""Keep agents-platform-multitenant's copy of this workspace's Notion token
in step with the copy this app owns (Kanban
``architecture:notion-token-per-tenant-ap-mt-step1``).

**This app stays the source of truth.** ``ctx.secrets`` is where the token
lives; AP-MT holds a derived copy so the control plane can reach
api.notion.com for this tenant while this workspace is offline. Everything
here is one-way: save pushes, logout deletes, and a periodic reconcile
re-pushes when the two fingerprints disagree. Nothing ever reads a token
back from AP-MT — a route that could do that would make the derived copy a
second authority, which is the one property this design must not have.

Why the call goes through aw-app-agents-platform-runners instead of straight
to AP-MT: that app owns the AP-MT address and identity token
(``agents_platform_base`` / ``agents_platform_token`` in its own config), and
an app cannot read another app's config — see ``src/apps/base.py``'s
``AppContext``, which grants no such facade. So each app keeps the credential
it owns and the hop between them is loopback HTTP authenticated with the
workspace's own ``X-Api-Key`` (``src/apps/runtime.py::_default_verify_http``
accepts it on any installed app's routes — the same door aw-workspace core
itself uses to call that app's ``/register-observability``).

Fail-open on push, fail-CLOSED on delete. A failed push is repaired by the
next reconcile within ~6 minutes, so it must not block a token save. A failed
delete has no such safety net in the direction that matters: reporting
"logged out" while a working token is still live in the control plane is the
failure this whole design would be judged on, so ``POST /logout`` surfaces it
and keeps the local token rather than reporting a success it did not achieve.
"""
from __future__ import annotations

import hashlib
import logging
import os

import httpx

log = logging.getLogger("aw_apps.notion.apmt")

RUNNERS_APP_ID = "agents-platform-runners"
API_KEY_VAR = "AW_WORKSPACE_API_KEY"
CONTAINER_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
TIMEOUT_S = 20.0


class ApmtSyncError(RuntimeError):
    """The runners app (and through it AP-MT) could not be reached or refused
    the call. Carries a human-readable reason — it ends up in a route's
    response, not just a log line."""


class ApmtNotConfigured(ApmtSyncError):
    """There is no derived copy to keep in step, and there cannot be one from
    here: aw-app-agents-platform-runners isn't installed, or it has no
    ``agents_platform_token``, or this workspace has no API key to call it
    with.

    Separate from a plain :class:`ApmtSyncError` because ``/logout`` has to
    tell "I could not delete the remote copy" (fail the logout — a live token
    would be left behind) from "there is no remote copy" (proceed — the
    common case in a workspace that never pushed one, and in every test).
    Collapsing the two would make logout permanently impossible on any
    workspace that isn't wired to AP-MT.
    """


def token_fingerprint(token: str | None) -> str:
    """sha256 of the token — the only thing about it that crosses the wire on
    a reconcile cycle.

    MUST stay byte-identical to agents-platform-multitenant's
    ``core/secret_crypto.py::fingerprint``; a divergence doesn't fail loudly,
    it just re-pushes the token every 6 minutes forever. Empty/absent token
    hashes to the empty string so "no token here" and "no token there" compare
    equal without either side special-casing it.
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _from_env_file(name: str) -> str | None:
    # Same ~8 lines every module that needs a workspace env var re-declares
    # (aw-app-agents-platform-runners' observability_push.py says the same) —
    # the server mirrors these into a 0600 .env at boot, and a Tier-1 app
    # reloaded after that boot may not have them in its own environment.
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(CONTAINER_DIR, ".aw-workspace")
    try:
        with open(os.path.join(home, ".env"), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        return None
    return None


def _api_key() -> str:
    key = os.environ.get(API_KEY_VAR) or _from_env_file(API_KEY_VAR)
    if not key:
        raise ApmtNotConfigured(
            f"{API_KEY_VAR} is not set — cannot reach {RUNNERS_APP_ID} over loopback")
    return key


def _runners_url(path: str) -> str:
    """Loopback only, never the published URL: this app runs INSIDE the
    workspace server, so the published URL would leave for the tunnel edge
    and come back — see kanban_dispatch.board_base_url's docstring for the
    30s cut that makes that the wrong answer even when it works."""
    base = os.environ.get("AW_LOCAL_API_URL") or \
        f"http://127.0.0.1:{os.environ.get('AW_PORT', '9030')}"
    return f"{base.rstrip('/')}/api/apps/{RUNNERS_APP_ID}{path}"


def _call(method: str, path: str, body: dict | None = None) -> dict:
    url = _runners_url(path)
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.request(method, url, json=body,
                                  headers={"X-Api-Key": _api_key()})
    except httpx.HTTPError as exc:
        raise ApmtSyncError(f"{RUNNERS_APP_ID} unreachable at {url}: {exc}") from exc
    if resp.status_code == 404:
        # The runners app isn't installed (or doesn't have these routes yet).
        # A workspace with no agents-platform to push to is a legitimate
        # configuration, not an error — say so precisely instead of
        # reporting a generic failure the operator would go hunting for.
        raise ApmtNotConfigured(f"{RUNNERS_APP_ID} is not installed (404 at {url})")
    if resp.status_code == 409:
        # The relay exists but this workspace has no agents_platform_token,
        # so nothing was ever pushed and nothing can be. See routes.py in
        # aw-app-agents-platform-runners for where this status comes from.
        raise ApmtNotConfigured(
            f"{RUNNERS_APP_ID} has no agents-platform configured: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise ApmtSyncError(
            f"{RUNNERS_APP_ID} refused the call ({resp.status_code}): {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ApmtSyncError(f"{RUNNERS_APP_ID} returned a non-JSON body") from exc


def push_token(token: str) -> dict:
    return _call("POST", "/notion-token", {"token": token})


def delete_token() -> dict:
    return _call("DELETE", "/notion-token")


def remote_state() -> dict:
    """``{"configured": bool, "token_fingerprint": str}`` as AP-MT sees it."""
    return _call("GET", "/notion-token/state")


def reconcile(local_token: str | None) -> dict:
    """Make AP-MT's copy match this app's, and report what that took.

    Called on the 360s skills-sync reconcile tick (see the runners app's
    plugin.py) and reachable by hand as ``POST /apmt/sync``. Three outcomes,
    all of them normal:

    * fingerprints agree            → nothing sent, ``changed: False``
    * local token, remote differs   → push (a rotation, or a push that failed
      when it was first attempted)
    * no local token, remote has one → delete (repairs a logout whose delete
      leg failed after the local secret was already gone)

    Never raises: this runs in a watchdog, where an exception is a stack
    trace every six minutes that nobody reads.
    """
    local_fp = token_fingerprint(local_token)
    try:
        state = remote_state()
    except ApmtSyncError as exc:
        return {"reconciled": False, "changed": False, "reason": str(exc)}

    remote_fp = state.get("token_fingerprint") or ""
    if local_fp == remote_fp:
        return {"reconciled": True, "changed": False, "reason": "fingerprints match"}

    try:
        if local_token:
            push_token(local_token)
            action = "pushed"
        else:
            delete_token()
            action = "deleted"
    except ApmtSyncError as exc:
        return {"reconciled": False, "changed": False, "reason": str(exc)}
    log.info("apmt: notion token %s (fingerprint drift repaired)", action)
    return {"reconciled": True, "changed": True, "action": action}
