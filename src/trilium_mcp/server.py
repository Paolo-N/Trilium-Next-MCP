"""Trilium MCP server (stdio). Configure with TRILIUM_URL and TRILIUM_ETAPI_TOKEN."""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import Trilium, TriliumError, md_to_html
from .layout import relation_map_content

INSTRUCTIONS = """\
Tools to read and organise a TriliumNext knowledge base through ETAPI.

Conventions
- noteId 'root' is the top of the tree. Text notes store HTML; pass content_format='markdown' (default)
  and it is converted. Link notes with [[noteId]] or [[noteId|label]]: these become internal links.
- NOTE MAP (type 'noteMap') is auto-generated: it draws the notes under its parent (or under label
  #mapRootNoteId) and their *relations* and internal links. So to get a good-looking map you must
  (1) give notes a meaningful hierarchy, (2) connect them with relations (trilium_set_relation) or [[links]],
  (3) style them (trilium_style_note: icon + color). Then call trilium_create_note_map.
  Use mapIncludeRelation / mapExcludeRelation to keep the map readable.
- RELATION MAP (type 'relationMap') is a manual canvas: trilium_create_relation_map places chosen notes
  on a grid/circle; arrows are the notes' relations.
- trilium_create_structure builds a whole nested outline (with labels, relations, icons) in one call.
- Destructive ops (delete, overwrite content) save a JSON snapshot first; the path is returned.
- Search syntax: https://docs.triliumnotes.org/user-guide/advanced-usage/search  e.g. '#book', '~author.title *= Tolkien',
  'note.type = text', 'note.dateCreated >= TODAY-7'.
"""

mcp = FastMCP("trilium", instructions=INSTRUCTIONS)
_client: Trilium | None = None


def api() -> Trilium:
    global _client
    if _client is None:
        url, token = os.environ.get("TRILIUM_URL"), os.environ.get("TRILIUM_ETAPI_TOKEN")
        if not url or not token:
            raise TriliumError("TRILIUM_URL and TRILIUM_ETAPI_TOKEN must be set")
        verify = os.environ.get("TRILIUM_VERIFY_TLS", "true").lower() not in ("0", "false", "no")
        _client = Trilium(url, token, verify)
    return _client


def _out(data: Any) -> str:
    return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1)


def _brief(n: dict) -> dict:
    return {k: n.get(k) for k in ("noteId", "title", "type", "parentNoteIds", "childNoteIds")}


def _prep(content: str, type_: str, fmt: str) -> str:
    return md_to_html(content) if type_ == "text" and fmt == "markdown" and content else content


# ---------------------------------------------------------------- read ----
@mcp.tool()
def trilium_app_info() -> str:
    """Trilium version info and connectivity check."""
    return _out(api().request("GET", "/app-info"))


@mcp.tool()
def trilium_search_notes(query: str, ancestor_note_id: str | None = None, limit: int = 25,
                         order_by: str | None = None, order_direction: str | None = None,
                         fast_search: bool = False, include_archived: bool = False,
                         depth: str | None = None) -> str:
    """Search notes with Trilium search syntax (full text, #label, ~relation, note.* properties).
    `depth` e.g. 'eq1' (direct children) or 'lt4'. Returns brief results with attributes."""
    res = api().request("GET", "/notes", params={
        "search": query, "ancestorNoteId": ancestor_note_id, "limit": limit, "orderBy": order_by,
        "orderDirection": order_direction, "fastSearch": fast_search,
        "includeArchivedNotes": include_archived, "ancestorDepth": depth})
    out = []
    for n in res["results"]:
        b = _brief(n)
        b["attributes"] = [f"{'#' if a['type'] == 'label' else '~'}{a['name']}={a['value']}"
                           for a in n.get("attributes", [])]
        out.append(b)
    return _out(out)


@mcp.tool()
def trilium_get_note(note_id: str, include_content: bool = True, max_chars: int = 20000) -> str:
    """Note metadata (title, type, attributes, parents, children) plus optionally its content."""
    c = api()
    note = c.get_note(note_id)
    if include_content and note["type"] not in ("file", "image") and not note.get("isProtected"):
        content = c.get_content(note_id)
        note["content"] = content[:max_chars]
        if len(content) > max_chars:
            note["contentTruncated"] = len(content)
    return _out(note)


@mcp.tool()
def trilium_get_tree(note_id: str = "root", depth: int = 2, max_notes: int = 200) -> str:
    """Outline of the subtree below a note: id, title, type, icon/color, relations. Breadth-limited."""
    c = api()
    count = 0

    def walk(nid: str, d: int) -> dict:
        nonlocal count
        n = c.get_note(nid)
        count += 1
        attrs = {a["name"]: a["value"] for a in n.get("attributes", [])
                 if a["name"] in ("iconClass", "color")}
        rels = [f"{a['name']}->{a['value']}" for a in n.get("attributes", []) if a["type"] == "relation"]
        node: dict[str, Any] = {"id": nid, "title": n["title"], "type": n["type"], **attrs}
        if rels:
            node["relations"] = rels
        kids = n.get("childNoteIds", [])
        if kids and d > 0:
            node["children"] = []
            for k in kids:
                if count >= max_notes:
                    node["children"].append({"truncated": True})
                    break
                node["children"].append(walk(k, d - 1))
        elif kids:
            node["childCount"] = len(kids)
        return node

    return _out(walk(note_id, depth))


@mcp.tool()
def trilium_get_backlinks_and_relations(note_id: str) -> str:
    """Notes pointing to this note via relations or internal links, plus its own outgoing relations."""
    c = api()
    note = c.get_note(note_id)
    incoming = c.request("GET", "/notes", params={"search": f"~*.noteId = {note_id}", "limit": 200})["results"]
    linking = c.request("GET", "/notes", params={"search": f"note.content *=* {note_id}", "limit": 200})["results"]
    return _out({
        "outgoingRelations": [a for a in note.get("attributes", []) if a["type"] == "relation"],
        "incomingRelationFrom": [_brief(n) for n in incoming],
        "contentMentions": [_brief(n) for n in linking if n["noteId"] != note_id],
    })


# --------------------------------------------------------------- write ----
@mcp.tool()
def trilium_create_note(parent_note_id: str, title: str, content: str = "", note_type: str = "text",
                        content_format: str = "markdown", mime: str | None = None,
                        position: int | None = None, icon: str | None = None,
                        color: str | None = None) -> str:
    """Create a note. note_type: text, code, book, search, render, relationMap, noteMap, mermaid, webView...
    For 'code' notes pass a mime (e.g. 'text/x-python', 'application/json'). `icon` is a boxicons class
    ('bx bx-book'); `color` any CSS colour."""
    c = api()
    if note_type == "code" and not mime:
        mime = "text/plain"
    res = c.create_note(parent_note_id, title, note_type, _prep(content, note_type, content_format),
                        mime, position)
    nid = res["note"]["noteId"]
    if icon:
        c.set_label(nid, "iconClass", icon)
    if color:
        c.set_label(nid, "color", color)
    return _out({"noteId": nid, "branchId": res["branch"]["branchId"], "title": title})


@mcp.tool()
def trilium_update_note(note_id: str, title: str | None = None, note_type: str | None = None,
                        mime: str | None = None) -> str:
    """Rename a note or change its type/mime. Use trilium_set_note_content for the body."""
    body = {k: v for k, v in (("title", title), ("type", note_type), ("mime", mime)) if v is not None}
    return _out(_brief(api().request("PATCH", f"/notes/{note_id}", json_body=body)))


@mcp.tool()
def trilium_set_note_content(note_id: str, content: str, content_format: str = "markdown") -> str:
    """Replace the whole content of a note (a snapshot of the old one is saved first)."""
    c = api()
    note = c.get_note(note_id)
    backup = c.snapshot(note_id, "overwrite", max_notes=1)
    c.set_content(note_id, _prep(content, note["type"], content_format))
    return _out({"ok": True, "backup": backup})


@mcp.tool()
def trilium_append_note_content(note_id: str, content: str, content_format: str = "markdown") -> str:
    """Append to the end of a note's content (text notes: appended as HTML)."""
    c = api()
    note = c.get_note(note_id)
    sep = "\n" if note["type"] != "text" else ""
    c.set_content(note_id, c.get_content(note_id) + sep + _prep(content, note["type"], content_format))
    return _out({"ok": True})


@mcp.tool()
def trilium_delete_note(note_id: str) -> str:
    """Delete a note and its subtree (soft-delete in Trilium; a JSON snapshot is also saved)."""
    if note_id in ("root", "_hidden", "_share"):
        raise TriliumError(f"refusing to delete system note {note_id}")
    c = api()
    backup = c.snapshot(note_id, "delete")
    c.request("DELETE", f"/notes/{note_id}")
    return _out({"deleted": note_id, "backup": backup})


@mcp.tool()
def trilium_clone_note(note_id: str, new_parent_note_id: str, prefix: str | None = None) -> str:
    """Add the note under another parent too (Trilium 'clone'); the note then lives in both places."""
    body: dict[str, Any] = {"noteId": note_id, "parentNoteId": new_parent_note_id}
    if prefix:
        body["prefix"] = prefix
    return _out(api().request("POST", "/branches", json_body=body))


@mcp.tool()
def trilium_move_note(note_id: str, new_parent_note_id: str) -> str:
    """Move a note: clone under the new parent, then remove the branch from the old parent(s)."""
    c = api()
    note = c.get_note(note_id)
    if new_parent_note_id in note["parentNoteIds"]:
        return _out({"ok": True, "unchanged": True})
    c.request("POST", "/branches", json_body={"noteId": note_id, "parentNoteId": new_parent_note_id})
    for bid in note["parentBranchIds"]:
        c.request("DELETE", f"/branches/{bid}")
    return _out({"ok": True, "movedTo": new_parent_note_id})


@mcp.tool()
def trilium_reorder_children(parent_note_id: str, ordered_child_ids: list[str]) -> str:
    """Set the display order of a parent's children (first id = top)."""
    c = api()
    for i, cid in enumerate(ordered_child_ids):
        bid = f"{parent_note_id}_{cid}"
        c.request("PATCH", f"/branches/{bid}", json_body={"notePosition": (i + 1) * 10})
    c.request("POST", f"/refresh-note-ordering/{parent_note_id}")
    return _out({"ok": True})


# ---------------------------------------------------------- attributes ----
@mcp.tool()
def trilium_set_label(note_id: str, name: str, value: str = "", inheritable: bool = False) -> str:
    """Create/update a label (#name=value) on a note. Handy ones: iconClass, color, cssClass, archived,
    sorted, viewType (list|grid|calendar|table|geoMap|board), mapRootNoteId, readOnly."""
    return _out(api().set_label(note_id, name, value, inheritable))


@mcp.tool()
def trilium_set_relation(note_id: str, name: str, target_note_id: str, inheritable: bool = False) -> str:
    """Create a relation (~name=target) from a note to another. Relations are the edges drawn in
    Note Maps and Relation Maps, e.g. 'author', 'dependsOn', 'partOf'."""
    c = api()
    for a in c.get_note(note_id).get("attributes", []):
        if a["type"] == "relation" and a["name"] == name and a["value"] == target_note_id \
                and a["noteId"] == note_id:
            return _out(a)
    return _out(c.add_attribute(note_id, "relation", name, target_note_id, inheritable))


@mcp.tool()
def trilium_delete_attribute(attribute_id: str) -> str:
    """Delete a label or relation by attributeId (see trilium_get_note)."""
    api().request("DELETE", f"/attributes/{attribute_id}")
    return _out({"deleted": attribute_id})


@mcp.tool()
def trilium_style_note(note_ids: list[str], icon: str | None = None, color: str | None = None,
                       css_class: str | None = None) -> str:
    """Apply icon (boxicons class like 'bx bx-server'), colour and/or cssClass to one or more notes.
    Icons and colours show up on tree nodes and Note Map nodes: use them to make maps legible."""
    c = api()
    for nid in note_ids:
        if icon:
            c.set_label(nid, "iconClass", icon)
        if color:
            c.set_label(nid, "color", color)
        if css_class:
            c.set_label(nid, "cssClass", css_class)
    return _out({"styled": len(note_ids)})


# ------------------------------------------------------------- maps ----
@mcp.tool()
def trilium_create_note_map(parent_note_id: str, title: str = "Note map",
                            root_note_id: str | None = None, include_relations: str | None = None,
                            exclude_relations: str | None = None, icon: str = "bx bx-network-chart") -> str:
    """Create a Note Map note. It shows the notes under its parent (or under `root_note_id`; use
    'hoisted' for the hoisted note) with their relations and links. Put it as a child of the subtree
    you want to visualise, or point root_note_id at it. include/exclude_relations: comma-separated names."""
    c = api()
    res = c.create_note(parent_note_id, title, "noteMap", "")
    nid = res["note"]["noteId"]
    if root_note_id:
        c.set_label(nid, "mapRootNoteId", root_note_id)
    if include_relations:
        c.set_label(nid, "mapIncludeRelation", include_relations)
    if exclude_relations:
        c.set_label(nid, "mapExcludeRelation", exclude_relations)
    c.set_label(nid, "iconClass", icon)
    return _out({"noteId": nid, "type": res["note"]["type"], "title": title})


@mcp.tool()
def trilium_create_relation_map(parent_note_id: str, title: str, note_ids: list[str],
                                layout: str = "grid", spacing: int = 260,
                                positions: dict[str, list[int]] | None = None) -> str:
    """Create a Relation Map placing the given notes on a canvas. layout: grid|circle|row|column;
    `positions` optionally overrides {noteId: [x, y]}. Arrows come from the notes' relations."""
    c = api()
    pos = {k: (v[0], v[1]) for k, v in (positions or {}).items()}
    content = relation_map_content(note_ids, layout, spacing, pos)
    res = c.create_note(parent_note_id, title, "relationMap", content, "application/json")
    return _out({"noteId": res["note"]["noteId"], "notes": len(note_ids)})


@mcp.tool()
def trilium_update_relation_map(note_id: str, note_ids: list[str], layout: str = "grid",
                                spacing: int = 260) -> str:
    """Rewrite which notes a Relation Map shows and how they are laid out (existing positions are kept
    for notes that were already on the map)."""
    c = api()
    try:
        old = {n["noteId"]: (n["x"], n["y"]) for n in json.loads(c.get_content(note_id)).get("notes", [])}
    except (ValueError, KeyError):
        old = {}
    c.set_content(note_id, relation_map_content(note_ids, layout, spacing, old))
    return _out({"ok": True, "notes": len(note_ids)})


@mcp.tool()
def trilium_create_structure(parent_note_id: str, structure: list[dict]) -> str:
    """Create a nested outline in one call. Each item:
    {title, type?='text', content?, content_format?='markdown', mime?, icon?, color?,
     labels?: {name: value}, relations?: {name: targetNoteId | '@ref'}, ref?: 'key', children?: [...]}.
    `ref` names a node so other nodes can point to it with '@key' in `relations` (resolved after all
    nodes exist). Returns the created tree with ids and a ref->noteId map.
    Typical use: build a project/knowledge hierarchy, wire relations, then trilium_create_note_map."""
    c = api()
    refs: dict[str, str] = {}
    pending: list[tuple[str, str, str]] = []

    def build(parent: str, items: list[dict]) -> list[dict]:
        out = []
        for it in items:
            t = it.get("type", "text")
            res = c.create_note(parent, it["title"], t,
                                _prep(it.get("content", ""), t, it.get("content_format", "markdown")),
                                it.get("mime") or ("text/plain" if t == "code" else None))
            nid = res["note"]["noteId"]
            if it.get("ref"):
                refs[it["ref"]] = nid
            if it.get("icon"):
                c.set_label(nid, "iconClass", it["icon"])
            if it.get("color"):
                c.set_label(nid, "color", it["color"])
            for k, v in (it.get("labels") or {}).items():
                c.set_label(nid, k, str(v))
            for k, v in (it.get("relations") or {}).items():
                pending.append((nid, k, v))
            node = {"noteId": nid, "title": it["title"]}
            if it.get("children"):
                node["children"] = build(nid, it["children"])
            out.append(node)
        return out

    tree = build(parent_note_id, structure)
    for nid, name, target in pending:
        if target.startswith("@"):
            if target[1:] not in refs:
                raise TriliumError(f"unknown ref {target}")
            target = refs[target[1:]]
        c.add_attribute(nid, "relation", name, target)
    return _out({"tree": tree, "refs": refs})


# -------------------------------------------------- calendar & history ----
@mcp.tool()
def trilium_get_day_note(date: str) -> str:
    """Journal note for a day (YYYY-MM-DD); created if missing. Week/month/year notes are reachable with
    trilium_call_endpoint (/calendar/weeks/YYYY-Www, /calendar/months/YYYY-MM, /calendar/years/YYYY)."""
    return _out(_brief(api().request("GET", f"/calendar/days/{date}")))


@mcp.tool()
def trilium_get_inbox(date: str) -> str:
    """Inbox note for a date (YYYY-MM-DD): where new quick captures should go."""
    return _out(_brief(api().request("GET", f"/inbox/{date}")))


@mcp.tool()
def trilium_recent_changes(ancestor_note_id: str | None = None) -> str:
    """Recently changed notes (optionally within a subtree)."""
    return _out(api().request("GET", "/notes/history", params={"ancestorNoteId": ancestor_note_id}))


@mcp.tool()
def trilium_create_revision(note_id: str) -> str:
    """Force a revision (version history entry) of a note, e.g. before a big edit."""
    api().request("POST", f"/notes/{note_id}/revision")
    return _out({"ok": True})


@mcp.tool()
def trilium_list_revisions(note_id: str) -> str:
    """List stored revisions of a note."""
    return _out(api().request("GET", f"/notes/{note_id}/revisions"))


@mcp.tool()
def trilium_call_endpoint(method: str, path: str, query: dict | None = None, body: dict | None = None) -> str:
    """Escape hatch: call any ETAPI endpoint (path relative to /etapi, e.g. '/notes/abc/attachments').
    Prefer the dedicated tools; JSON body only."""
    return _out(api().request(method.upper(), path, params=query, json_body=body))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
