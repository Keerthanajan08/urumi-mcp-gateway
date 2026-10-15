"""Hosted MCP servers: an admin gives a container image; the platform runs it in the cluster.

Per hosted server the gateway creates, in its own namespace and labelled `urumi.io/hosted=<slug>`:
  Secret          env vars the admin supplied (never returned by the API afterwards)
  NetworkPolicy   ingress from the gateway pods only; egress DNS + public internet only (no RFC1918,
                  so it cannot reach MariaDB, Postgres, WordPress, the K8s API or other tenants)
  Deployment      non-root, read-only rootfs, no capabilities, seccomp RuntimeDefault, no service-account
                  token, CPU/memory requests+limits, tcp probes
  Service         ClusterIP `hosted-<slug>` (no Ingress: unreachable from outside)
and registers it as a normal upstream server. Delete removes all four plus the registration.

Builders below are pure (stdlib only) so they are unit-tested without a cluster. KubeClient talks to
the API with server-side apply, so re-running create is idempotent (safe retry).

Trust boundary (see threat model): the gateway ServiceAccount can create these kinds in ITS OWN namespace only.
Kubernetes RBAC cannot restrict by label, so an exploited gateway could touch other objects in that namespace;
mitigations are the image-prefix allowlist, validation here, and keeping this feature off by default.
"""
from __future__ import annotations

import re

LABEL = "urumi.io/hosted"
MANAGED = {"app.kubernetes.io/managed-by": "urumi-gateway", "app.kubernetes.io/component": "hosted-mcp"}
IMAGE_RE = re.compile(r"^[a-z0-9]([a-z0-9._/-]*[a-z0-9])?(:[A-Za-z0-9._-]{1,128})?(@sha256:[a-f0-9]{64})?$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]{0,80}$")
RESERVED_SLUGS = {"gateway"}


class HostedError(ValueError):
    pass


def name_for(slug: str) -> str:
    return f"hosted-{slug}"


def internal_url(slug: str, ns: str, port: int, path: str) -> str:
    return f"http://{name_for(slug)}.{ns}.svc.cluster.local:{port}{path}"


def validate_request(image: str, port: int, path: str, env: dict, allowed_prefixes: list[str]) -> None:
    if not allowed_prefixes:
        raise HostedError("No image registries are allowed (set hosted.allowedImagePrefixes in the chart).")
    if not IMAGE_RE.match(image or "") or len(image) > 300:
        raise HostedError("Invalid image reference.")
    if not any(image.startswith(p) for p in allowed_prefixes):
        raise HostedError(f"Image must start with one of: {', '.join(allowed_prefixes)}")
    if not 1024 <= int(port) <= 65535:
        raise HostedError("port must be 1024-65535 (containers run non-root).")
    if not PATH_RE.match(path or ""):
        raise HostedError("Invalid MCP path.")
    if len(env) > 20:
        raise HostedError("At most 20 environment variables.")
    for k, v in env.items():
        if not ENV_RE.match(k) or k.upper().startswith(("KUBERNETES_", "LD_", "PATH")):
            raise HostedError(f"Environment variable name not allowed: {k}")
        if not isinstance(v, str) or len(v) > 4096:
            raise HostedError(f"Value for {k} must be a string under 4 KB.")


def build_manifests(*, ns: str, slug: str, image: str, port: int, env: dict, run_as_user: int = 10001,
                    cpu_limit: str = "250m", mem_limit: str = "256Mi") -> list[tuple[str, dict]]:
    """-> ordered [(kind, manifest)]; apply in order, delete in reverse."""
    n = name_for(slug)
    labels = {**MANAGED, LABEL: slug}
    meta = lambda: {"name": n, "namespace": ns, "labels": dict(labels)}  # noqa: E731
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": meta(), "type": "Opaque", "stringData": dict(env)}
    netpol = {
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(),
        "spec": {
            "podSelector": {"matchLabels": {LABEL: slug}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}}}],
                         "ports": [{"protocol": "TCP", "port": port}]}],
            "egress": [
                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                         "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                 "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                {"to": [{"ipBlock": {"cidr": "0.0.0.0/0",
                                     "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"]}}],
                 "ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 80}]},
            ],
        },
    }
    probe = {"tcpSocket": {"port": port}}
    deploy = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(),
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {LABEL: slug}},
            "template": {
                "metadata": {"labels": dict(labels)},
                "spec": {
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": run_as_user, "runAsGroup": run_as_user,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "mcp", "image": image, "imagePullPolicy": "IfNotPresent",
                        "ports": [{"name": "http", "containerPort": port}],
                        "envFrom": [{"secretRef": {"name": n}}],
                        "env": [{"name": "PORT", "value": str(port)}],
                        "startupProbe": {**probe, "periodSeconds": 3, "failureThreshold": 40},
                        "readinessProbe": {**probe, "periodSeconds": 10},
                        "livenessProbe": {**probe, "periodSeconds": 20, "failureThreshold": 3},
                        "resources": {"requests": {"cpu": "25m", "memory": "64Mi"},
                                      "limits": {"cpu": cpu_limit, "memory": mem_limit}},
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                    }],
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}],
                },
            },
        },
    }
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": meta(),
           "spec": {"type": "ClusterIP", "selector": {LABEL: slug}, "ports": [{"name": "http", "port": port, "targetPort": port}]}}
    return [("Secret", secret), ("NetworkPolicy", netpol), ("Deployment", deploy), ("Service", svc)]


_PATHS = {"Secret": "/api/v1/namespaces/{ns}/secrets", "Service": "/api/v1/namespaces/{ns}/services",
          "Deployment": "/apis/apps/v1/namespaces/{ns}/deployments",
          "NetworkPolicy": "/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies"}
_SA = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeClient:
    """Minimal in-cluster client (httpx) so we do not pull the whole kubernetes SDK into the image."""

    def __init__(self, ns: str, api: str = "https://kubernetes.default.svc", transport=None):
        import httpx
        self.ns = ns
        try:
            token = open(f"{_SA}/token").read().strip()
        except OSError:
            token = ""
        self._c = httpx.AsyncClient(base_url=api, headers={"Authorization": f"Bearer {token}"} if token else {},
                                    verify=f"{_SA}/ca.crt" if transport is None else True, timeout=15, transport=transport,
                                    follow_redirects=False)

    async def apply(self, kind: str, manifest: dict) -> None:
        """Server-side apply: create-or-update, so a retried create converges instead of failing with 409."""
        name = manifest["metadata"]["name"]
        r = await self._c.patch(f"{_PATHS[kind].format(ns=self.ns)}/{name}", json=manifest,
                                params={"fieldManager": "urumi-gateway", "force": "true"},
                                headers={"Content-Type": "application/apply-patch+yaml"})
        if r.status_code >= 300:
            raise HostedError(f"Kubernetes refused {kind}/{name}: HTTP {r.status_code} {r.text[:200]}")

    async def delete(self, kind: str, name: str) -> None:
        r = await self._c.delete(f"{_PATHS[kind].format(ns=self.ns)}/{name}", params={"propagationPolicy": "Foreground"})
        if r.status_code >= 300 and r.status_code != 404:  # already gone is fine (idempotent teardown)
            raise HostedError(f"Kubernetes refused to delete {kind}/{name}: HTTP {r.status_code}")

    async def aclose(self):
        await self._c.aclose()


async def deploy(kube: KubeClient, manifests: list[tuple[str, dict]]) -> None:
    for kind, m in manifests:
        await kube.apply(kind, m)


async def teardown(kube: KubeClient, slug: str) -> None:
    """Reverse order; every step is attempted so a partial failure can simply be retried."""
    errors = []
    for kind in ("Service", "Deployment", "NetworkPolicy", "Secret"):
        try:
            await kube.delete(kind, name_for(slug))
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    if errors:
        raise HostedError("; ".join(errors))
