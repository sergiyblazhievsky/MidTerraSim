"""Integration tests for server.py's stdlib HTTP/JSON API (/health, /state,
/save, and error handling), run against a real ThreadingHTTPServer bound to
an ephemeral localhost port."""
import json
import threading
import urllib.error
import urllib.request

import pytest

import server as server_module


@pytest.fixture
def running_server(world):
    """Spins up server.py's real HTTP handler (bound to World `world`) on an
    OS-assigned ephemeral port, and tears it down after the test."""
    handler_cls = server_module.make_handler(world)
    httpd = server_module.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", world
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _get_json(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post_json(url):
    req = urllib.request.Request(url, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post_json_payload(url, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, method="POST", data=body,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_health_returns_ok_and_current_revision(running_server):
    base_url, world = running_server

    status, body = _get_json(f"{base_url}/health")

    assert status == 200
    assert body["status"] == "ok"
    assert body["revision"] == world.revision


def test_state_returns_a_full_snapshot(running_server):
    base_url, world = running_server

    status, body = _get_json(f"{base_url}/state")

    assert status == 200
    assert body["chunk"]["size"] == [world.sx, world.sy, world.sz]
    assert "creatures" in body and "vegetation" in body and "drops" in body
    assert "structures" in body and "structure_revision" in body


def test_state_response_has_json_content_type(running_server):
    base_url, _ = running_server
    with urllib.request.urlopen(f"{base_url}/state", timeout=5) as resp:
        assert resp.headers.get("Content-Type") == "application/json"


def test_root_serves_the_html_inspector_page(running_server):
    base_url, _ = running_server
    with urllib.request.urlopen(f"{base_url}/", timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "text/html; charset=utf-8"
        body = resp.read().decode("utf-8")

    assert "<html" in body.lower()
    assert "Server Inspector" in body
    assert "/state" in body  # the page polls the JSON API client-side
    assert "/admin/speed_multiplier" in body  # the admin panel posts here
    # grass patches cover most of the map; the inspector deliberately
    # hides them so flowers/bushes/trees stay readable
    assert "v.type !== 'grass'" in body


def test_admin_get_returns_default_speed_multiplier(running_server):
    base_url, world = running_server

    status, body = _get_json(f"{base_url}/admin")

    assert status == 200
    assert body == {"speed_multiplier": 1}


def test_admin_post_speed_multiplier_applies_and_persists_in_world(running_server):
    base_url, world = running_server

    status, body = _post_json_payload(f"{base_url}/admin/speed_multiplier", {"value": 25})

    assert status == 200
    assert body == {"speed_multiplier": 25}
    with world.lock:
        assert world.speed_multiplier == 25

    # subsequent GET /admin reflects the change
    _, admin_body = _get_json(f"{base_url}/admin")
    assert admin_body == {"speed_multiplier": 25}


def test_admin_post_speed_multiplier_rejects_out_of_range_value(running_server):
    base_url, world = running_server

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _post_json_payload(f"{base_url}/admin/speed_multiplier", {"value": 101})

    assert exc_info.value.code == 400
    body = json.loads(exc_info.value.read().decode("utf-8"))
    assert "error" in body
    with world.lock:
        assert world.speed_multiplier == 1  # unchanged


def test_admin_post_speed_multiplier_rejects_missing_value_field(running_server):
    base_url, _ = running_server

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _post_json_payload(f"{base_url}/admin/speed_multiplier", {})

    assert exc_info.value.code == 400


def test_admin_post_speed_multiplier_rejects_empty_body(running_server):
    base_url, _ = running_server
    req = urllib.request.Request(f"{base_url}/admin/speed_multiplier", method="POST", data=b"")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)

    assert exc_info.value.code == 400


def test_admin_post_speed_multiplier_rejects_malformed_json_body(running_server):
    base_url, _ = running_server
    req = urllib.request.Request(
        f"{base_url}/admin/speed_multiplier", method="POST",
        data=b"{not valid json", headers={"Content-Type": "application/json"},
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)

    assert exc_info.value.code == 400


def test_save_forces_a_write_to_the_world_file(running_server, isolated_paths):
    base_url, world = running_server
    world_path = isolated_paths["world_path"]
    world_path.unlink()  # remove the pre-existing fixture file to prove /save recreates it
    assert not world_path.exists()

    status, body = _post_json(f"{base_url}/save")

    assert status == 200
    assert body["saved"] is True
    assert world_path.exists()


def test_unknown_route_returns_404_with_json_error_body(running_server):
    base_url, _ = running_server

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base_url}/nope", timeout=5)

    assert exc_info.value.code == 404
    body = json.loads(exc_info.value.read().decode("utf-8"))
    assert body == {"error": "not found"}


def test_wrong_method_on_a_known_route_returns_404(running_server):
    # /save only handles POST; GET /save must not be treated as a valid route.
    base_url, _ = running_server

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base_url}/save", timeout=5)

    assert exc_info.value.code == 404


def test_post_to_unknown_route_returns_404(running_server):
    base_url, _ = running_server
    req = urllib.request.Request(f"{base_url}/nope", method="POST", data=b"")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)

    assert exc_info.value.code == 404


def test_unsupported_http_method_returns_501(running_server):
    base_url, _ = running_server
    req = urllib.request.Request(f"{base_url}/health", method="DELETE")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)

    assert exc_info.value.code == 501


def test_state_revision_advances_after_a_manual_tick(running_server):
    base_url, world = running_server
    _, before = _get_json(f"{base_url}/state")

    with world.lock:
        world.tick(0.01)

    _, after = _get_json(f"{base_url}/state")
    assert after["revision"] > before["revision"]


def test_server_keeps_serving_after_many_concurrent_requests(running_server):
    """The server must remain responsive under concurrent access, and a
    client disconnecting mid-poll must not affect subsequent requests --
    exactly the behavior the client/server split relies on."""
    base_url, _ = running_server
    errors = []

    def worker():
        try:
            for _ in range(5):
                _get_json(f"{base_url}/state")
                _get_json(f"{base_url}/health")
        except Exception as exc:  # pragma: no cover - failure path, reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    # the server must still answer after the concurrent burst finishes
    status, body = _get_json(f"{base_url}/health")
    assert status == 200
    assert body["status"] == "ok"
