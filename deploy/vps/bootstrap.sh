#!/usr/bin/env bash
# One-command production-like deploy on a fresh Ubuntu 22.04/24.04 VPS (k3s + cert-manager + Let's Encrypt).
#
#   git clone <your repo> && cd urumi-mcp-gateway
#   sudo DOMAIN=yourname.duckdns.org EMAIL=you@example.com LLM_API_KEY=gsk_... ./deploy/vps/bootstrap.sh
#
# Same Helm chart as local, with values-prod.yaml; only the domain, image tags and API-server address are set here.
# DNS first: DOMAIN and *.DOMAIN must point at this server (DuckDNS does both for one name), and ports 80 + 443
# must be open in the cloud firewall. Safe to re-run: every step checks what is already done.
#
# Options (environment variables)
#   DOMAIN, EMAIL      required. EMAIL is for Let's Encrypt expiry notices and becomes the dashboard admin login.
#   LLM_API_KEY        chat agent key (Groq by default; see gateway.llmBaseUrl / llmModel in values.yaml)
#   STORES=1           enable multi-store provisioning (default 1)      HOSTED=1   enable hosted MCP servers (default 0)
#   ACME_SERVER        default Let's Encrypt production. Use https://acme-staging-v02.api.letsencrypt.org/directory
#                      to test without touching rate limits (browsers will then warn about the certificate).
#   ACME_CA_FILE       CA bundle for a private ACME server (testing only)
#   SKIP_BUILD=1       images urumi/*:$TAG are already in k3s;  RUN_CHECKS=0 skip the checks at the end
#   HELM_EXTRA         extra flags for helm upgrade, e.g. "--set gateway.llmModel=openai/gpt-oss-20b"
set -euo pipefail

: "${DOMAIN:?set DOMAIN, e.g. DOMAIN=yourname.duckdns.org}"
: "${EMAIL:?set EMAIL: certificate expiry notices and the dashboard admin login}"
STORES=${STORES:-1}; HOSTED=${HOSTED:-0}; RUN_CHECKS=${RUN_CHECKS:-1}; SKIP_BUILD=${SKIP_BUILD:-0}
ACME_SERVER=${ACME_SERVER:-https://acme-v02.api.letsencrypt.org/directory}
K3S_VERSION=${K3S_VERSION:-v1.31.5+k3s1}; HELM_VERSION=${HELM_VERSION:-v3.16.4}; CM_VERSION=${CM_VERSION:-v1.16.2}
NS=urumi; CHART=deploy/charts/urumi
cd "$(dirname "$0")/../.."
TAG=${TAG:-prod-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"

step "1/9 Checks"
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
[ "$mem_gb" -ge 3 ] || die "this needs at least 4 GB RAM (8 GB recommended); found ${mem_gb} GB"
[ "$mem_gb" -ge 7 ] || echo "note: ${mem_gb} GB RAM. Fine for the core, tight once you create extra stores (8 GB recommended)."
PUBLIC_IP=${PUBLIC_IP:-$(curl -fsS --max-time 10 https://api.ipify.org || true)}
echo "public IP: ${PUBLIC_IP:-unknown}   domain: $DOMAIN   image tag: $TAG"
for h in "app.$DOMAIN" "store.$DOMAIN"; do
  got=$(getent ahostsv4 "$h" | awk 'NR==1{print $1}')
  if [ -n "$PUBLIC_IP" ] && [ "$got" != "$PUBLIC_IP" ]; then
    die "$h resolves to '${got:-nothing}', not this server ($PUBLIC_IP). Point DNS here first (DuckDNS: set the IP), wait a minute, re-run."
  fi
done

step "2/9 Packages (docker, make, python venv)"
need=""
for p in docker:docker.io make:make git:git jq:jq curl:curl; do command -v "${p%%:*}" >/dev/null || need="$need ${p#*:}"; done
python3 -c "import venv, ensurepip" 2>/dev/null || need="$need python3-venv"
if [ -n "$need" ]; then apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need; fi
if command -v systemctl >/dev/null && ! docker info >/dev/null 2>&1; then systemctl enable --now docker; fi

step "3/9 k3s $K3S_VERSION (Traefik, local-path storage, NetworkPolicy controller included)"
if ! kubectl get nodes >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - --write-kubeconfig-mode 644
fi
for _ in $(seq 1 60); do kubectl get nodes 2>/dev/null | grep -q " Ready" && break; sleep 5; done
kubectl get nodes
command -v helm >/dev/null || curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | DESIRED_VERSION="$HELM_VERSION" bash

step "4/9 Images (built on this server, loaded into k3s: no registry needed)"
if [ "$SKIP_BUILD" != 1 ]; then
  make build TAG="$TAG"
  for s in woo-mcp demo-mcp gateway dashboard; do docker save "urumi/$s:$TAG" | k3s ctr -n k8s.io images import - >/dev/null; done
fi
for s in woo-mcp demo-mcp gateway dashboard; do
  k3s ctr -n k8s.io images ls -q | grep -q "urumi/$s:$TAG" || die "image urumi/$s:$TAG is not in k3s"
done
echo "images ready"

step "5/9 cert-manager $CM_VERSION + Let's Encrypt issuer"
helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
helm repo update jetstack >/dev/null
helm upgrade --install cert-manager jetstack/cert-manager --version "$CM_VERSION" -n cert-manager --create-namespace \
  --set crds.enabled=true --wait --timeout 10m >/dev/null
CA_LINE=""
[ -n "${ACME_CA_FILE:-}" ] && CA_LINE="    caBundle: $(base64 -w0 < "$ACME_CA_FILE")"
for _ in $(seq 1 12); do   # the webhook can take a few seconds after the rollout to accept requests
  cat <<YAML | kubectl apply -f - >/dev/null 2>&1 && break || sleep 5
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: letsencrypt-prod }
spec:
  acme:
    server: $ACME_SERVER
    email: $EMAIL
$CA_LINE
    privateKeySecretRef: { name: letsencrypt-prod-account }
    solvers:
      # ClusterIP (not the default NodePort) solver Services: store namespaces forbid NodePorts by quota
      - http01: { ingress: { ingressClassName: traefik, serviceType: ClusterIP } }
YAML
done
kubectl get clusterissuer letsencrypt-prod >/dev/null || die "could not create the ClusterIssuer"

# In-cluster DNS: resolve DOMAIN and *.DOMAIN straight to Traefik. cert-manager self-checks each HTTP-01 challenge
# from inside the cluster before asking Let's Encrypt; on clouds that do not "hairpin" a VM's traffic to its own
# public IP, that check would otherwise hang forever.
TRAEFIK_IP=$(kubectl -n kube-system get svc traefik -o jsonpath='{.spec.clusterIP}')
kubectl -n kube-system get configmap coredns-custom >/dev/null 2>&1 || kubectl -n kube-system create configmap coredns-custom >/dev/null
kubectl -n kube-system patch configmap coredns-custom --type merge -p "$(jq -n --arg k urumi-domain.server --arg v \
"$DOMAIN:53 {
    template IN A $DOMAIN {
        answer \"{{ .Name }} 60 IN A $TRAEFIK_IP\"
    }
    template IN AAAA $DOMAIN {
        rcode NOERROR
    }
}" '{data: {($k): $v}}')" >/dev/null
kubectl -n kube-system rollout restart deploy/coredns >/dev/null && kubectl -n kube-system rollout status deploy/coredns --timeout=120s >/dev/null

step "6/9 Secrets (created once, outside Helm; never in git or values)"
if kubectl -n "$NS" get secret urumi-secrets >/dev/null 2>&1; then
  echo "urumi-secrets already exists: keeping it"
  [ -n "${LLM_API_KEY:-}" ] && kubectl -n "$NS" patch secret urumi-secrets -p "{\"stringData\":{\"llm-api-key\":\"$LLM_API_KEY\"}}" >/dev/null
else
  LLM_API_KEY="${LLM_API_KEY:-}" ./deploy/prod/create-secrets.sh "$NS" | sed 's/password: .*/password: (shown at the end)/'
fi

step "7/9 Helm install (values-prod.yaml)"
API=$(kubectl get endpoints kubernetes -o jsonpath='{.subsets[0].addresses[0].ip}')
OPTS=()
[ "$STORES" = 1 ] && OPTS+=(--set stores.enabled=true --set "stores.apiServerCIDR=$API/32")
if [ -n "${HELM_EXTRA:-}" ]; then read -ra EXTRA <<<"$HELM_EXTRA"; OPTS+=("${EXTRA[@]}"); fi
[ "$HOSTED" = 1 ] && OPTS+=(--set hosted.enabled=true --set "hosted.apiServerCIDR=$API/32" --set "hosted.allowedImagePrefixes={urumi/}")
helm upgrade --install urumi "$CHART" -n "$NS" -f "$CHART/values-prod.yaml" \
  --set domain="$DOMAIN" --set secrets.adminEmail="$EMAIL" --set images.pullPolicy=IfNotPresent \
  --set images.wooMcp="urumi/woo-mcp:$TAG" --set images.demoMcp="urumi/demo-mcp:$TAG" \
  --set images.gateway="urumi/gateway:$TAG" --set images.dashboard="urumi/dashboard:$TAG" \
  "${OPTS[@]}" --wait --timeout 15m
kubectl -n "$NS" rollout status deploy/wordpress --timeout=15m >/dev/null

step "8/9 TLS certificates from Let's Encrypt"
if ! kubectl -n "$NS" wait certificate --all --for=condition=Ready --timeout=6m; then
  kubectl -n "$NS" get certificate,challenge 2>/dev/null || true
  kubectl -n "$NS" describe challenge 2>/dev/null | grep -E "Reason|State|Presented" | head -10 || true
  die "certificates not issued. Usual causes: port 80 closed in the cloud firewall, DNS not pointing here, or Let's Encrypt rate limits (try ACME_SERVER=staging)."
fi

step "9/9 Checks"
PUBLIC_CA=0; [ "$ACME_SERVER" = "https://acme-v02.api.letsencrypt.org/directory" ] && PUBLIC_CA=1
for h in app store; do
  if curl -fsS -o /dev/null "https://$h.$DOMAIN/"; then echo "https://$h.$DOMAIN OK (trusted certificate)"
  elif [ "$PUBLIC_CA" = 1 ]; then echo "WARNING: https://$h.$DOMAIN did not answer with a trusted certificate"
  else echo "https://$h.$DOMAIN: certificate from a test CA ($ACME_SERVER), not trusted by browsers (expected)"; fi
done
helm test urumi -n "$NS" | grep -E "Phase" || true
if [ "$RUN_CHECKS" = 1 ] && [ "$PUBLIC_CA" != 1 ]; then
  NS=$NS ./scripts/netpol-check.sh || true
  echo "(test CA in use: skipped the HTTPS client checks, which need a publicly trusted certificate)"
elif [ "$RUN_CHECKS" = 1 ]; then
  [ -x .venv/bin/python ] || make venv >/dev/null
  NS=$NS ./scripts/netpol-check.sh || true
  PW=$(kubectl -n "$NS" get secret urumi-secrets -o jsonpath='{.data.bootstrap-admin-password}' | base64 -d)
  EMAIL="$EMAIL" PASSWORD="$PW" .venv/bin/python scripts/oauth_check.py "https://app.$DOMAIN/mcp" | tail -1 || true
  if [ -n "$(kubectl -n "$NS" get secret urumi-secrets -o jsonpath='{.data.llm-api-key}' | base64 -d)" ]; then
    DOMAIN="$DOMAIN" SCHEME=https NS=$NS ADMIN_EMAIL="$EMAIL" ADMIN_PW="$PW" .venv/bin/python scripts/dod_check.py | tail -3 || true
  else
    echo "(no LLM key: skipped the definition-of-done check, which uses the chat agent)"
  fi
fi

PW=$(kubectl -n "$NS" get secret urumi-secrets -o jsonpath='{.data.bootstrap-admin-password}' | base64 -d)
WPPW=$(kubectl -n "$NS" get secret urumi-secrets -o jsonpath='{.data.wp-admin-password}' | base64 -d)
cat <<TXT

$(printf '\033[1;32m')Deployed.$(printf '\033[0m')
  Dashboard   https://app.$DOMAIN        login: $EMAIL / $PW
  Storefront  https://store.$DOMAIN      WordPress admin: https://store.$DOMAIN/wp-admin  (shopadmin / $WPPW)
  MCP         https://app.$DOMAIN/mcp    (API key from Dashboard -> My keys, or OAuth login)
  Stores      created stores get https://<name>.$DOMAIN
Keep these passwords somewhere safe. Re-run this script any time; it only changes what is missing.
TXT
