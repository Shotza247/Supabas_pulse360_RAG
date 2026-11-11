"""Minimal Box REST client used by the document sync.

Authentication is either Client Credentials Grant (production: BOX_CLIENT_ID,
BOX_CLIENT_SECRET, BOX_ENTERPRISE_ID) or a 60-minute developer token (first manual
test only). Nothing here logs tokens, secrets or shared links.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import requests

from faq_agent.supabase_store import MAX_BYTES

logger = logging.getLogger("faq_agent")

API = "https://api.box.com/2.0"
TOKEN_URL = "https://api.box.com/oauth2/token"
ITEM_FIELDS = "id,type,name,size,sha1,etag,modified_at"
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_ATTEMPTS = 3


class BoxError(RuntimeError):
    """A Box API failure. The message carries the status and path, never credentials."""


class BoxFileTooLarge(BoxError):
    pass


def missing_box_settings(settings, *, folder_id: str = "", shared_link: str = "") -> list[str]:
    """Return the env var names still needed before a sync can authenticate and pick a source."""
    missing = []
    if not settings.box_developer_token:
        missing += [
            name
            for name, value in (
                ("BOX_CLIENT_ID", settings.box_client_id),
                ("BOX_CLIENT_SECRET", settings.box_client_secret),
                ("BOX_ENTERPRISE_ID", settings.box_enterprise_id),
            )
            if not value
        ]
    if not (folder_id or shared_link):
        missing.append("BOX_FOLDER_ID or BOX_SHARED_LINK")
    return missing


class BoxClient:
    def __init__(self, settings, session=None, max_bytes: int = MAX_BYTES):
        self.settings = settings
        self.session = session or requests.Session()
        self.max_bytes = max_bytes
        self.timeout = settings.request_timeout
        self._token = ""
        self._expires = 0.0

    # ------------------------------------------------------------------ auth

    def _access_token(self) -> str:
        if self.settings.box_developer_token:
            return self.settings.box_developer_token
        if self._token and time.monotonic() < self._expires - 60:
            return self._token
        response = self.session.request(
            "POST",
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.box_client_id,
                "client_secret": self.settings.box_client_secret,
                "box_subject_type": "enterprise",
                "box_subject_id": self.settings.box_enterprise_id,
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise BoxError(
                f"Box token request failed with status {response.status_code}; check "
                "BOX_CLIENT_ID/BOX_CLIENT_SECRET/BOX_ENTERPRISE_ID and that an admin "
                "authorized the app"
            )
        body = response.json()
        self._token = body["access_token"]
        self._expires = time.monotonic() + int(body.get("expires_in", 3600))
        return self._token

    def _headers(self, shared_link: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        if shared_link:
            headers["BoxApi"] = f"shared_link={shared_link}"
        return headers

    # --------------------------------------------------------------- transport

    def _request(
        self, url, *, params=None, shared_link=None, stream=False, _reauth=True
    ) -> requests.Response:
        for attempt in range(1, _ATTEMPTS + 1):
            response = self.session.request(
                "GET",
                url,
                headers=self._headers(shared_link),
                params=params,
                stream=stream,
                timeout=self.timeout,
            )
            status = response.status_code
            if status == 401 and _reauth and not self.settings.box_developer_token:
                self._token = ""  # expired mid-run: fetch a fresh token once
                response.close()
                return self._request(
                    url, params=params, shared_link=shared_link, stream=stream, _reauth=False
                )
            # 202 on /content means "file not ready yet, retry after a delay".
            if (status in _RETRY_STATUSES or status == 202) and attempt < _ATTEMPTS:
                try:
                    delay = min(float(response.headers.get("Retry-After", 2**attempt)), 30.0)
                except (TypeError, ValueError):
                    delay = float(2**attempt)
                response.close()
                logger.warning("box_retrying status=%d attempt=%d/%d", status, attempt, _ATTEMPTS)
                time.sleep(delay)
                continue
            if status >= 400 or status == 202:
                response.close()
                raise BoxError(f"Box API returned {status} for {url.removeprefix(API)}")
            return response
        raise BoxError("Box request retries exhausted")  # pragma: no cover

    # -------------------------------------------------------------------- API

    def resolve_shared_link(self, url: str) -> dict:
        """Resolve a Box shared link to its file/folder item (id, type, name, ...)."""
        return self._request(
            f"{API}/shared_items", params={"fields": ITEM_FIELDS}, shared_link=url
        ).json()

    def iter_files(self, folder_id: str, shared_link: str | None = None) -> Iterator[dict]:
        """Yield every file under a folder, recursing into subfolders."""
        pending, visited = [str(folder_id)], set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            marker = None
            while True:
                params = {"limit": 1000, "usemarker": "true", "fields": ITEM_FIELDS}
                if marker:
                    params["marker"] = marker
                page = self._request(
                    f"{API}/folders/{current}/items", params=params, shared_link=shared_link
                ).json()
                for entry in page.get("entries", []):
                    if entry.get("type") == "folder":
                        pending.append(str(entry["id"]))
                    elif entry.get("type") == "file":
                        yield entry
                marker = page.get("next_marker")
                if not marker:
                    break

    def download(self, file_id: str, shared_link: str | None = None) -> bytes:
        """Download a file, aborting as soon as it exceeds the upload size limit."""
        response = self._request(
            f"{API}/files/{file_id}/content", shared_link=shared_link, stream=True
        )
        try:
            buffer = bytearray()
            for chunk in response.iter_content(65536):
                buffer.extend(chunk)
                if len(buffer) > self.max_bytes:
                    raise BoxFileTooLarge(f"File exceeds the {self.max_bytes // (1024 * 1024)} MB limit")
            return bytes(buffer)
        finally:
            response.close()
