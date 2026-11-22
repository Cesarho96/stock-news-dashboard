"""Release registry in SSM Parameter Store: which release is live, which one is the last
validated ("last good") rollback target, blocked releases, and the AI kill switch.

Parameters under REGISTRY_PREFIX (e.g. /stock-news-dashboard/prod):
  release/current       {"releaseId", "functionVersion", "state": baking|validated|rolled-back,
                         "goodEditions", "badEditions", "deployedAt", "source"}
  release/last-good     {"releaseId", "functionVersion", "siteRelease", "validatedAt"}
  release/site-current  {"siteRelease", "deployedAt"}
  release/blocked       {"releases": [git SHAs that must not be deployed again]}
  ai-enabled            "true" | "false"  (kill switch: false = publish data-only editions)

SSM keeps the history of every parameter (aws ssm get-parameter-history), which doubles
as the audit trail of deployments, promotions and rollbacks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Registry:
    def __init__(self, prefix: str, ssm=None):
        if not prefix.startswith("/"):
            raise ValueError("REGISTRY_PREFIX must start with '/'")
        self.prefix = prefix.rstrip("/")
        self._ssm = ssm

    @property
    def ssm(self):
        if self._ssm is None:
            import boto3

            self._ssm = boto3.client("ssm")
        return self._ssm

    # ---- raw access --------------------------------------------------------------------
    def _get(self, name: str) -> str | None:
        try:
            return self.ssm.get_parameter(Name=f"{self.prefix}/{name}")["Parameter"]["Value"]
        except Exception as exc:
            code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code")
            if code == "ParameterNotFound":
                return None
            raise

    def _put(self, name: str, value: str, description: str = "") -> None:
        self.ssm.put_parameter(Name=f"{self.prefix}/{name}", Value=value, Type="String", Overwrite=True,
                               Description=description[:1024] if description else "stock-news-dashboard release registry")

    def _get_json(self, name: str) -> dict | None:
        raw = self._get(name)
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            log.warning("registry parameter %s is not JSON", name)
            return None

    def _put_json(self, name: str, doc: dict, description: str = "") -> None:
        self._put(name, json.dumps(doc, separators=(",", ":"), sort_keys=True), description)

    # ---- kill switch -------------------------------------------------------------------
    def ai_enabled(self) -> bool:
        try:
            value = self._get("ai-enabled")
        except Exception as exc:  # never block an edition because the registry is unreadable
            log.warning("kill switch unreadable (%s); AI stays enabled", exc)
            return True
        return (value or "true").strip().lower() not in ("false", "0", "off", "no")

    def set_ai_enabled(self, enabled: bool, reason: str = "") -> None:
        self._put("ai-enabled", "true" if enabled else "false", f"kill switch: {reason}" if reason else "")

    # ---- releases ----------------------------------------------------------------------
    def current(self) -> dict | None:
        return self._get_json("release/current")

    def last_good(self) -> dict | None:
        return self._get_json("release/last-good")

    def site_current(self) -> dict | None:
        return self._get_json("release/site-current")

    def blocked(self) -> list[str]:
        return list((self._get_json("release/blocked") or {}).get("releases") or [])

    def block(self, release_id: str, reason: str = "") -> None:
        releases = self.blocked()
        if release_id and release_id not in releases:
            releases.append(release_id)
            self._put_json("release/blocked", {"releases": releases[-50:]}, f"blocked {release_id}: {reason}")

    def unblock(self, release_id: str) -> None:
        self._put_json("release/blocked", {"releases": [r for r in self.blocked() if r != release_id]},
                       f"unblocked {release_id}")

    def register(self, release_id: str, function_version: str, source: str) -> dict:
        """A new release went live: start its bake (or keep counters if it is already registered)."""
        cur = self.current() or {}
        if cur.get("releaseId") == release_id and str(cur.get("functionVersion")) == str(function_version):
            return cur
        good = self.last_good() or {}
        state = "validated" if str(good.get("functionVersion")) == str(function_version) else "baking"
        doc = {"releaseId": release_id, "functionVersion": str(function_version), "state": state,
               "goodEditions": 0, "badEditions": 0, "deployedAt": _now(), "source": source}
        self._put_json("release/current", doc, f"live: {release_id} (v{function_version}) via {source}")
        return doc

    def set_site(self, site_release: str) -> None:
        self._put_json("release/site-current", {"siteRelease": site_release, "deployedAt": _now()},
                       f"site {site_release}")

    def mark_good(self, doc: dict, reason: str) -> dict:
        site = (self.site_current() or {}).get("siteRelease")
        good = {"releaseId": doc.get("releaseId"), "functionVersion": str(doc.get("functionVersion")),
                "siteRelease": site, "validatedAt": _now(), "reason": reason}
        self._put_json("release/last-good", good, f"last good: {good['releaseId']} ({reason})")
        cur = dict(doc, state="validated")
        self._put_json("release/current", cur, f"validated: {good['releaseId']}")
        return good

    def record_edition(self, release_id: str, function_version: str | None, status: str, bake_editions: int,
                       counted: bool = True) -> str | None:
        """Bake accounting after each scheduled edition. Returns 'validated' when the release
        just earned last-good status."""
        if not counted or not function_version or function_version == "$LATEST":
            return None
        cur = self.current()
        if cur is None:  # first deployment (no CodeDeploy hook ran): start tracking now
            cur = self.register(release_id, function_version, "first-edition")
        if str(cur.get("functionVersion")) != str(function_version) or cur.get("state") != "baking":
            return None
        if status == "ok":
            cur["goodEditions"] = int(cur.get("goodEditions") or 0) + 1
        else:
            cur["badEditions"] = int(cur.get("badEditions") or 0) + 1
        if status == "ok" and cur["goodEditions"] >= max(1, bake_editions):
            self.mark_good(cur, f"{cur['goodEditions']} good editions in production")
            return "validated"
        self._put_json("release/current", cur, f"baking: {cur['goodEditions']} good / {cur['badEditions']} bad")
        return None

    def mark_rolled_back(self, bad: dict, target: dict, reason: str) -> None:
        self.block(str(bad.get("releaseId") or ""), reason)
        doc = {"releaseId": target.get("releaseId"), "functionVersion": str(target.get("functionVersion")),
               "state": "validated", "goodEditions": 0, "badEditions": 0, "deployedAt": _now(),
               "source": "rollback", "rolledBackFrom": {"releaseId": bad.get("releaseId"),
                                                        "functionVersion": str(bad.get("functionVersion"))},
               "reason": reason[:300]}
        self._put_json("release/current", doc, f"rolled back to {target.get('releaseId')}: {reason}")

    def status(self) -> dict:
        return {"current": self.current(), "lastGood": self.last_good(), "site": self.site_current(),
                "blocked": self.blocked(), "aiEnabled": self.ai_enabled()}
