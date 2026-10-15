import json
"""Hosted-server manifests and validation: pure functions, no cluster needed."""
import asyncio

from app import hosted

ALLOW = ["ghcr.io/acme/", "urumi/"]


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except hosted.HostedError as e:
        return str(e)
    raise AssertionError("expected HostedError")


def test_validation():
    hosted.validate_request("ghcr.io/acme/tool:1.2", 8000, "/mcp", {"API_KEY": "x"}, ALLOW)
    assert "allowed" in raises(hosted.validate_request, "ghcr.io/acme/x", 8000, "/mcp", {}, [])
    assert "must start" in raises(hosted.validate_request, "evil.io/x:1", 8000, "/mcp", {}, ALLOW)
    assert "Invalid image" in raises(hosted.validate_request, "ghcr.io/acme/x; rm -rf /", 8000, "/mcp", {}, ALLOW)
    assert "port" in raises(hosted.validate_request, "urumi/x:1", 80, "/mcp", {}, ALLOW)
    assert "not allowed" in raises(hosted.validate_request, "urumi/x:1", 8000, "/mcp", {"KUBERNETES_SERVICE_HOST": "x"}, ALLOW)
    assert "not allowed" in raises(hosted.validate_request, "urumi/x:1", 8000, "/mcp", {"bad name": "x"}, ALLOW)
    assert "path" in raises(hosted.validate_request, "urumi/x:1", 8000, "mcp", {}, ALLOW).lower()


def test_manifests_are_locked_down():
    ms = dict(hosted.build_manifests(ns="urumi", slug="crm", image="urumi/crm:1", port=8000, env={"TOKEN": "s3"}))
    assert list(ms) == ["Secret", "NetworkPolicy", "Deployment", "Service"]
    assert ms["Secret"]["stringData"] == {"TOKEN": "s3"}
    pod = ms["Deployment"]["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["resources"]["limits"] and c["resources"]["requests"]
    assert c["readinessProbe"] and c["livenessProbe"]
    assert "value" not in str(c["envFrom"]) and c["envFrom"][0]["secretRef"]["name"] == "hosted-crm"  # secret not inlined
    np = ms["NetworkPolicy"]["spec"]
    assert np["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == {"app.kubernetes.io/component": "gateway"}
    internet = [r for r in np["egress"] if "ipBlock" in r["to"][0]][0]["to"][0]["ipBlock"]
    assert "10.0.0.0/8" in internet["except"] and "169.254.0.0/16" in internet["except"]
    assert ms["Service"]["spec"]["type"] == "ClusterIP"
    for m in ms.values():  # everything is labelled so it can be found and removed
        assert m["metadata"]["labels"]["urumi.io/hosted"] == "crm"
    assert hosted.internal_url("crm", "urumi", 8000, "/mcp") == "http://hosted-crm.urumi.svc.cluster.local:8000/mcp"


def test_kube_client_apply_and_idempotent_teardown():
    import httpx
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, req.headers.get("content-type")))
        return httpx.Response(404 if req.method == "DELETE" else 200, json={})

    async def run():
        kube = hosted.KubeClient("urumi", api="https://k8s.test", transport=httpx.MockTransport(handler))
        await hosted.deploy(kube, hosted.build_manifests(ns="urumi", slug="crm", image="urumi/crm:1", port=8000, env={}))
        await hosted.teardown(kube, "crm")  # 404s must be tolerated
        await kube.aclose()

    asyncio.run(run())
    applies = [c for c in calls if c[0] == "PATCH"]
    assert [c[1].split("/")[-2] for c in applies] == ["secrets", "networkpolicies", "deployments", "services"]
    assert all(c[2] == "application/apply-patch+yaml" for c in applies)
    assert [c[1].split("/")[-2] for c in calls if c[0] == "DELETE"] == ["services", "deployments", "networkpolicies", "secrets"]


def test_hosted_server_reports_starting_then_unreachable():
    import asyncio, time
    from cryptography.fernet import Fernet
    from app.config import Settings
    from app.security import Vault
    from app.upstream import ServerCfg, UpstreamManager, UpstreamState
    up = UpstreamManager(Settings(ssrf_allow_private=True, upstream_list_timeout=2), Vault(Fernet.generate_key().decode()), None)
    cfg = ServerCfg(1, "h", "H", "http://127.0.0.1:1/mcp", "none", None, None, True)
    up.state["h"] = UpstreamState(starting_until=time.time() + 60)
    assert asyncio.run(up.refresh(cfg)).status == "starting"   # pod still booting: not reported as dead
    up.state["h"].starting_until = 0
    assert asyncio.run(up.refresh(cfg)).status == "unreachable"


def test_llm_schema_is_provider_friendly():
    from app.chat_agent import llm_schema
    s = {"type": "object", "title": "list_productsArguments", "properties": {
        "search": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Search"},
        "status": {"type": "string", "enum": ["publish", "draft"], "title": "Status"}}, "required": []}
    out = llm_schema(s)
    assert "title" not in json.dumps(out)
    assert out["properties"]["search"] == {"type": "string", "default": None}
    assert out["properties"]["status"]["enum"] == ["publish", "draft"]
    assert llm_schema({"type": "object"}) == {"type": "object", "properties": {}}


def _fake_svc(settings):
    from mcp import types
    from app.service import ToolEntry

    class Svc:
        s = settings
        calls = []
        async def list_tools(self, user):
            return [ToolEntry("shop__list_orders", "shop", "Shop", "list_orders", "[Shop] list orders", {"type": "object", "properties": {}})]
        async def call_tool(self, user, name, args, source):
            self.calls.append(name)
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"id": 7, "items": [1, 2]}, indent=2))])
    return Svc()


def test_chat_waits_out_a_429_then_answers_and_compacts_results():
    import asyncio, httpx
    from app.config import Settings
    from app.chat_agent import run_chat
    from app.service import Principal
    seen = {"n": 0, "tool_msg": None}

    def llm(req):
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset-tokens": "0.05s"}, json={"error": "slow down"})
        body = json.loads(req.content)
        if not any(m["role"] == "tool" for m in body["messages"]):
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "shop__list_orders", "arguments": "{}"}}]}}]})
        seen["tool_msg"] = next(m["content"] for m in body["messages"] if m["role"] == "tool")
        return httpx.Response(200, json={"choices": [{"message": {"content": "One order: #7"}}]})

    s = Settings(llm_api_key="k", llm_base_url="http://llm/v1")
    svc = _fake_svc(s)
    client = httpx.AsyncClient(transport=httpx.MockTransport(llm))
    out = asyncio.run(run_chat(s, svc, Principal(1, "a@x.io", "admin"), [{"role": "user", "content": "orders?"}], client=client))
    assert out["reply"] == "One order: #7" and svc.calls == ["shop__list_orders"] and seen["n"] == 3
    assert seen["tool_msg"] == '{"id":7,"items":[1,2]}'          # minified before it goes to the model


def test_chat_gives_a_clear_rate_limit_error_when_the_wait_is_too_long():
    import asyncio, httpx, pytest
    from app.config import Settings
    from app.chat_agent import ChatError, run_chat
    from app.service import Principal
    s = Settings(llm_api_key="k", llm_base_url="http://llm/v1")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "120"})))
    with pytest.raises(ChatError) as e:
        asyncio.run(run_chat(s, _fake_svc(s), Principal(1, "a@x.io", "admin"), [{"role": "user", "content": "hi"}], client=client))
    assert e.value.status == 429 and "rate limit" in str(e.value) and "121 seconds" in str(e.value)


def test_chat_recovers_when_the_provider_rejects_a_tool_call():
    import asyncio, httpx
    from app.config import Settings
    from app.chat_agent import run_chat
    from app.service import Principal
    seen = {"n": 0, "hint": None}

    def llm(req):
        seen["n"] += 1
        body = json.loads(req.content)
        if seen["n"] == 1:  # Groq-style rejection of a malformed call
            return httpx.Response(400, json={"error": {"code": "tool_use_failed", "message": "missing properties: 'title'"}})
        seen["hint"] = seen["hint"] or next((m["content"] for m in body["messages"] if m["role"] == "system" and "rejected" in m["content"]), None)
        if not any(m["role"] == "tool" for m in body["messages"]):
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "shop__list_orders", "arguments": "{}"}}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    s = Settings(llm_api_key="k", llm_base_url="http://llm/v1")
    out = asyncio.run(run_chat(s, _fake_svc(s), Principal(1, "a@x.io", "admin"), [{"role": "user", "content": "x"}],
                               client=httpx.AsyncClient(transport=httpx.MockTransport(llm))))
    assert out["reply"] == "done" and "missing properties: 'title'" in seen["hint"]


def test_tool_retry_hint_names_the_required_arguments():
    from app.chat_agent import _tool_retry_hint
    tools = [{"type": "function", "function": {"name": "notes__add_note", "parameters": {
        "type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}}, "required": ["title", "body"]}}}]
    h = _tool_retry_hint("parameters for tool notes__add_note did not match schema: missing properties: 'title'", tools)
    assert "include every required argument: title, body" in h and "title (string, required)" in h


def test_system_prompt_lists_every_store():
    from app.chat_agent import _stores_in
    tools = [{"type": "function", "function": {"name": n, "description": d}} for n, d in [
        ("woocommerce__list_orders", "[WooCommerce Store] List orders"), ("chai-corner__list_orders", "[Store: Chai Corner] List orders"),
        ("notes__add_note", "[Team Notes] Save a note")]]
    assert _stores_in(tools) == [("woocommerce", "WooCommerce Store"), ("chai-corner", "Store: Chai Corner")]
