"""Run inside the API container after deleting only lab:product:1."""

import json
from urllib.error import HTTPError
from urllib.request import urlopen

BASE = "http://127.0.0.1:8000"


def get(path):
    with urlopen(BASE + path, timeout=5) as response:
        return response.headers, json.load(response)


assert get("/health/live")[1]["status"] == "ok"
assert get("/health/ready")[1]["redis"] == "ok"
headers, first = get("/products/1")
assert headers["X-Cache"] == "MISS", headers
assert headers["X-Request-ID"]
headers, second = get("/products/1")
assert headers["X-Cache"] == "HIT", headers
assert first == second and first["id"] == 1
try:
    get("/products/999")
except HTTPError as exc:
    assert exc.code == 404
else:
    raise AssertionError("Missing product should return 404")
print("PASS: liveness, readiness, cache MISS -> HIT, product data, request ID, 404")
