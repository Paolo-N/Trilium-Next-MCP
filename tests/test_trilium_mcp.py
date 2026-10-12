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
        self.content = {}
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
            self.content[nid] = body["content"]
            self.notes[nid] = {"noteId": nid, "title": body["title"], "type": body["type"], "attributes": [],
                               "mime": body.get("mime", "text/html"),
                               "parentNoteIds": [body["parentNoteId"]], "childNoteIds": [],
                               "parentBranchIds": [f"{body['parentNoteId']}_{nid}"]}
            self.notes[body["parentNoteId"]]["childNoteIds"].append(nid)
            return httpx.Response(201, json={"note": self.notes[nid], "branch": {"branchId": f"x_{nid}"}})
        if req.method == "POST" and path == "/attributes":
            a = {**body, "attributeId": f"a{len(self.log)}"}
            self.notes[body["noteId"]]["attributes"].append(a)
            return httpx.Response(201, json=a)
        if path.endswith("/content"):
            nid = path.split("/")[2]
            if req.method == "PUT":
                self.content[nid] = req.content.decode()
                return httpx.Response(204)
            # like the real server: Content-Type follows the note's mime
            return httpx.Response(200, content=self.content.get(nid, "").encode(),
                                  headers={"content-type": self.notes[nid].get("mime", "text/html")})
        if path.startswith("/notes/") and req.method == "GET":
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


def _positions(fake, nid):
    return {n["noteId"]: (n["x"], n["y"]) for n in json.loads(fake.content[nid])["notes"]}


def test_relation_map_create_respects_positions(fake):
    out = json.loads(server.trilium_create_relation_map(
        "root", "M", ["a1", "b2", "c3"], positions={"b2": [111, 222], "zz": [9, 9]}))
    pos = _positions(fake, out["noteId"])
    assert pos["b2"] == (111, 222)
    assert set(pos) == {"a1", "b2", "c3"}  # ids not in note_ids are ignored


def test_relation_map_update_reads_json_content(fake):
    """Regression: json-mime content used to come back as a dict -> json.loads TypeError."""
    nid = json.loads(server.trilium_create_relation_map(
        "root", "M", ["a1", "b2"], positions={"a1": [10, 20]}))["noteId"]
    server.trilium_update_relation_map(nid, ["a1", "b2", "c3"], positions={"c3": [7, 8]})
    pos = _positions(fake, nid)
    assert pos["a1"] == (10, 20)  # kept from the existing map
    assert pos["c3"] == (7, 8)
    server.trilium_update_relation_map(nid, ["a1"], positions={"a1": [1, 2]})
    assert _positions(fake, nid) == {"a1": (1, 2)}


def test_get_content_is_always_text(fake):
    nid = json.loads(server.trilium_create_relation_map("root", "M", ["a1"]))["noteId"]
    assert isinstance(server.api().get_content(nid), str)


def _transform(fake, nid):
    return json.loads(fake.content[nid])["transform"]


def test_relation_map_update_preserves_transform(fake):
    nid = json.loads(server.trilium_create_relation_map(
        "root", "M", ["a1"], transform={"x": 319.6, "y": 484.6, "scale": 0.57}))["noteId"]
    assert _transform(fake, nid) == {"x": 319.6, "y": 484.6, "scale": 0.57}
    server.trilium_update_relation_map(nid, ["a1", "b2"])
    assert _transform(fake, nid) == {"x": 319.6, "y": 484.6, "scale": 0.57}
    server.trilium_update_relation_map(nid, ["a1", "b2"], transform={"scale": 1})
    assert _transform(fake, nid) == {"x": 319.6, "y": 484.6, "scale": 1}
