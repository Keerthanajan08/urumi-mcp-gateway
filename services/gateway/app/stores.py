"""Multi-store provisioning (bonus): one WooCommerce store per Kubernetes namespace.

Desired state is a row in the `stores` table (written by the gateway API). The store provisioner
(app/provisioner.py, its own Deployment + ServiceAccount) reconciles every row into Kubernetes:

  provisioning --(all workloads Ready)--> ready      (its WooCommerce MCP server is registered in the gateway)
       |  \\--(deadline passed)--------> failed     (reason taken from pod states / warning events)
  deleting ----(namespace gone)--------> row removed (registration removed first, so tools vanish at once)

Per store namespace `store-<slug>`:
  Namespace (Pod Security "baseline" enforced), ResourceQuota, LimitRange, Secret, ConfigMap (bootstrap scripts),
  NetworkPolicies (default deny), MariaDB StatefulSet + PVC, WordPress Deployment + PVC + setup sidecar,
  WooCommerce MCP Deployment, Services, Ingress host `<slug>.<domain>`.

Why it is safe to retry / restart
  Every write is a server-side apply of a fully specified object (idempotent). Generated secrets are created once
  and persisted (encrypted) in the row, so re-applying never rotates them. The deadline is persisted too, so a
  provisioner that restarts mid-provisioning simply resumes: the store either becomes ready or fails with a reason.

Isolation
  A store's MCP pod may talk only to its own WordPress; its WordPress only to its own MariaDB (and the internet
  for plugin downloads); only the gateway pods may call its MCP server. Nothing crosses store namespaces.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone

from sqlalchemy import delete, select

from .kube import Kube, KubeError
from .models import Server, Store, ToolPolicy
from .security import Vault

log = logging.getLogger("provisioner")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?$")   # 20 chars: "store-" + slug stays a short DNS label
PART_OF = "urumi-store"


class StoreError(ValueError):
    pass


# ----------------------------------------------------------------------------------------------- naming / secrets
def namespace_for(cfg: dict, slug: str) -> str:
    return f"{cfg.get('namespacePrefix', 'store-')}{slug}"


def host_for(cfg: dict, slug: str) -> str:
    return f"{slug}.{cfg['domain']}"


def url_for(cfg: dict, slug: str) -> str:
    return f"{cfg.get('scheme', 'http')}://{host_for(cfg, slug)}"


def mcp_url(cfg: dict, slug: str) -> str:
    return f"http://woo-mcp.{namespace_for(cfg, slug)}.svc.cluster.local:8000/mcp"


def validate_slug(slug: str, cfg: dict) -> None:
    if not SLUG_RE.match(slug or ""):
        raise StoreError("Store short name must be 1-20 characters: lowercase letters, digits and hyphens.")
    if slug in set(cfg.get("reservedSlugs", [])):
        raise StoreError(f"'{slug}' is reserved.")


def new_secrets() -> dict[str, str]:
    t = lambda n=24: secrets.token_urlsafe(n)  # noqa: E731
    return {"mariadb-root-password": t(), "mariadb-password": t(), "wp-admin-password": t(18),
            "wc-consumer-key": "ck_" + secrets.token_hex(20), "wc-consumer-secret": "cs_" + secrets.token_hex(20),
            "mcp-token": t(32)}


# ----------------------------------------------------------------------------------------------- manifests
def _labels(slug: str, component: str | None = None) -> dict:
    d = {"app.kubernetes.io/part-of": PART_OF, "app.kubernetes.io/managed-by": "urumi-store-provisioner",
         "urumi.io/store": slug}
    if component:
        d.update({"app.kubernetes.io/component": component, "app.kubernetes.io/name": component})
    return d


def _sel(component: str) -> dict:
    return {"app.kubernetes.io/component": component, "app.kubernetes.io/part-of": PART_OF}


def _secret_env(name: str, key: str) -> dict:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": "store-secrets", "key": key}}}


_POD_SEC = {"seccompProfile": {"type": "RuntimeDefault"}}
_DROP_ALL = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
_PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"]


def _dns_egress() -> dict:
    return {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}


def build_manifests(cfg: dict, *, slug: str, name: str, owner_email: str, secret_values: dict,
                    files: dict[str, str]) -> list[dict]:
    """Every object for one store, in apply order. Pure function: unit-tested without a cluster."""
    ns = namespace_for(cfg, slug)
    img = cfg["images"]
    pull = img.get("pullPolicy", "IfNotPresent")
    st = cfg.get("store", {})
    res = cfg.get("resources", {})
    q = cfg.get("quota", {})
    storage = cfg.get("storage", {})
    sc = {"storageClassName": storage["className"]} if storage.get("className") else {}
    ing = cfg.get("ingress", {})
    meta = lambda n, comp=None: {"name": n, "namespace": ns, "labels": _labels(slug, comp)}  # noqa: E731
    site_url = url_for(cfg, slug)
    db_env = [{"name": "WORDPRESS_DB_HOST", "value": "mariadb:3306"}, {"name": "WORDPRESS_DB_NAME", "value": "wordpress"},
              {"name": "WORDPRESS_DB_USER", "value": "wordpress"}, _secret_env("WORDPRESS_DB_PASSWORD", "mariadb-password")]
    out: list[dict] = []

    # --- namespace + guardrails
    out.append({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {
        **_labels(slug), "pod-security.kubernetes.io/enforce": "baseline", "pod-security.kubernetes.io/warn": "baseline"}}})
    out.append({"apiVersion": "v1", "kind": "ResourceQuota", "metadata": meta("store-quota"), "spec": {"hard": {
        "requests.cpu": q.get("requestsCpu", "1"), "requests.memory": q.get("requestsMemory", "1536Mi"),
        "limits.cpu": q.get("limitsCpu", "4"), "limits.memory": q.get("limitsMemory", "3Gi"),
        "pods": str(q.get("pods", 6)), "persistentvolumeclaims": str(q.get("pvcs", 3)),
        "requests.storage": q.get("storage", "10Gi"), "services.loadbalancers": "0", "services.nodeports": "0"}}})
    out.append({"apiVersion": "v1", "kind": "LimitRange", "metadata": meta("store-limits"), "spec": {"limits": [{
        "type": "Container", "default": {"cpu": "250m", "memory": "256Mi"},
        "defaultRequest": {"cpu": "25m", "memory": "64Mi"}, "max": {"cpu": "2", "memory": "2Gi"}}]}})
    out.append({"apiVersion": "v1", "kind": "Secret", "metadata": meta("store-secrets"), "type": "Opaque",
                "stringData": dict(secret_values)})
    out.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("wp-setup", "wordpress"), "data": dict(files)})

    # --- network policies: default deny, then exactly the paths a store needs
    gw_ns = cfg["gatewayNamespace"]
    pols = [
        ("default-deny", {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "egress": [_dns_egress()]}),
        ("wordpress", {"podSelector": {"matchLabels": _sel("wordpress")}, "policyTypes": ["Ingress", "Egress"],
                       "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                           "kubernetes.io/metadata.name": ing.get("controllerNamespace", "kube-system")}}},
                           {"podSelector": {"matchLabels": _sel("woo-mcp")}}], "ports": [{"protocol": "TCP", "port": 80}]}],
                       "egress": [_dns_egress(),
                                  {"to": [{"podSelector": {"matchLabels": _sel("mariadb")}}], "ports": [{"protocol": "TCP", "port": 3306}]},
                                  {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": _PRIVATE}}],
                                   "ports": [{"protocol": "TCP", "port": 80}, {"protocol": "TCP", "port": 443}]},
                                  # WordPress calls its own public URL (wp-cron, WooCommerce Action Scheduler,
                                  # Site Health loopback); in-cluster DNS sends that to the ingress controller
                                  {"to": [{"namespaceSelector": {"matchLabels": {
                                      "kubernetes.io/metadata.name": ing.get("controllerNamespace", "kube-system")}}}],
                                   "ports": [{"protocol": "TCP", "port": p} for p in (80, 443, 8000, 8443)]}]}),
        ("mariadb", {"podSelector": {"matchLabels": _sel("mariadb")}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"podSelector": {"matchLabels": _sel("wordpress")}}],
                                  "ports": [{"protocol": "TCP", "port": 3306}]}]}),
        ("woo-mcp", {"podSelector": {"matchLabels": _sel("woo-mcp")}, "policyTypes": ["Ingress", "Egress"],
                     "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": gw_ns}},
                                            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}}}],
                                  "ports": [{"protocol": "TCP", "port": 8000}]}],
                     "egress": [_dns_egress(), {"to": [{"podSelector": {"matchLabels": _sel("wordpress")}}],
                                                "ports": [{"protocol": "TCP", "port": 80}]}]}),
    ]
    if ing.get("tls", {}).get("enabled"):
        # cert-manager's temporary HTTP-01 solver pod must be reachable by the ingress controller, or the store's
        # Let's Encrypt certificate is never issued (default deny above)
        pols.append(("acme-http01-solver", {
            "podSelector": {"matchLabels": {"acme.cert-manager.io/http01-solver": "true"}}, "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": ing.get("controllerNamespace", "kube-system")}}}],
                "ports": [{"protocol": "TCP", "port": 8089}]}]}))
    for n, spec in pols:
        out.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(n), "spec": spec})

    # --- MariaDB
    out.append({"apiVersion": "v1", "kind": "Service", "metadata": meta("mariadb", "mariadb"),
                "spec": {"clusterIP": "None", "selector": _sel("mariadb"), "ports": [{"name": "mysql", "port": 3306}]}})
    out.append({"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": meta("mariadb", "mariadb"), "spec": {
        "serviceName": "mariadb", "replicas": 1, "selector": {"matchLabels": _sel("mariadb")},
        "template": {"metadata": {"labels": _labels(slug, "mariadb")}, "spec": {
            "automountServiceAccountToken": False, "enableServiceLinks": False,
            "securityContext": {**_POD_SEC, "runAsNonRoot": True, "runAsUser": 999, "runAsGroup": 999, "fsGroup": 999},
            "containers": [{
                "name": "mariadb", "image": img["mariadb"], "imagePullPolicy": pull,
                "args": ["--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci"],
                "ports": [{"name": "mysql", "containerPort": 3306}],
                "env": [{"name": "MARIADB_DATABASE", "value": "wordpress"}, {"name": "MARIADB_USER", "value": "wordpress"},
                        _secret_env("MARIADB_PASSWORD", "mariadb-password"),
                        _secret_env("MARIADB_ROOT_PASSWORD", "mariadb-root-password")],
                "startupProbe": {"tcpSocket": {"port": 3306}, "periodSeconds": 5, "failureThreshold": 60},
                "readinessProbe": {"exec": {"command": ["sh", "-c", 'mariadb-admin ping -h 127.0.0.1 -uroot -p"$MARIADB_ROOT_PASSWORD" --silent']},
                                   "periodSeconds": 10, "timeoutSeconds": 5},
                "livenessProbe": {"tcpSocket": {"port": 3306}, "periodSeconds": 20, "failureThreshold": 6},
                "resources": res.get("mariadb", {}), "securityContext": _DROP_ALL,
                "volumeMounts": [{"name": "data", "mountPath": "/var/lib/mysql"}]}]}},
        "volumeClaimTemplates": [{"metadata": {"name": "data", "labels": _labels(slug, "mariadb")}, "spec": {
            "accessModes": ["ReadWriteOnce"], **sc, "resources": {"requests": {"storage": storage.get("mariadb", "2Gi")}}}}]}})

    # --- WordPress + WooCommerce (same bootstrap sidecar as the main store)
    out.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta("wordpress-data", "wordpress"),
                "spec": {"accessModes": ["ReadWriteOnce"], **sc, "resources": {"requests": {"storage": storage.get("wordpress", "2Gi")}}}})
    out.append({"apiVersion": "v1", "kind": "Service", "metadata": meta("wordpress", "wordpress"),
                "spec": {"selector": _sel("wordpress"), "ports": [{"name": "http", "port": 80, "targetPort": 80}]}})
    config_extra = (f"define('WP_HOME', '{site_url}');\ndefine('WP_SITEURL', '{site_url}');\ndefine('DISALLOW_FILE_EDIT', true);\n"
                    "if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') { $_SERVER['HTTPS'] = 'on'; }\n")
    out.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("wordpress", "wordpress"), "spec": {
        "replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": _sel("wordpress")},
        "template": {"metadata": {"labels": _labels(slug, "wordpress")}, "spec": {
            "automountServiceAccountToken": False, "enableServiceLinks": False, "securityContext": _POD_SEC,
            "containers": [
                {"name": "wordpress", "image": img["wordpress"], "imagePullPolicy": pull,
                 "command": ["/bin/sh", "-c"],
                 "args": ["php -r 'include \"/usr/src/wordpress/wp-includes/version.php\"; echo $wp_version;' > "
                          "/var/www/html/.image-wp-version; exec docker-entrypoint.sh apache2-foreground"],
                 "ports": [{"name": "http", "containerPort": 80}],
                 "env": [*db_env, {"name": "WORDPRESS_CONFIG_EXTRA", "value": config_extra}],
                 "startupProbe": {"tcpSocket": {"port": 80}, "periodSeconds": 5, "failureThreshold": 60},
                 "readinessProbe": {"exec": {"command": ["test", "-f", "/var/www/html/.setup-complete"]}, "periodSeconds": 10},
                 "livenessProbe": {"tcpSocket": {"port": 80}, "periodSeconds": 20, "failureThreshold": 6},
                 "resources": res.get("wordpress", {}),
                 "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {
                     "drop": ["ALL"], "add": ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER", "NET_BIND_SERVICE", "KILL"]}},
                 "volumeMounts": [{"name": "wp-data", "mountPath": "/var/www/html"}]},
                {"name": "wp-setup", "image": img["wpcli"], "imagePullPolicy": pull,
                 "command": ["/bin/sh", "/scripts/wp-setup.sh"],
                 "env": [*db_env,
                         {"name": "SITE_URL", "value": site_url}, {"name": "SITE_TITLE", "value": name},
                         {"name": "WP_ADMIN_USER", "value": st.get("adminUser", "shopadmin")},
                         {"name": "WP_ADMIN_EMAIL", "value": owner_email or "shop@example.com"},
                         {"name": "CURRENCY", "value": st.get("currency", "USD")}, {"name": "COUNTRY", "value": st.get("country", "US")},
                         {"name": "WC_VERSION", "value": st.get("woocommerceVersion", "")}, {"name": "HOME", "value": "/tmp"},
                         _secret_env("WP_ADMIN_PASSWORD", "wp-admin-password"),
                         _secret_env("WC_CONSUMER_KEY", "wc-consumer-key"), _secret_env("WC_CONSUMER_SECRET", "wc-consumer-secret")],
                 "resources": res.get("wpSetup", {}),
                 "securityContext": {**_DROP_ALL, "runAsUser": 33, "runAsGroup": 33, "runAsNonRoot": True},
                 "volumeMounts": [{"name": "wp-data", "mountPath": "/var/www/html"},
                                  {"name": "scripts", "mountPath": "/scripts", "readOnly": True}]}],
            "volumes": [{"name": "wp-data", "persistentVolumeClaim": {"claimName": "wordpress-data"}},
                        {"name": "scripts", "configMap": {"name": "wp-setup"}}]}}}})

    # --- this store's own WooCommerce MCP server (cluster-internal; only the gateway may call it)
    out.append({"apiVersion": "v1", "kind": "Service", "metadata": meta("woo-mcp", "woo-mcp"),
                "spec": {"type": "ClusterIP", "selector": _sel("woo-mcp"), "ports": [{"name": "http", "port": 8000, "targetPort": 8000}]}})
    out.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("woo-mcp", "woo-mcp"), "spec": {
        "replicas": 1, "selector": {"matchLabels": _sel("woo-mcp")},
        "template": {"metadata": {"labels": _labels(slug, "woo-mcp")}, "spec": {
            "automountServiceAccountToken": False, "enableServiceLinks": False, "securityContext": _POD_SEC,
            "containers": [{
                "name": "woo-mcp", "image": img["wooMcp"], "imagePullPolicy": pull,
                "ports": [{"name": "http", "containerPort": 8000}],
                "env": [{"name": "WC_BASE_URL", "value": f"http://wordpress.{ns}.svc.cluster.local"},
                        {"name": "STORE_TIMEZONE", "value": st.get("timezone", "UTC")}, {"name": "WC_AUTH_MODE", "value": "basic"},
                        _secret_env("WC_CONSUMER_KEY", "wc-consumer-key"), _secret_env("WC_CONSUMER_SECRET", "wc-consumer-secret"),
                        _secret_env("MCP_AUTH_TOKEN", "mcp-token")],
                "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}, "periodSeconds": 10},
                "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "initialDelaySeconds": 10, "periodSeconds": 20},
                "resources": res.get("wooMcp", {}),
                "securityContext": {**_DROP_ALL, "readOnlyRootFilesystem": True, "runAsNonRoot": True,
                                    "runAsUser": 10001, "runAsGroup": 10001},
                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
            "volumes": [{"name": "tmp", "emptyDir": {}}]}}}})

    # --- public storefront
    tls = ing.get("tls", {})
    ingress = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": meta("store", "ingress"), "spec": {
        "ingressClassName": ing.get("className", "traefik"),
        "rules": [{"host": host_for(cfg, slug), "http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": {
            "service": {"name": "wordpress", "port": {"number": 80}}}}]}}]}}
    if tls.get("enabled"):
        ingress["metadata"]["annotations"] = {"cert-manager.io/cluster-issuer": tls.get("clusterIssuer", "")}
        ingress["spec"]["tls"] = [{"hosts": [host_for(cfg, slug)], "secretName": "store-tls"}]
    out.append(ingress)
    return out


# ----------------------------------------------------------------------------------------------- reconciler
def _now() -> datetime:
    return datetime.now(timezone.utc)


async def diagnose(kube: Kube, ns: str) -> str:
    """Best-effort human reason why a store is not ready (pod states, scheduling, quota/warning events)."""
    reasons: list[str] = []
    try:
        for pod in await kube.list("Pod", ns):
            pname = pod["metadata"]["name"].rsplit("-", 2)[0] if "-" in pod["metadata"]["name"] else pod["metadata"]["name"]
            for c in pod.get("status", {}).get("conditions", []):
                if c.get("type") == "PodScheduled" and c.get("status") == "False":
                    reasons.append(f"{pname}: not scheduled ({c.get('message', '')[:160]})")
            for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                w = (cs.get("state") or {}).get("waiting")
                if w and w.get("reason") not in ("ContainerCreating", "PodInitializing"):
                    reasons.append(f"{pname}/{cs['name']}: {w.get('reason')} {w.get('message', '')[:160]}".strip())
                elif not cs.get("ready") and cs.get("restartCount", 0) >= 3:
                    reasons.append(f"{pname}/{cs['name']}: restarted {cs['restartCount']} times")
        events = [e for e in await kube.list("Event", ns) if e.get("type") == "Warning"]
        events.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        for e in events[-2:]:
            reasons.append(f"event {e.get('reason')}: {e.get('message', '')[:200]}")
    except KubeError as e:
        reasons.append(f"could not inspect namespace ({e})")
    return "; ".join(dict.fromkeys(reasons)) or "workloads did not become ready"


class Provisioner:
    def __init__(self, cfg: dict, sm, vault: Vault, kube: Kube, files: dict[str, str], clock=time.time):
        self.cfg, self.sm, self.vault, self.kube, self.files, self.clock = cfg, sm, vault, kube, files, clock
        self._last_apply: dict[int, float] = {}

    async def reconcile_all(self) -> None:
        async with self.sm() as db:
            ids = (await db.scalars(select(Store.id))).all()
        # stores are independent: several are provisioned (and torn down) at the same time
        await asyncio.gather(*(self.reconcile(i) for i in ids), return_exceptions=False)

    async def reconcile(self, store_id: int) -> None:
        try:
            async with self.sm() as db:
                row = await db.get(Store, store_id)
                if row is None:
                    return
                if row.status == "provisioning":
                    await self._provision(db, row)
                elif row.status == "deleting":
                    await self._teardown(db, row)
                elif row.status == "ready":
                    await self._keep_converged(db, row)
                await db.commit()
        except Exception:  # noqa: BLE001 - one broken store must never stop the loop for the others
            log.exception("reconcile of store %s failed", store_id)

    def _manifests(self, row: Store) -> list[dict]:
        return build_manifests(self.cfg, slug=row.slug, name=row.name, owner_email=row.owner_email,
                               secret_values=json.loads(self.vault.decrypt(row.secrets_enc)), files=self.files)

    async def _apply_all(self, row: Store) -> None:
        for m in self._manifests(row):
            await self.kube.apply(m)
        self._last_apply[row.id] = self.clock()

    async def _ready(self, ns: str) -> tuple[bool, str]:
        db = await self.kube.get("StatefulSet", ns, "mariadb")
        if not db or (db.get("status", {}).get("readyReplicas") or 0) < 1:
            return False, "starting the database"
        wp = await self.kube.get("Deployment", ns, "wordpress")
        if not wp or (wp.get("status", {}).get("readyReplicas") or 0) < 1:
            return False, "installing WordPress and WooCommerce"
        mcp = await self.kube.get("Deployment", ns, "woo-mcp")
        if not mcp or (mcp.get("status", {}).get("readyReplicas") or 0) < 1:
            return False, "starting the store's MCP server"
        return True, ""

    async def _provision(self, db, row: Store) -> None:
        ns = namespace_for(self.cfg, row.slug)
        if self.clock() > row.deadline:
            row.status = "failed"
            row.reason = f"Timed out after {int(self.cfg.get('provisionTimeoutSeconds', 900))}s: {await diagnose(self.kube, ns)}"
            log.warning("store %s failed: %s", row.slug, row.reason)
            return
        try:
            await self._apply_all(row)
        except KubeError as e:   # e.g. quota or admission denial: keep retrying until the deadline, show why
            row.reason = f"Retrying: {e}"
            return
        ok, why = await self._ready(ns)
        if not ok:
            row.reason = why
            return
        # register this store's MCP server in the gateway (same table the admin UI writes)
        srv = await db.scalar(select(Server).where(Server.slug == row.slug))
        if srv is None:
            srv = Server(slug=row.slug, name=f"Store: {row.name}", url=mcp_url(self.cfg, row.slug), auth_type="bearer",
                         credential_enc=self.vault.encrypt(json.loads(self.vault.decrypt(row.secrets_enc))["mcp-token"]),
                         enabled=True)
            db.add(srv)
            await db.flush()
        elif srv.url != mcp_url(self.cfg, row.slug):
            row.status, row.reason = "failed", f"The tool namespace '{row.slug}' is already used by another server."
            return
        row.server_id, row.status, row.reason, row.ready_at = srv.id, "ready", None, _now()
        log.info("store %s is ready", row.slug)

    async def _keep_converged(self, db, row: Store) -> None:
        """Ready stores are re-applied every few minutes (self-healing if someone deletes a piece)."""
        if self.clock() - self._last_apply.get(row.id, 0) < float(self.cfg.get("resyncSeconds", 300)):
            return
        if await self.kube.get("Namespace", None, namespace_for(self.cfg, row.slug)) is None:
            row.status, row.reason = "failed", "Its namespace was deleted outside the platform. Delete or retry the store."
            return
        try:
            await self._apply_all(row)
        except KubeError as e:
            log.warning("resync of %s failed: %s", row.slug, e)

    async def _teardown(self, db, row: Store) -> None:
        # 1) registration first, so the tools disappear for everyone immediately
        srv = await db.scalar(select(Server).where(Server.slug == row.slug))
        if srv is not None and srv.url == mcp_url(self.cfg, row.slug):
            await db.execute(delete(ToolPolicy).where(ToolPolicy.server_id == srv.id))
            await db.delete(srv)
        # 2) the namespace: deleting it removes every object in it, including PVCs (and their volumes) and Secrets
        ns = namespace_for(self.cfg, row.slug)
        try:
            await self.kube.delete("Namespace", None, ns)
            still_there = await self.kube.get("Namespace", None, ns) is not None
        except KubeError as e:
            row.reason = f"Retrying teardown: {e}"
            return
        if still_there:
            row.reason = "Deleting the namespace, volumes and secrets"
            return
        await db.delete(row)
        self._last_apply.pop(row.id, None)
        log.info("store %s deleted", row.slug)
