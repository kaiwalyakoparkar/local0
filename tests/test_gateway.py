"""Gateway API-definition builder — pure-function checks, no live gateway.

The deploy sequence needs a live APIM to exercise, but the highest-value
assertion is that the generated import envelope carries the right shape: the
router endpoint, a keyless plan, and the 424→reroute flow that the whole
escalation contract depends on.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.gateway import Provider, build_api_definition  # noqa: E402

ROUTER = "http://router-service:8081"
FALLBACK = Provider(name="big", base_url="https://cloud.example/v1",
                    api_key="sk-test", model="big-model")


def test_path_stable():
    _, path = build_api_definition(ROUTER, FALLBACK)
    assert path == "/local0/"


def test_router_endpoint_present():
    env, _ = build_api_definition(ROUTER, FALLBACK)
    target = env["api"]["endpointGroups"][0]["endpoints"][0]["configuration"]["target"]
    assert target == ROUTER


def test_keyless_plan():
    env, _ = build_api_definition(ROUTER, FALLBACK)
    assert env["plans"][0]["security"]["type"] == "KEY_LESS"


def test_424_reroute_condition_and_fallback():
    env, _ = build_api_definition(ROUTER, FALLBACK)
    blob = json.dumps(env)
    # The escalation contract: reroute is gated on upstream 424, and the callout
    # targets the cloud fallback.
    assert "{#response.status == 424}" in blob
    assert "cloud.example" in blob


def test_api_key_not_in_url():
    # The key rides an Authorization header, never the target URL.
    env, _ = build_api_definition(ROUTER, FALLBACK)
    target = env["api"]["endpointGroups"][0]["endpoints"][0]["configuration"]["target"]
    assert "sk-test" not in target
