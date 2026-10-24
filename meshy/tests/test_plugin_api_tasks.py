"""Task lookup, redirects and size caps of the Meshy router, against a loopback stand-in for api.meshy.ai.

Real router code over real HTTP: the router's API base and asset-host allowlist point at loopback servers
(test-only monkeypatches), so redirects, Content-Length and streamed bodies are the real urllib paths. The
stand-in follows Meshy's documented shapes (docs/openapi + the Meshy MCP server's endpoint notes): every
task answers in full on its own route; the generic routes also answer for other tasks, reshaped without
their files, except Creative Lab and Text to Motion, which answer only on their own routes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import HTTPException

_SRC = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_KEY = "msy-test-not-a-real-key"
_CL = ("figure", "lamp", "keychain", "fridge-magnet", "vinyl-figure", "brick-figure", "keycap", "fidget-pixel")
# Meshy's task routes (Meshy MCP server utils/endpoints.js TASK_ENDPOINTS), by the record's ``type``.
_ROUTES = {
    "text-to-3d-preview": "/openapi/v2/text-to-3d", "image-to-3d": "/openapi/v1/image-to-3d",
    "multi-image-to-3d": "/openapi/v1/multi-image-to-3d", "remesh": "/openapi/v1/remesh",
    "retexture": "/openapi/v1/retexture", "rig": "/openapi/v1/rigging", "animate": "/openapi/v1/animations",
    "text-to-image": "/openapi/v1/text-to-image", "image-to-image": "/openapi/v1/image-to-image",
    "convert": "/openapi/v1/convert", "resize": "/openapi/v1/resize", "uv-unwrap": "/openapi/v1/uv-unwrap",
    "print-repair": "/openapi/v1/print/repair", "print-split": "/openapi/v1/print/split",
    "print-multi-color": "/openapi/v1/print/multi-color", "print-analyze": "/openapi/v1/print/analyze",
    "text-to-motion": "/openapi/v1/text-to-motion",
    **{f"creative-lab-{p}-{s}": f"/openapi/creative-lab/{p}/v1/{s}" for p in _CL for s in ("prototype", "build")},
    "creative-lab-fidget-collapsible": "/openapi/creative-lab/fidget-collapsible/v1",
}
_GENERIC = {"/openapi/v2/text-to-3d", "/openapi/v1/image-to-3d", "/openapi/v1/multi-image-to-3d",
            "/openapi/v1/retexture", "/openapi/v1/print/repair"}
_FILES = ("model_urls", "result", "image_urls", "texture_urls", "thumbnail_url")


def _tid(n: int) -> str:
    return f"01a157b8-7317-7551-abc5-{n:012x}"


class _Server:
    """One loopback HTTP server; ``handle(path, headers) -> (status, headers, body)``; records every hit."""

    def __init__(self, handle):
        hits, lock = [], threading.Lock()

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                with lock:
                    hits.append((self.path, self.headers.get("Authorization")))
                status, headers, body = handle(self.path, self.headers)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in headers and status not in (204, 304):
                    if not headers.get("X-No-Length"):
                        self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        self.hits = hits
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.httpd.block_on_close = False
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def url(self, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Meshy:
    """api.meshy.ai + assets.meshy.ai stand-in. ``generic=False`` models routes that answer only their own type."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.overrides: dict[str, tuple[int, dict, bytes]] = {}
        self.assets: dict[str, object] = {}
        self.generic = True
        self.owner_key = _KEY
        self.server = _Server(self._handle)

    def add(self, task_type: str, n: int, **fields) -> str:
        task_id = _tid(n)
        self.tasks[task_id] = {"id": task_id, "type": task_type, "status": "SUCCEEDED", "progress": 100,
                               "created_at": 1_000 + n, **fields}
        return task_id

    def meshy_hits(self) -> list[str]:
        return [p for p, _auth in self.server.hits if p.startswith("/openapi/")]

    def _handle(self, raw: str, headers):
        path = raw.split("?", 1)[0]
        if path in self.overrides:
            return self.overrides[path]
        if path.startswith("/assets/"):
            got = self.assets.get(path[len("/assets/"):], 404)
            if isinstance(got, int):
                return got, {}, b""
            return got if isinstance(got, tuple) else (200, {}, got)
        if headers.get("Authorization") != f"Bearer {self.owner_key}":
            return 404, {}, b'{"message": "Not Found"}'
        route, _, last = path.rpartition("/")
        if last in self.tasks:
            row = self.tasks[last]
            own = _ROUTES[row["type"]]
            if route == own:
                return 200, {"Content-Type": "application/json"}, json.dumps(row).encode()
            if self.generic and route in _GENERIC and not row["type"].startswith(("creative-lab-", "text-to-motion")):
                shaped = {k: v for k, v in row.items() if k not in _FILES}
                return 200, {"Content-Type": "application/json"}, json.dumps(shaped).encode()
            return 404, {}, b'{"message": "Not Found"}'
        if path in _ROUTES.values():
            rows = [r for r in self.tasks.values() if _ROUTES[r["type"]] == path]
            return 200, {"Content-Type": "application/json"}, json.dumps(rows).encode()
        return 404, {}, b'{"message": "Not Found"}'


@pytest.fixture
def meshy():
    stand_in = _Meshy()
    foreign = _Server(lambda *_a: (200, {}, b"FOREIGN"))
    stand_in.foreign = foreign
    yield stand_in
    stand_in.server.close()
    foreign.close()


@pytest.fixture
def api(tmp_path, monkeypatch, meshy):
    home, hermes = tmp_path / "home", tmp_path / "hermes"
    data = hermes / "plugin-data" / "agent-plugin-meshy"
    for d in (home, data, tmp_path / "outside"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("MESHY_API_KEY", _KEY)

    def tripwire(*_a, **_k):
        raise AssertionError("bare urlopen: every request must go through the router's guarded opener")

    monkeypatch.setattr(urllib.request, "urlopen", tripwire)
    spec = importlib.util.spec_from_file_location("meshy_plugin_api_tasks_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Test-only: the API base and the asset host are the loopback stand-in (no real Meshy host is reachable).
    monkeypatch.setattr(mod, "_API", meshy.server.url())
    monkeypatch.setattr(mod, "_ASSET_HOSTS", frozenset({"127.0.0.1"}))
    monkeypatch.setattr(mod, "_ASSET_SCHEMES", frozenset({"http"}))
    monkeypatch.setattr(mod, "_api_key", lambda: os.environ.get("MESHY_API_KEY"))
    mod.tmp, mod.data, mod.home = tmp_path, data, home
    return mod


def _status(fn, *args, **kwargs) -> HTTPException:
    with pytest.raises(HTTPException) as err:
        fn(*args, **kwargs)
    return err.value


def _asset(meshy, name: str) -> str:
    return f"{meshy.server.url()}/assets/{name}"


# --- item 2: a card resolves every task type the plugin creates -------------------------------------------

_CASES = [  # type, fields, has_model
    ("convert", {"model_urls": {"glb": "G", "fbx": "F"}}, True),
    ("resize", {"model_urls": {"glb": "G"}}, True),
    ("uv-unwrap", {"model_urls": {"glb": "G"}}, True),
    ("print-repair", {"model_urls": {"glb": "G", "stl": "S"}}, True),
    ("print-split", {"model_urls": {"glb": "G"}, "part_count": 3}, True),
    ("print-multi-color", {"model_urls": {"3mf": "M"}}, True),  # 3MF is viewable (item 13)
    ("text-to-motion", {"result": {"motion_url": "F", "motion_format": "fbx"}}, False),
    ("creative-lab-figure-build", {"model_urls": {"glb": "G", "obj": "O"}}, True),
    ("creative-lab-lamp-prototype", {"image_urls": ["I"]}, False),
    ("creative-lab-fidget-collapsible", {"model_urls": {"glb": "G"}}, True),
]


@pytest.mark.parametrize("generic", [True, False], ids=["generic-routes", "own-routes-only"])
def test_task_card_resolves_every_task_type_and_polls_with_one_call(api, meshy, generic):
    meshy.generic = generic
    for n, (kind, fields, has_model) in enumerate(_CASES):
        fields = {k: ({f: _asset(meshy, f"{n}.{f}") for f in v} if k == "model_urls" else v) for k, v in fields.items()}
        task = meshy.add(kind, n, **fields)
        got = api.task_state(task)
        assert (got["status"], got["type"], got["has_model"]) == ("SUCCEEDED", kind, has_model), kind
        meshy.server.hits.clear()
        api.task_state(task)  # the card's next poll: the owning route, once
        assert meshy.meshy_hits() == [f"{_ROUTES[kind]}/{task}"], kind
    convert = _tid(0)
    assert [f["format"] for f in api.formats(convert)["formats"]] == ["glb", "fbx"]


def test_unknown_task_is_404_and_a_retry_does_not_probe_again(api, meshy, monkeypatch):
    unknown = _tid(999)
    err = _status(api.task_state, unknown)
    assert (err.status_code, err.detail) == (404, "Meshy has no finished task with that id")
    assert len(meshy.meshy_hits()) == len(set(_ROUTES.values()))  # every route once, never twice
    meshy.server.hits.clear()
    assert _status(api.task_state, unknown).status_code == 404
    assert meshy.meshy_hits() == []  # the card's retry within the window costs no Meshy call
    # The verdict expires: a task that shows up later is found.
    monkeypatch.setattr(api, "_MISS_TTL_S", 0.0)
    assert _status(api.task_state, unknown).status_code == 404  # still cached from before the patch
    with api._ROUTE_LOCK:
        api._MISS_CACHE.clear()
    meshy.add("convert", 999, model_urls={"glb": _asset(meshy, "m.glb")})
    assert api.task_state(unknown)["status"] == "SUCCEEDED"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_meshy_outage_is_a_502_never_no_such_task(api, meshy, status):
    task = meshy.add("convert", 1, model_urls={"glb": _asset(meshy, "m.glb")})
    meshy.overrides[f"/openapi/v1/image-to-3d/{task}"] = (status, {}, b"{}")
    assert _status(api.task_state, task).status_code == 502
    # Outage in the probe of the remaining routes (own-route-only Meshy) is a 502 too, and not remembered.
    meshy.generic = False
    meshy.overrides = {f"/openapi/v1/uv-unwrap/{_tid(2)}": (status, {}, b"{}")}
    assert _status(api.task_state, _tid(2)).status_code == 502
    meshy.overrides = {}
    assert api.task_state(task)["has_model"]


def test_lookups_are_per_api_key(api, meshy, monkeypatch):
    """One process serves several profiles: another key's miss never hides this key's task."""
    task = meshy.add("print-repair", 3, model_urls={"glb": _asset(meshy, "m.glb")})
    monkeypatch.setenv("MESHY_API_KEY", "msy-another-profile")
    assert _status(api.task_state, task).status_code == 404
    monkeypatch.setenv("MESHY_API_KEY", _KEY)
    assert api.task_state(task)["has_model"]


def test_route_cache_is_bounded_and_safe_under_concurrent_polls(api, meshy, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(api, "_ROUTE_CACHE_CAP", 3)
    tasks = [meshy.add("resize", 10 + i, model_urls={"glb": _asset(meshy, "m.glb")}) for i in range(6)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        states = list(pool.map(api.task_state, tasks * 4))
    assert all(s["status"] == "SUCCEEDED" for s in states)
    assert len(api._ROUTE_CACHE) <= 3


def test_progress_chip_and_gallery_cover_post_processing_and_creative_lab(api, meshy):
    running = meshy.add("print-repair", 20, status="IN_PROGRESS", progress=40)
    queued = meshy.add("convert", 21, status="PENDING", progress=0)
    import time
    for t in (running, queued):
        meshy.tasks[t]["created_at"] = int(time.time() * 1000)
    active = api.active(tasks=f"print-repair:{running},convert:{queued}")["tasks"]
    assert sorted((t["task_id"], t["task_type"]) for t in active) == [(running, "print-repair"), (queued, "convert")]

    converted = meshy.add("convert", 22, model_urls={"glb": _asset(meshy, "c.glb")})
    figure = meshy.add("creative-lab-figure-build", 23, model_urls={"glb": _asset(meshy, "f.glb")})
    listed = {m["task_id"]: m for m in api.models(limit=10)["models"]}
    assert listed[converted]["task_type"] == "convert" and listed[converted]["glb_url"].endswith("c.glb")
    assert listed[figure]["task_type"] == "creative-lab-figure-build"


def test_gallery_opens_the_glb_when_a_task_has_several_saved_formats(api, meshy):
    task = meshy.add("image-to-3d", 30, model_urls={"glb": _asset(meshy, "m.glb")})
    folder = api.data / "meshy_output" / task
    folder.mkdir(parents=True)
    (folder / ".meshy-task").write_text(task, encoding="utf-8")
    glb, stl = folder / "model.glb", folder / "model.stl"
    glb.write_bytes(b"glTF")
    stl.write_bytes(b"solid")
    os.utime(glb, (1_000_000, 1_000_000))  # the STL was saved last
    row = next(m for m in api.models(limit=10)["models"] if m["task_id"] == task)
    assert row["local_path"] == str(glb)


# --- hardening (b): redirects stay on the allowed host -------------------------------------------------------

def _redirect_to(location: str) -> tuple[int, dict, bytes]:
    return 302, {"Location": location}, b""


@pytest.mark.parametrize("route", ["download", "remote"])
def test_asset_redirect_off_the_asset_host_is_refused(api, meshy, route):
    meshy.assets["m.glb"] = _redirect_to(f"{meshy.foreign.url('localhost')}/m.glb")
    dest = api.data / "model.glb"
    err = _status(api._download, _asset(meshy, "m.glb"), dest) if route == "download" else _status(api.remote, _asset(meshy, "m.glb"))
    assert err.status_code == 502 and "redirect" in err.detail
    assert meshy.foreign.hits == []
    assert not dest.exists() and not list(api.data.glob("*.part"))
    # A hop that stays on the asset host is followed (signed links may bounce within the CDN).
    meshy.assets["m.glb"] = _redirect_to(_asset(meshy, "real.glb"))
    meshy.assets["real.glb"] = b"glTF-real"
    if route == "download":
        api._download(_asset(meshy, "m.glb"), dest)
        assert dest.read_bytes() == b"glTF-real"
    else:
        assert api.remote(_asset(meshy, "m.glb"))["bytes"] == 9


def test_obj_material_redirect_off_the_asset_host_is_refused(api, meshy):
    task = meshy.add("image-to-3d", 40, model_urls={"obj": _asset(meshy, "m.obj"), "mtl": _asset(meshy, "m.mtl")})
    meshy.assets["m.obj"] = b"mtllib material.mtl\nv 0 0 0\n"
    meshy.assets["m.mtl"] = _redirect_to(f"{meshy.foreign.url('localhost')}/m.mtl")
    assert _status(api.save, {"task": task, "format": "obj"}).status_code == 502
    assert meshy.foreign.hits == []
    assert not (api.data / "meshy_output" / task / "model.mtl").exists()


def test_api_redirect_never_carries_the_key_to_another_host(api, meshy):
    task = _tid(41)
    meshy.overrides[f"/openapi/v1/image-to-3d/{task}"] = _redirect_to(f"{meshy.foreign.url('localhost')}/steal")
    assert _status(api.task_state, task).status_code == 502
    assert meshy.foreign.hits == []


# --- hardening (d): size caps leave nothing behind ----------------------------------------------------------

def test_declared_and_counted_size_caps_leave_nothing_on_disk(api, meshy, monkeypatch):
    monkeypatch.setattr(api, "_MAX_SAVE_BYTES", 1000)
    monkeypatch.setattr(api, "_MAX_FILE_BYTES", 1000)
    # Declares a body over the cap: refused before reading it (the stand-in sends 10 bytes and hangs up).
    meshy.assets["declared.glb"] = (200, {"Content-Length": str(10**9)}, b"0123456789")
    # Declares nothing and streams past the cap: refused by the bytes counted.
    meshy.assets["streamed.glb"] = (200, {"X-No-Length": "1"}, b"x" * 5000)
    for name in ("declared.glb", "streamed.glb"):
        dest = api.data / name
        err = _status(api._download, _asset(meshy, name), dest)
        assert err.status_code == 413, name
        assert not dest.exists() and not [p for p in api.data.iterdir() if p.name.endswith(".part")], name
        assert _status(api.remote, _asset(meshy, name)).status_code == 413, name


# --- hardening (c): clean errors, not raw 500s ----------------------------------------------------------------

def test_meshy_answers_of_the_wrong_shape_never_raise(api, meshy):
    meshy.overrides["/openapi/v1/balance"] = (200, {}, b'["not", "an", "object"]')
    assert api.status()["balance"] is None
    task = meshy.add("text-to-3d-preview", 50, progress="n/a", status="IN_PROGRESS", model_type="standard")
    assert api.task_state(task)["progress"] == 0
    assert api.actions(task)["balance"] is None
    meshy.overrides["/openapi/v1/convert"] = (200, {}, b'["junk", 7, null]')
    assert isinstance(api.models(limit=5)["models"], list)
    meshy.overrides[f"/openapi/v1/image-to-3d/{_tid(51)}"] = (200, {}, b"<html>not json</html>")
    assert _status(api.task_state, _tid(51)).status_code == 502


def test_a_task_without_a_glb_is_viewable_in_its_best_format(api, meshy):
    # Meshy's documented Convert record lists only the formats it made; the card's own Convert row makes these.
    task = meshy.add("convert", 7, model_urls={"fbx": _asset(meshy, "c.fbx"), "stl": _asset(meshy, "c.stl")})
    got = api.task_state(task)
    assert (got["has_model"], got["view_format"]) == (True, "fbx")
    assert [r["glb_url"] for r in api.models(limit=60)["models"] if r["task_id"] == task] == [_asset(meshy, "c.fbx")]


def test_a_rejected_key_is_a_permanent_4xx_and_a_plan_gated_route_is_not(api, meshy):
    task = meshy.add("convert", 8, model_urls={"glb": _asset(meshy, "m.glb")})
    # A route this key's plan cannot read answers 403; the task is still found on its own route.
    meshy.overrides[f"/openapi/v1/image-to-3d/{task}"] = (403, {}, b"{}")
    assert api.task_state(task)["has_model"]
    # A revoked key (401 everywhere): a 4xx the card stops on, never a 502 it retries forever.
    meshy.owner_key = "msy_revoked"
    meshy.overrides = {f"/openapi/v1/image-to-3d/{_tid(9)}": (401, {}, b"{}")}
    err = _status(api.task_state, _tid(9))
    assert (err.status_code, err.detail) == (403, "Meshy rejected the API key")


def test_a_refusal_on_every_route_is_meshys_403_never_no_such_task(api, meshy):
    # Meshy's 403 (IP allowlist, org file filtering) on every route: its reason reaches the card as a 403,
    # and it is never remembered as "no such task".
    refused = (403, {}, b'{"message": "ip_not_authorized: 203.0.113.9"}')
    task = _tid(11)
    meshy.overrides = {f"{r}/{task}": refused for r in set(_ROUTES.values())}
    err = _status(api.task_state, task)
    assert err.status_code == 403 and "ip_not_authorized" in err.detail
    with api._ROUTE_LOCK:
        assert not api._MISS_CACHE
    # An outage on one route outranks a refusal elsewhere: the card retries instead of stopping.
    meshy.overrides[f"/openapi/v1/uv-unwrap/{task}"] = (503, {}, b"{}")
    assert _status(api.task_state, task).status_code == 502
    # The task's own route refusing after it was found is a 403 too, never a file-less generic record.
    meshy.overrides = {}
    found = meshy.add("convert", 12, model_urls={"glb": _asset(meshy, "m.glb")})
    assert api.task_state(found)["has_model"]
    meshy.overrides = {f"{_ROUTES['convert']}/{found}": refused}
    assert _status(api.task_state, found).status_code == 403


def test_a_task_with_only_a_blend_still_offers_its_download(api, meshy):
    task = meshy.add("convert", 13, model_urls={"blend": _asset(meshy, "m.blend")})
    got = api.task_state(task)
    assert (got["has_model"], got["has_files"]) == (False, True)


def test_a_plan_gated_route_never_turns_an_unknown_task_into_a_refusal(api, meshy):
    # One product route 403s for every id (the plan lacks it); a deleted task is still "no such task", cached.
    unknown = _tid(14)
    meshy.overrides = {f"/openapi/v1/print/repair/{unknown}": (403, {}, b'{"message": "plan lacks endpoint"}')}
    assert "/openapi/v1/print/repair" in _ROUTES.values()
    assert _status(api.task_state, unknown).status_code == 404
    meshy.server.hits.clear()
    assert _status(api.task_state, unknown).status_code == 404
    assert meshy.meshy_hits() == []


def test_progress_chip_reads_only_the_tasks_it_names(api, meshy):
    """One Meshy read per named task on its own route, none when the chip watches nothing (was 17 list calls)."""
    running = meshy.add("text-to-3d-preview", 30, status="IN_PROGRESS", progress=40, prompt="duck")
    done = meshy.add("rig", 31)
    rigged = meshy.add("rig", 32, status="PENDING", progress=0)
    assert api.active() == {"configured": True, "tasks": [], "done": [], "missing": []}
    assert meshy.meshy_hits() == []

    got = api.active(tasks=f"text-to-3d:{running},rigging:{done},any:{rigged},remesh:{_tid(33)}")
    assert [(t["task_id"], t["task_type"], t["progress"]) for t in got["tasks"]] == [
        (rigged, "rigging", 0), (running, "text-to-3d", 40)]
    assert (got["done"], got["missing"]) == ([done], [_tid(33)])
    hits = meshy.meshy_hits()
    assert f"/openapi/v2/text-to-3d/{running}" in hits and f"/openapi/v1/rigging/{done}" in hits
    assert not any("page_size" in h or h.rstrip("/").endswith(("text-to-3d", "rigging")) for h in hits)
    meshy.server.hits.clear()
    api.active(tasks=f"text-to-3d:{running},any:{rigged}")
    assert len(meshy.meshy_hits()) == 2  # the "any" task's route is cached after its first read


@pytest.mark.parametrize("tasks", ["text-to-3d", f"Text:{_tid(1)}", f"text-to-3d:{_tid(1)}/../balance", "any:../../v1/balance",
                                   ",".join(f"text-to-3d:{_tid(i)}" for i in range(9))])
def test_progress_chip_input_is_checked_before_any_meshy_call(api, meshy, tasks):
    assert _status(api.active, tasks=tasks).status_code == 400
    assert meshy.meshy_hits() == []


def test_progress_chip_stops_on_a_refused_key_and_retries_an_outage(api, meshy):
    task = meshy.add("text-to-3d-preview", 34, status="IN_PROGRESS", progress=10)
    meshy.overrides = {f"/openapi/v2/text-to-3d/{task}": (401, {}, b"{}")}
    assert _status(api.active, tasks=f"text-to-3d:{task}").status_code == 403  # permanent: the chip stops
    meshy.overrides = {f"/openapi/v2/text-to-3d/{task}": (503, {}, b"{}")}
    assert _status(api.active, tasks=f"text-to-3d:{task}").status_code == 502  # transient: it backs off


def test_status_names_a_403_refusal_instead_of_blaming_the_key(api, meshy):
    meshy.overrides = {"/openapi/v1/balance": (403, {}, b'{"message": "ip_not_authorized: 203.0.113.9"}')}
    assert api.status()["error"] == "Meshy refused the request: ip_not_authorized: 203.0.113.9"
    meshy.overrides = {"/openapi/v1/balance": (401, {}, b"{}")}
    assert api.status()["error"] == "Meshy rejected the API key"


def test_progress_chip_finds_a_task_named_with_a_wrong_kind(api, meshy):
    # Meshy's server accepts a wrong task_type on a status call; one such entry must not fail the whole batch.
    task = meshy.add("text-to-motion", 35, status="IN_PROGRESS", progress=70)
    got = api.active(tasks=f"preview:{task}")
    assert [(t["task_id"], t["task_type"]) for t in got["tasks"]] == [(task, "text-to-motion")]


def test_progress_chip_lookups_never_starve_the_shared_probe_slots(api, meshy):
    """Four lookups of ids no generic route knows run _task's own fan-out; they must finish, and a card's
    lookup alongside them too (holding a shared slot while waiting for one deadlocked every probe)."""
    from concurrent.futures import ThreadPoolExecutor
    ids = [meshy.add("text-to-motion", 40 + i, status="IN_PROGRESS") for i in range(api._ACTIVE_MAX)]
    card = meshy.add("creative-lab-figure-build", 49, model_urls={"glb": _asset(meshy, "f.glb")})
    with ThreadPoolExecutor(max_workers=2) as pool:
        chip = pool.submit(api.active, tasks=",".join(f"any:{i}" for i in ids))
        state = pool.submit(api.task_state, card)
        assert len(chip.result(timeout=60)["tasks"]) == api._ACTIVE_MAX
        assert state.result(timeout=60)["has_model"]
