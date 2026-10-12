import json

import httpx
import pytest

from trilium_mcp import server
from trilium_mcp.client import Trilium, md_to_html
from trilium_mcp.layout import layout_positions, relation_map_content


class FakeTrilium:
    """Minimal in-memory ETAPI."""

    def __init__(self):
        self.notes = {"root": {"noteId": "root", "title": "root", "type": "book", "attributes": [],
                               "parentNoteIds": [], "childNoteIds": [], "parentBranchIds": []}}
        self.n = 0
        self.log = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/etapi")
        body = json.loads(req.content) if req.content and "json" in req.headers.get("content-type", "") else None
        self.log.append((req.method, path, body))
        if req.method == "POST" and path == "/create-note":
            if body["type"] == "noteMap":
                return httpx.Response(400, text="'noteMap' is not a valid note type")
            self.n += 1
            nid = f"n{self.n:04d}"
            self.notes[nid] = {"noteId": nid, "title": body["title"], "type": body["type"], "attributes": [],
                               "parentNoteIds": [body["parentNoteId"]], "childNoteIds": [],
                               "parentBranchIds": [f"{body['parentNoteId']}_{nid}"]}
            self.notes[body["parentNoteId"]]["childNoteIds"].append(nid)
            return httpx.Response(201, json={"note": self.notes[nid], "branch": {"branchId": f"x_{nid}"}})
        if req.method == "POST" and path == "/attributes":
            a = {**body, "attributeId": f"a{len(self.log)}"}
            self.notes[body["noteId"]]["attributes"].append(a)
            return httpx.Response(201, json=a)
        if path.startswith("/notes/") and req.method == "GET" and not path.endswith("/content"):
            return httpx.Response(200, json=self.notes[path.split("/")[2]])
        if req.method == "PATCH" and path.startswith("/notes/"):
            self.notes[path.split("/")[2]].update(body)
            return httpx.Response(200, json=self.notes[path.split("/")[2]])
        return httpx.Response(204)


@pytest.fixture
def fake(monkeypatch):
    f = FakeTrilium()
    c = Trilium("http://t", "tok")
    c.http = httpx.Client(base_url="http://t/etapi", transport=httpx.MockTransport(f.handler))
    monkeypatch.setattr(server, "_client", c)
    return f


def test_markdown_and_links():
    html = md_to_html("# Hi\n\nsee [[abcd1234|Other]] and [[bad link]]")
    assert "<h1>" in html
    assert 'href="#root/abcd1234">Other</a>' in html
    assert "[[bad link]]" in html


def test_layouts():
    assert len(layout_positions(7, "circle")) == 7
    assert layout_positions(3, "row") == [(0, 0), (260, 0), (520, 0)]
    c = json.loads(relation_map_content(["a", "b"], positions={"a": (5, 6)}))
    assert c["notes"][0] == {"noteId": "a", "x": 5, "y": 6}
    assert c["transform"]["scale"] == 1


def test_note_map_fallback_and_labels(fake):
    out = json.loads(server.trilium_create_note_map("root", "Map", root_note_id="root",
                                                    include_relations="dependsOn"))
    note = fake.notes[out["noteId"]]
    assert note["type"] == "noteMap"
    names = {a["name"]: a["value"] for a in note["attributes"]}
    assert names["mapRootNoteId"] == "root" and names["mapIncludeRelation"] == "dependsOn"


def test_structure_resolves_refs(fake):
    res = json.loads(server.trilium_create_structure("root", [
        {"title": "Proj", "ref": "p", "icon": "bx bx-folder", "children": [
            {"title": "Task", "relations": {"partOf": "@p"}, "labels": {"status": "todo"}}]}]))
    task = fake.notes[res["tree"][0]["children"][0]["noteId"]]
    rel = next(a for a in task["attributes"] if a["type"] == "relation")
    assert rel["value"] == res["refs"]["p"]


def test_refuses_system_delete(fake):
    with pytest.raises(Exception):
        server.trilium_delete_note("root")
