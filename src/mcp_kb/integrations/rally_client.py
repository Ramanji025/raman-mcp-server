"""Rally (CA Agile Central) REST API client.

Fetches User Stories (HierarchicalRequirement), Tasks, Defects and
Acceptance Criteria so they can be mapped to microservice code.

Configuration (environment variables or .env):
    RALLY_API_KEY        — Rally API key (ZSESSIONID header)
    RALLY_BASE_URL       — defaults to https://rally1.rallydev.com/slm/webservice/v2.0
    RALLY_WORKSPACE_REF  — optional workspace OID, e.g. /workspace/12345678
    RALLY_PROJECT_REF    — optional project OID filter
"""
from __future__ import annotations

import re
from urllib.parse import urlencode

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

try:
    import json as _json
    import ssl as _ssl
    import urllib.request
    _STDLIB_AVAILABLE = True
except ImportError:
    _STDLIB_AVAILABLE = False


_DEFAULT_BASE = "https://rally1.rallydev.com/slm/webservice/v2.0"

# Fields fetched for a User Story.
_STORY_FIELDS = (
    "FormattedID,Name,Description,Notes,ScheduleState,Priority,PlanEstimate,"
    "Owner,Tags,Iteration,Release,Tasks,Children,TestCases,Defects,"
    "AcceptanceCriteria,c_AcceptanceCriteria"
)


class RallyClient:
    """Minimal Rally REST v2 client — no SDK dependency."""

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE,
                 workspace_ref: str | None = None,
                 project_ref: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.workspace_ref = workspace_ref
        self.project_ref = project_ref

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get_story(self, story_id: str) -> dict:
        """Fetch a single User Story by FormattedID (e.g. 'US1234')."""
        story_id = story_id.strip().upper()
        params: dict = {
            "query": f'(FormattedID = "{story_id}")',
            "fetch": _STORY_FIELDS,
            "pagesize": "1",
        }
        if self.workspace_ref:
            params["workspace"] = self.workspace_ref
        if self.project_ref:
            params["project"] = self.project_ref

        resp = self._get("hierarchicalrequirement", params)
        results = resp.get("QueryResult", {}).get("Results", [])
        if not results:
            return {}
        raw = results[0]
        return self._normalise_story(raw)

    def search_stories(self, query_text: str, limit: int = 10) -> list[dict]:
        """Full-text search stories by name/description keyword."""
        safe = query_text.replace('"', "'")
        params: dict = {
            "query": f'(Name contains "{safe}")',
            "fetch": _STORY_FIELDS,
            "pagesize": str(limit),
            "order": "LastUpdateDate desc",
        }
        if self.workspace_ref:
            params["workspace"] = self.workspace_ref
        resp = self._get("hierarchicalrequirement", params)
        return [self._normalise_story(r)
                for r in resp.get("QueryResult", {}).get("Results", [])]

    def get_iteration_stories(self, iteration_name: str, limit: int = 50) -> list[dict]:
        """Fetch all stories in a named iteration/sprint."""
        safe = iteration_name.replace('"', "'")
        params: dict = {
            "query": f'(Iteration.Name = "{safe}")',
            "fetch": _STORY_FIELDS,
            "pagesize": str(limit),
        }
        if self.workspace_ref:
            params["workspace"] = self.workspace_ref
        resp = self._get("hierarchicalrequirement", params)
        return [self._normalise_story(r)
                for r in resp.get("QueryResult", {}).get("Results", [])]

    # ------------------------------------------------------------------ #
    # Normalisation
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalise_story(raw: dict) -> dict:
        """Flatten Rally's nested structure into a flat story dict."""
        def _ref_name(field) -> str | None:
            if isinstance(field, dict):
                return field.get("_refObjectName") or field.get("Name")
            return None

        ac_text = raw.get("c_AcceptanceCriteria") or raw.get("AcceptanceCriteria") or ""
        if isinstance(ac_text, dict):
            ac_text = ac_text.get("_value", "")

        return {
            "id": raw.get("FormattedID", ""),
            "name": raw.get("Name", ""),
            "description": _strip_html(raw.get("Description") or ""),
            "notes": _strip_html(raw.get("Notes") or ""),
            "acceptance_criteria": _strip_html(str(ac_text)),
            "state": raw.get("ScheduleState", ""),
            "priority": raw.get("Priority", ""),
            "estimate": raw.get("PlanEstimate"),
            "owner": _ref_name(raw.get("Owner")),
            "iteration": _ref_name(raw.get("Iteration")),
            "release": _ref_name(raw.get("Release")),
            "tags": [t.get("Name", "") for t in (raw.get("Tags", {}) or {}).get("_tagsNameArray", [])],
            "ref": raw.get("_ref", ""),
        }

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _get(self, resource: str, params: dict) -> dict:
        url = f"{self.base_url}/{resource}"
        qs = urlencode(params)
        full_url = f"{url}?{qs}"
        headers = {"ZSESSIONID": self.api_key, "Accept": "application/json"}

        if _HTTPX_AVAILABLE:
            with httpx.Client(verify=False, timeout=30) as client:
                resp = client.get(full_url, headers=headers)
                resp.raise_for_status()
                return resp.json()

        # Fallback to stdlib urllib.
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        req = urllib.request.Request(full_url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return _json.loads(r.read().decode("utf-8"))


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_rally_client_from_settings(settings) -> RallyClient | None:
    """Build a RallyClient from Settings, return None if not configured."""
    api_key = getattr(settings, "rally_api_key", None)
    if not api_key:
        return None
    return RallyClient(
        api_key=api_key,
        base_url=getattr(settings, "rally_base_url", _DEFAULT_BASE),
        workspace_ref=getattr(settings, "rally_workspace_ref", None),
        project_ref=getattr(settings, "rally_project_ref", None),
    )
