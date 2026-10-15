"""Multi-store provisioning (bonus): manifests, the gateway API, and the provisioner's reconcile loop
against an in-memory fake Kubernetes API (no cluster needed)."""
import asyncio, json, tempfile

import httpx, pytest
from sqlalchemy import select

from app import stores as S
from app.config import stores_cfg
from app.db import make_engine, make_sessionmaker
from app.kube import KubeError
from app.main import create_app
from app.models import Server, Store
from app.security import Vault
from .test_gateway_flow import Thread, free_port, login
from .test_standout import settings

CFG = {"enabled": True, "namespacePrefix": "store-", "domain": "shops.test", "scheme": "https", "maxPerUser": 2,
       "provisionTimeoutSeconds": 600, "gatewayNamespace": "urumi", "reservedSlugs": ["app", "store"],
       "images": {"wordpress": "wordpress:6.7-apache", "wpcli": "wordpress:cli", "mariadb": "mariadb:11.4",
                  "wooMcp": "urumi/woo-mcp:1", "pullPolicy": "IfNotPresent"},
       "store": {"woocommerceVersion": "10.3.0", "currency": "INR", "country": "IN", "timezone": "Asia/Kolkata"},
       "ingress": {"className": "traefik", "controllerNamespace": "kube-system", "tls": {"enabled": True, "clusterIssuer": "le"}},
       "quota": {"requestsCpu": "1", "requestsMemory": "1Gi"},
       "resources": {"wooMcp": {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "128Mi"}}}}
FILES = {"wp-setup.sh": "#!/bin/sh\necho setup", "create-api-keys.php": "<?php echo 1;"}


# ------------------------------------------------------------------------------------------------ manifests
def test_manifests_namespace_guardrails_isolation():
    sec = S.new_secrets()
    ms = S.build_manifests(CFG, slug="shop-a", name="Shop A", owner_email="o@x.io", secret_values=sec, files=FILES)
    kinds = [m["kind"] for m in ms]
    assert kinds[0] == "Namespace" and {"ResourceQuota", "LimitRange", "Secret", "StatefulSet", "Deployment", "Ingress"} <= set(kinds)
    ns = ms[0]
    assert ns["metadata"]["name"] == "store-shop-a" and ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "baseline"
    assert all(m["metadata"].get("namespace") == "store-shop-a" for m in ms[1:])
    # secrets appear ONLY in the Secret object; everything else references it
    blob = json.dumps([m for m in ms if m["kind"] != "Secret"])
    assert not any(v in blob for v in sec.values())
    pols = {m["metadata"]["name"]: m["spec"] for m in ms if m["kind"] == "NetworkPolicy"}
    assert pols["default-deny"]["policyTypes"] == ["Ingress", "Egress"]
    mcp_in = pols["woo-mcp"]["ingress"][0]["from"][0]
    assert mcp_in["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "urumi"}       # only the gateway
    assert mcp_in["podSelector"]["matchLabels"] == {"app.kubernetes.io/component": "gateway"}
    mcp_out = pols["woo-mcp"]["egress"][1]["to"][0]
    assert "namespaceSelector" not in mcp_out          # same-namespace WordPress only: cannot reach other stores
    assert pols["mariadb"]["ingress"][0]["from"] == [{"podSelector": {"matchLabels": S._sel("wordpress")}}]
    wp_loop = pols["wordpress"]["egress"][-1]           # loopback to its own URL goes via the ingress controller only
    assert wp_loop["to"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}]
    assert pols["acme-http01-solver"]["podSelector"]["matchLabels"] == {"acme.cert-manager.io/http01-solver": "true"}  # TLS on
    ing = next(m for m in ms if m["kind"] == "Ingress")
    assert ing["spec"]["rules"][0]["host"] == "shop-a.shops.test" and ing["spec"]["tls"][0]["hosts"] == ["shop-a.shops.test"]
    quota = next(m for m in ms if m["kind"] == "ResourceQuota")["spec"]["hard"]
    assert quota["requests.cpu"] == "1" and quota["services.loadbalancers"] == "0"
    for d in (m for m in ms if m["kind"] in ("Deployment", "StatefulSet")):
        pod = d["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        for c in pod["containers"]:
            assert c["securityContext"]["allowPrivilegeEscalation"] is False and "ALL" in c["securityContext"]["capabilities"]["drop"]
            assert c.get("readinessProbe") or c["name"] == "wp-setup"
    wp_setup = next(c for m in ms if m["kind"] == "Deployment" for c in m["spec"]["template"]["spec"]["containers"] if c["name"] == "wp-setup")
    assert {"name": "WC_VERSION", "value": "10.3.0"} in wp_setup["env"]


def test_slug_rules():
    for bad in ["", "-x", "UPPER", "a" * 21, "app", "has_underscore"]:
        with pytest.raises(S.StoreError):
            S.validate_slug(bad, CFG)
    S.validate_slug("shop-b2", CFG)


# ------------------------------------------------------------------------------------------------ fake k8s
class FakeKube:
    def __init__(self):
        self.objs: dict[tuple, dict] = {}
        self.ready: set[str] = set()          # namespaces whose workloads report Ready
        self.terminating: set[str] = set()
        self.fail_apply: str | None = None
        self.pods: dict[str, list] = {}

    async def apply(self, m):
        if self.fail_apply:
            raise KubeError(403, self.fail_apply)
        self.objs[(m["kind"], m["metadata"].get("namespace"), m["metadata"]["name"])] = m

    async def get(self, kind, ns, name):
        o = self.objs.get((kind, ns, name))
        if o and kind in ("Deployment", "StatefulSet"):
            o = {**o, "status": {"readyReplicas": 1 if ns in self.ready else 0}}
        return o

    async def list(self, kind, ns, **_):
        if kind == "Pod":
            return self.pods.get(ns, [])
        return [{"type": "Warning", "reason": "FailedCreate", "message": "exceeded quota: store-quota", "lastTimestamp": "1"}] \
            if kind == "Event" and ns in self.pods else []

    async def delete(self, kind, ns, name):
        if (kind, ns, name) not in self.objs:
            return False
        if kind == "Namespace":
            if name in self.terminating:      # second loop: namespace finished terminating
                for k in [k for k in self.objs if k[1] == name or k == (kind, ns, name)]:
                    del self.objs[k]
            else:
                self.terminating.add(name)
        return True


@pytest.fixture()
def stack():
    db, port = tempfile.mkdtemp(), free_port()
    s = settings(db, stores={**CFG})
    app = create_app(s)
    gw = Thread(app, port)
    base = f"http://127.0.0.1:{port}"
    admin = login(base, "admin@t.io", "adminpass1")
    clock = {"t": 1_000.0}
    sm = make_sessionmaker(make_engine(s.database_url))
    kube = FakeKube()
    prov = lambda: S.Provisioner(stores_cfg(s), sm, Vault(s.encryption_key), kube, FILES, clock=lambda: clock["t"])  # noqa: E731
    yield {"base": base, "admin": admin, "sm": sm, "kube": kube, "prov": prov, "clock": clock, "db": db, "key": s.encryption_key}
    gw.stop()


def rows(sm):
    async def f():
        async with sm() as db:
            return {r.slug: r for r in (await db.scalars(select(Store))).all()}
    return asyncio.run(f())


def servers(sm):
    async def f():
        async with sm() as db:
            return {r.slug: r for r in (await db.scalars(select(Server))).all()}
    return asyncio.run(f())


def _deadline_to(sm, slug, t):
    async def f():
        async with sm() as db:
            r = await db.scalar(select(Store).where(Store.slug == slug))
            r.deadline = t
            await db.commit()
    asyncio.run(f())


def test_store_lifecycle_registration_failure_retry_teardown(stack):
    A, kube, sm, clock = stack["admin"], stack["kube"], stack["sm"], stack["clock"]
    r = A.post("/api/stores", json={"name": "Shop A"})
    assert r.status_code == 201 and r.json()["status"] == "provisioning" and r.json()["url"] == "https://shop-a.shops.test"
    assert A.post("/api/stores", json={"name": "Shop A"}).status_code == 409                       # duplicate
    assert A.post("/api/stores", json={"name": "x", "slug": "app"}).status_code == 400              # reserved
    assert A.post("/api/stores", json={"name": "Shop B"}).status_code == 201
    assert "limit" in A.post("/api/stores", json={"name": "Shop C"}).json()["detail"]              # max per user
    _deadline_to(sm, "shop-a", int(clock["t"]) + 600); _deadline_to(sm, "shop-b", int(clock["t"]) + 600)

    p = stack["prov"]()
    asyncio.run(p.reconcile_all())
    st = rows(sm)
    assert st["shop-a"].status == "provisioning" and st["shop-a"].reason == "starting the database"
    assert ("Namespace", None, "store-shop-a") in kube.objs and ("Namespace", None, "store-shop-b") in kube.objs  # in parallel

    kube.ready.add("store-shop-a")
    asyncio.run(stack["prov"]().reconcile_all())                    # a NEW provisioner instance: restart is harmless
    st, sv = rows(sm), servers(sm)
    assert st["shop-a"].status == "ready" and st["shop-a"].server_id == sv["shop-a"].id
    assert sv["shop-a"].url == "http://woo-mcp.store-shop-a.svc.cluster.local:8000/mcp" and sv["shop-a"].auth_type == "bearer"
    token = json.loads(Vault(stack["key"]).decrypt(st["shop-a"].secrets_enc))["mcp-token"]
    assert token not in json.dumps(A.get("/api/servers").json()) and token not in json.dumps(A.get("/api/stores").json())

    # shop-b never becomes ready: it fails at the deadline with a reason from pods/events
    kube.pods["store-shop-b"] = [{"metadata": {"name": "wordpress-7d9-abc"}, "status": {"containerStatuses": [
        {"name": "wp-setup", "ready": False, "restartCount": 0, "state": {"waiting": {"reason": "ImagePullBackOff", "message": "no such image"}}}]}}]
    clock["t"] += 601
    asyncio.run(stack["prov"]().reconcile_all())
    b = rows(sm)["shop-b"]
    assert b.status == "failed" and "Timed out" in b.reason and "ImagePullBackOff" in b.reason and "exceeded quota" in b.reason
    sid_b = A.get("/api/stores").json()["stores"][1]["id"]
    assert A.post(f"/api/stores/{sid_b}/retry").json()["status"] == "provisioning"

    # delete shop-a: tools disappear immediately (server disabled), then registration + namespace are removed
    sid_a = A.get("/api/stores").json()["stores"][0]["id"]
    assert A.delete(f"/api/stores/{sid_a}").json()["status"] == "deleting"
    assert servers(sm)["shop-a"].enabled is False
    asyncio.run(stack["prov"]().reconcile_all())
    assert "shop-a" not in servers(sm) and rows(sm)["shop-a"].reason.startswith("Deleting the namespace")
    asyncio.run(stack["prov"]().reconcile_all())
    assert "shop-a" not in rows(sm) and not [k for k in kube.objs if k[1] == "store-shop-a" or k[2] == "store-shop-a"]


def test_members_manage_only_their_own_stores_and_apply_errors_are_reported(stack):
    A, kube, sm = stack["admin"], stack["kube"], stack["sm"]
    inv = A.post("/api/users/invite", json={"email": "m@t.io", "role": "member"}).json()
    httpx.post(f"{stack['base']}/api/auth/accept-invite", json={"token": inv["invite_token"], "password": "memberpass1"})
    M = login(stack["base"], "m@t.io", "memberpass1")
    sid = A.post("/api/stores", json={"name": "Admin Shop"}).json()["id"]
    assert M.delete(f"/api/stores/{sid}").status_code == 403
    mine = M.post("/api/stores", json={"name": "Member Shop"}).json()
    assert mine["can_manage"] and M.get("/api/stores").json()["stores"][0]["can_manage"] is False
    kube.fail_apply = "admission webhook denied: only store-* namespaces"
    _deadline_to(sm, "member-shop", 10**10); _deadline_to(sm, "admin-shop", 10**10)
    asyncio.run(stack["prov"]().reconcile_all())
    assert rows(sm)["member-shop"].reason.startswith("Retrying: ") and rows(sm)["member-shop"].status == "provisioning"



def test_delete_during_reconcile_wins_over_ready(stack):
    """Optimistic locking: the provisioner read the row, the user deletes, the provisioner tries to mark it Ready."""
    A, kube, sm = stack["admin"], stack["kube"], stack["sm"]
    sid = A.post("/api/stores", json={"name": "Racy"}).json()["id"]
    _deadline_to(sm, "racy", 10**10)
    kube.ready.add("store-racy")
    orig_get = kube.get

    async def get_and_race(kind, ns, name):
        if kind == "Deployment" and name == "woo-mcp" and not getattr(kube, "raced", False):
            kube.raced = True
            assert A.delete(f"/api/stores/{sid}").json()["status"] == "deleting"   # user clicks Delete right now
        return await orig_get(kind, ns, name)
    kube.get = get_and_race
    asyncio.run(stack["prov"]().reconcile_all())          # its commit loses (StaleDataError) and is rolled back
    assert rows(sm)["racy"].status == "deleting" and "racy" not in servers(sm)
    kube.get = orig_get
    asyncio.run(stack["prov"]().reconcile_all()); asyncio.run(stack["prov"]().reconcile_all())
    assert "racy" not in rows(sm)
