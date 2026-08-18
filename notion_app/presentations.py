"""Reaching aw-app-presentations over the workspace's own HTTP API.

Used by exactly one thing: ``attach_kanban_presentation``, which needs a
presentation rendered to PNG and a share token minted before it has anything
to hand Notion. Everything else in this app talks only to Notion.

Why HTTP and not an import, when both apps are ``tier: inprocess`` and share
this very process: the presentation store is an instance wired up inside
``build_app(store, export_dir)``, not an importable singleton. Reaching for it
directly means reaching into another app's object graph and breaking the first
time it rewires — the same reason ``aw-app-ssh`` talks to ``aw-app-secrets``
over the API instead of importing it.

**Two different addresses, on purpose.** The calls go over *loopback*, because
the server we want is this same process and a render can take a minute — the
published URL goes out through the tunnel edge, which cuts requests at ~30s and
would fail an export that was otherwise working fine. But the share link we
write into the Notion card is for a human to click days later, so *that* one
has to be the published external URL. Getting these two backwards produces a
card whose link only works from inside the container.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API_URL_VAR = "AW_WORKSPACE_API_URL"
API_KEY_VAR = "AW_WORKSPACE_API_KEY"
CONTAINER_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
PREFIX = "/api/apps/presentations/presentations"

# A Playwright render of a big deck is slow, and this is a loopback call with
# no edge in front of it, so it can afford to wait.
EXPORT_TIMEOUT_S = 120.0
DEFAULT_TIMEOUT_S = 30.0


class PresentationsUnavailable(RuntimeError):
    """Could not reach, or was refused by, aw-app-presentations.

    Named separately from NotionError so the caller can say *which* of the two
    services failed — "presentation not found" and "Notion page not shared with
    the integration" send you to completely different places.
    """


def _env_file() -> str:
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(CONTAINER_DIR, ".aw-workspace")
    return os.path.join(home, ".env")


def _from_env_file(name: str) -> str | None:
    try:
        with open(_env_file(), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        return None
    return None


def internal_base_url() -> str:
    """Where to *call* the API from inside this process — loopback."""
    return (os.environ.get("AW_LOCAL_API_URL")
            or f"http://127.0.0.1:{os.environ.get('AW_PORT', '9030')}").rstrip("/")


def public_base_url() -> str:
    """Where a *human* reaches this workspace. Empty when the workspace has no
    published URL — callers must treat that as "no shareable link", not as a
    relative one, because a bare path in a Notion bookmark resolves against
    notion.so."""
    return (os.environ.get(API_URL_VAR) or _from_env_file(API_URL_VAR) or "").rstrip("/")


class PresentationsClient:
    def __init__(self, *, base_url: str | None = None) -> None:
        self._base = (base_url or "").rstrip("/") or None

    def _request(self, method: str, path: str, body: dict | None,
                 timeout: float) -> dict:
        base = self._base or internal_base_url()
        key = os.environ.get(API_KEY_VAR) or _from_env_file(API_KEY_VAR)
        if not key:
            raise PresentationsUnavailable(
                f"{API_KEY_VAR} not found in the environment or {_env_file()} — "
                "cannot authenticate against the workspace API")
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"X-Api-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code == 404:
                raise PresentationsUnavailable(
                    f"presentation not found ({detail})") from None
            if exc.code == 501:
                # The export endpoint's own "Playwright isn't usable here"
                # answer — a real, actionable state, not a transient failure.
                raise PresentationsUnavailable(
                    f"aw-app-presentations cannot render right now: {detail}") from None
            raise PresentationsUnavailable(
                f"aw-app-presentations returned {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise PresentationsUnavailable(
                f"could not reach aw-app-presentations at {base}: {exc.reason}") from None
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            raise PresentationsUnavailable(
                f"aw-app-presentations returned a non-JSON body: {raw[:200]!r}") from None

    def export_png(self, presentation_id: str) -> tuple[str, str]:
        """``(png_path, title)``. The path is server-local, which is fine —
        this app reads it off the same filesystem a moment later."""
        body = self._request("POST", f"{PREFIX}/{presentation_id}/export", {},
                             EXPORT_TIMEOUT_S)
        path = body.get("path")
        if not path:
            raise PresentationsUnavailable(
                f"export returned no path: {json.dumps(body)[:200]}")
        return path, (body.get("title") or presentation_id)

    def share_url(self, presentation_id: str) -> str:
        """Mint a non-expiring share token and build the public link.

        Returns "" when the workspace has no published URL — the caller then
        attaches the image without a bookmark rather than writing a link that
        goes nowhere.
        """
        body = self._request("POST", f"{PREFIX}/{presentation_id}/share",
                             {"expires_in": None}, DEFAULT_TIMEOUT_S)
        token = body.get("token")
        public = public_base_url()
        if not token or not public:
            return ""
        return f"{public}{PREFIX}/{presentation_id}/html?token={token}"


__all__ = ["PresentationsClient", "PresentationsUnavailable",
           "internal_base_url", "public_base_url"]
