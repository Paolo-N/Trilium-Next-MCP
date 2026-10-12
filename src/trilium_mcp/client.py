"""Thin synchronous client for the Trilium ETAPI."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import markdown as _markdown

BACKUP_DIR = Path(os.environ.get("TRILIUM_BACKUP_DIR", "/tmp/trilium-mcp-backups"))

_WIKI_LINK = re.compile(r"\[\[([A-Za-z0-9_]{4,32})(?:\|([^\]]+))?\]\]")


class TriliumError(RuntimeError):
    pass


def md_to_html(text: str) -> str:
    """Markdown -> HTML for Trilium text notes.

    `[[noteId]]` / `[[noteId|label]]` become Trilium internal links, which is
    what feeds the link map of a Note Map.
    """

    def link(m: re.Match[str]) -> str:
        nid, label = m.group(1), m.group(2)
        return f'<a class="reference-link" href="#root/{nid}">{label or nid}</a>'

    text = _WIKI_LINK.sub(link, text)
    return _markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])


class Trilium:
    def __init__(self, base_url: str, token: str, verify_tls: bool = True, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=f"{self.base_url}/etapi",
            headers={"Authorization": token},
            verify=verify_tls,
            timeout=timeout,
        )

    # -- low level -------------------------------------------------------
    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: Any = None, content: str | bytes | None = None,
                content_type: str | None = None, raw: bool = False) -> Any:
        headers = {"Content-Type": content_type} if content_type else None
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for k, v in params.items():
            if isinstance(v, bool):
                params[k] = "true" if v else "false"
        r = self.http.request(method, path, params=params, json=json_body,
                              content=content, headers=headers)
        if r.status_code >= 400:
            raise TriliumError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        if r.status_code == 204 or not r.content:
            return None
        if not raw and "json" in r.headers.get("content-type", ""):
            return r.json()
        return r.text

    # -- notes -----------------------------------------------------------
    def get_note(self, note_id: str) -> dict:
        return self.request("GET", f"/notes/{note_id}")

    def get_content(self, note_id: str) -> str:
        # raw: json-mime notes (relationMap, code/json) must come back as text, not parsed
        return self.request("GET", f"/notes/{note_id}/content", raw=True) or ""

    def set_content(self, note_id: str, content: str) -> None:
        self.request("PUT", f"/notes/{note_id}/content", content=content.encode(),
                     content_type="text/plain")

    def create_note(self, parent_note_id: str, title: str, type_: str = "text",
                    content: str = "", mime: str | None = None,
                    note_position: int | None = None) -> dict:
        body: dict[str, Any] = {"parentNoteId": parent_note_id, "title": title,
                                "type": type_, "content": content}
        if mime:
            body["mime"] = mime
        if note_position is not None:
            body["notePosition"] = note_position
        try:
            return self.request("POST", "/create-note", json_body=body)
        except TriliumError as e:
            # create-note may reject types missing from its allow-list (e.g. noteMap);
            # PATCH does not validate the type, so create as text and convert.
            if type_ in ("text", "code") or "not a valid note type" not in str(e):
                raise
            body["type"], body["content"] = "text", ""
            body.pop("mime", None)
            res = self.request("POST", "/create-note", json_body=body)
            patch: dict[str, Any] = {"type": type_}
            if mime is not None:
                patch["mime"] = mime
            self.request("PATCH", f"/notes/{res['note']['noteId']}", json_body=patch)
            if content:
                self.set_content(res["note"]["noteId"], content)
            res["note"]["type"] = type_
            return res

    # -- attributes ------------------------------------------------------
    def add_attribute(self, note_id: str, type_: str, name: str, value: str = "",
                      inheritable: bool = False, position: int | None = None) -> dict:
        body: dict[str, Any] = {"noteId": note_id, "type": type_, "name": name,
                                "value": value, "isInheritable": inheritable}
        if position is not None:
            body["position"] = position
        return self.request("POST", "/attributes", json_body=body)

    def set_label(self, note_id: str, name: str, value: str = "", inheritable: bool = False) -> dict:
        """Upsert: replaces the value of an existing own label with that name."""
        for a in self.get_note(note_id).get("attributes", []):
            if a["type"] == "label" and a["name"] == name and a["noteId"] == note_id:
                if a["value"] != value:
                    self.request("PATCH", f"/attributes/{a['attributeId']}", json_body={"value": value})
                return {**a, "value": value}
        return self.add_attribute(note_id, "label", name, value, inheritable)

    # -- safety net ------------------------------------------------------
    def snapshot(self, note_id: str, reason: str, max_notes: int = 500) -> str:
        """Dump a note subtree (metadata + content) to the backup dir before destructive ops."""
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        seen: dict[str, dict] = {}
        queue = [note_id]
        while queue and len(seen) < max_notes:
            nid = queue.pop(0)
            if nid in seen:
                continue
            note = self.get_note(nid)
            entry = {"note": note, "content": None}
            if note.get("type") not in ("file", "image") and not note.get("isProtected"):
                try:
                    entry["content"] = self.get_content(nid)
                except TriliumError:
                    pass
            seen[nid] = entry
            queue.extend(note.get("childNoteIds", []))
        path = BACKUP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{reason}_{note_id}.json"
        path.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(path)
