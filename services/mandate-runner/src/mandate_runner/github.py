"""GitHub REST calls the runner makes, authenticated as the mandate-runner App."""

from __future__ import annotations

import calendar
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import jwt

from .models import PullRequest, Review
from .secrets import AppCredentials

API = "https://api.github.com"
SKEW_TOLERANCE = 30.0  # seconds of clock difference before the App JWT is re-timed
MAX_PR_FILES = 3000  # GitHub's ceiling for the PR files listing


class DiffTooLarge(RuntimeError):
    pass


class GitHub(Protocol):
    def list_open_prs(self, repo: str) -> list[PullRequest]: ...
    def list_reviews(self, repo: str, number: int) -> list[Review]: ...
    def list_changed_paths(self, repo: str, number: int) -> list[str]: ...
    def merge_base(self, repo: str, base_sha: str, head_sha: str) -> str: ...
    def file_at(self, repo: str, path: str, ref: str) -> bytes | None: ...
    def post_status(self, repo: str, sha: str, state: str, description: str, target_url: str | None) -> None: ...


class GitHubApp:
    def __init__(self, creds: AppCredentials, context: str, *, api: str = API, client: httpx.Client | None = None):
        self._creds = creds
        self._context = context
        self._api = api.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0, headers={"User-Agent": "mandate-runner"})
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires = 0.0
        # GitHub's clock minus ours. Docker Desktop's VM clock can lag the host after
        # the laptop sleeps, which makes GitHub reject the App JWT as not yet valid
        # or expired.
        self._skew = 0.0

    def __repr__(self) -> str:
        return f"GitHubApp(app_id={self._creds.app_id})"

    # --- auth -------------------------------------------------------------

    def _now(self) -> float:
        return time.time() + self._skew

    def _app_jwt(self) -> str:
        now = int(self._now())
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(self._creds.app_id)}
        return jwt.encode(payload, self._creds.private_key, algorithm="RS256")

    def _learn_skew(self, resp: httpx.Response) -> bool:
        """Takes GitHub's clock from the Date header. True if ours was off enough to matter."""
        date = resp.headers.get("Date")
        if not date:
            return False
        try:
            skew = parsedate_to_datetime(date).timestamp() - time.time()
        except (TypeError, ValueError):
            return False
        if abs(skew - self._skew) < SKEW_TOLERANCE:
            return False
        self._skew = skew
        return True

    def _app_call(self, method: str, url: str) -> httpx.Response:
        """A call authenticated by the App JWT, retried once with GitHub's clock on a 401."""
        resp = self._client.request(method, url, headers=self._headers(f"Bearer {self._app_jwt()}"))
        if resp.status_code == 401 and self._learn_skew(resp):
            resp = self._client.request(method, url, headers=self._headers(f"Bearer {self._app_jwt()}"))
        return resp

    def installation_token(self) -> str:
        with self._lock:
            if self._token is None or self._now() > self._token_expires - 300:
                resp = self._app_call(
                    "POST", f"{self._api}/app/installations/{self._creds.installation_id}/access_tokens"
                )
                resp.raise_for_status()
                body = resp.json()
                token: str = body["token"]
                self._token = token
                expires = body.get("expires_at", "")
                self._token_expires = (
                    calendar.timegm(time.strptime(expires, "%Y-%m-%dT%H:%M:%SZ")) if expires else self._now() + 3000
                )
                return token
            return self._token

    def _drop_token(self, token: str) -> None:
        """Forgets a token GitHub refused, unless another thread already replaced it."""
        with self._lock:
            if self._token == token:
                self._token = None

    @staticmethod
    def _headers(auth: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {"Authorization": auth, "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}

    def _request(
        self, method: str, path: str, *, accept: str = "application/vnd.github+json", **kw: Any
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._api}{path}"
        token = self.installation_token()
        resp = self._client.request(method, url, headers=self._headers(f"token {token}", accept), **kw)
        if resp.status_code == 401:
            # The cached token can be stale (revoked, or expired by GitHub's clock while
            # ours lagged). Mint a fresh one and try once more.
            self._drop_token(token)
            token = self.installation_token()
            resp = self._client.request(method, url, headers=self._headers(f"token {token}", accept), **kw)
        return resp

    def _get_json(self, path: str, **params: Any) -> Any:
        resp = self._request("GET", path, params=params or None)
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, path: str, **params: Any) -> list[Any]:
        items: list[Any] = []
        url: str | None = path
        query: dict[str, Any] | None = {"per_page": 100, **params}
        while url:
            resp = self._request("GET", url, params=query)
            resp.raise_for_status()
            items.extend(resp.json())
            url = resp.links.get("next", {}).get("url")
            query = None  # the next link carries the query
        return items

    # --- identity checks ---------------------------------------------------

    def authenticated_app_id(self) -> int:
        resp = self._app_call("GET", f"{self._api}/app")
        resp.raise_for_status()
        return int(resp.json()["id"])

    def app_id_for_slug(self, slug: str) -> int:
        return int(self._get_json(f"/apps/{quote(slug)}")["id"])

    # --- reads -------------------------------------------------------------

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        out = []
        for p in self._paginate(f"/repos/{repo}/pulls", state="open"):
            head_repo = (p["head"].get("repo") or {}).get("full_name", "")
            out.append(
                PullRequest(
                    repo=repo,
                    number=p["number"],
                    head_sha=p["head"]["sha"],
                    head_ref=p["head"]["ref"],
                    head_repo=head_repo,
                    base_ref=p["base"]["ref"],
                    base_sha=p["base"]["sha"],
                    draft=bool(p.get("draft")),
                    labels=frozenset(label["name"] for label in p.get("labels", [])),
                )
            )
        return out

    def list_reviews(self, repo: str, number: int) -> list[Review]:
        return [
            Review(
                id=r["id"],
                login=(r.get("user") or {}).get("login", ""),
                state=r.get("state", ""),
                commit_id=r.get("commit_id") or "",
                body=r.get("body") or "",
            )
            for r in self._paginate(f"/repos/{repo}/pulls/{number}/reviews")
        ]

    def list_changed_paths(self, repo: str, number: int) -> list[str]:
        files = self._paginate(f"/repos/{repo}/pulls/{number}/files")
        if len(files) >= MAX_PR_FILES:
            raise DiffTooLarge(f"{repo}#{number} changes {MAX_PR_FILES}+ files")
        paths: list[str] = []
        for f in files:
            paths.append(f["filename"])
            if f.get("previous_filename"):
                paths.append(f["previous_filename"])  # moving a file away touches it too
        return paths

    def merge_base(self, repo: str, base_sha: str, head_sha: str) -> str:
        body = self._get_json(f"/repos/{repo}/compare/{base_sha}...{head_sha}", per_page=1)
        return body["merge_base_commit"]["sha"]

    def file_at(self, repo: str, path: str, ref: str) -> bytes | None:
        resp = self._request(
            "GET",
            f"/repos/{repo}/contents/{quote(path)}",
            params={"ref": ref},
            accept="application/vnd.github.raw+json",
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    # --- writes ------------------------------------------------------------

    def post_status(self, repo: str, sha: str, state: str, description: str, target_url: str | None) -> None:
        body: dict[str, str] = {"state": state, "context": self._context, "description": description}
        if target_url:
            body["target_url"] = target_url
        resp = self._request("POST", f"/repos/{repo}/statuses/{sha}", json=body)
        resp.raise_for_status()
