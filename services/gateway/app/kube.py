"""Minimal async Kubernetes API client (httpx) used by the store provisioner.

Only what the provisioner needs: server-side apply of any supported kind, get, delete, and a few
read helpers for readiness and failure diagnosis. Server-side apply makes every write idempotent,
which is what makes "create store" safe to retry and the reconcile loop safe to restart.
"""
from __future__ import annotations

import httpx

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# kind -> (api prefix, plural, namespaced)
KINDS: dict[str, tuple[str, str, bool]] = {
    "Namespace": ("/api/v1", "namespaces", False),
    "Secret": ("/api/v1", "secrets", True),
    "ConfigMap": ("/api/v1", "configmaps", True),
    "Service": ("/api/v1", "services", True),
    "PersistentVolumeClaim": ("/api/v1", "persistentvolumeclaims", True),
    "ResourceQuota": ("/api/v1", "resourcequotas", True),
    "LimitRange": ("/api/v1", "limitranges", True),
    "Pod": ("/api/v1", "pods", True),
    "Event": ("/api/v1", "events", True),
    "Deployment": ("/apis/apps/v1", "deployments", True),
    "StatefulSet": ("/apis/apps/v1", "statefulsets", True),
    "NetworkPolicy": ("/apis/networking.k8s.io/v1", "networkpolicies", True),
    "Ingress": ("/apis/networking.k8s.io/v1", "ingresses", True),
}


class KubeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def path(kind: str, namespace: str | None, name: str | None = None) -> str:
    prefix, plural, namespaced = KINDS[kind]
    p = f"{prefix}/namespaces/{namespace}/{plural}" if namespaced else f"{prefix}/{plural}"
    return f"{p}/{name}" if name else p


class Kube:
    def __init__(self, api: str = "https://kubernetes.default.svc", transport: httpx.AsyncBaseTransport | None = None,
                 field_manager: str = "urumi-store-provisioner"):
        token = ""
        try:
            token = open(f"{SA_DIR}/token").read().strip()
        except OSError:
            pass
        self.fm = field_manager
        self._c = httpx.AsyncClient(base_url=api, timeout=20, follow_redirects=False, transport=transport,
                                    headers={"Authorization": f"Bearer {token}"} if token else {},
                                    verify=f"{SA_DIR}/ca.crt" if transport is None else True)

    async def aclose(self):
        await self._c.aclose()

    @staticmethod
    def _raise(r: httpx.Response, what: str):
        if r.status_code >= 300:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise KubeError(r.status_code, f"{what}: HTTP {r.status_code}: {msg[:300]}")

    async def apply(self, manifest: dict) -> None:
        kind, meta = manifest["kind"], manifest["metadata"]
        r = await self._c.patch(path(kind, meta.get("namespace"), meta["name"]), json=manifest,
                                params={"fieldManager": self.fm, "force": "true"},
                                headers={"Content-Type": "application/apply-patch+yaml"})
        self._raise(r, f"apply {kind}/{meta['name']}")

    async def get(self, kind: str, namespace: str | None, name: str) -> dict | None:
        r = await self._c.get(path(kind, namespace, name))
        if r.status_code == 404:
            return None
        self._raise(r, f"get {kind}/{name}")
        return r.json()

    async def list(self, kind: str, namespace: str, **params) -> list[dict]:
        r = await self._c.get(path(kind, namespace), params=params)
        self._raise(r, f"list {kind}")
        return r.json().get("items", [])

    async def delete(self, kind: str, namespace: str | None, name: str) -> bool:
        """-> True if it was deleted or is being deleted, False if it was already gone."""
        r = await self._c.delete(path(kind, namespace, name), params={"propagationPolicy": "Background"})
        if r.status_code == 404:
            return False
        self._raise(r, f"delete {kind}/{name}")
        return True
