# Local workflow: k3d (k3s in Docker) + Helm.   Prereqs: docker, k3d, kubectl, helm
CLUSTER ?= urumi
NS      ?= urumi
TAG     ?= local
CHART   := deploy/charts/urumi
# extra flags for `k3d cluster create`, e.g. a registry mirror config
K3D_ARGS ?=

.PHONY: help cluster build import install upgrade uninstall status verify test venv logs-setup nuke dod-check standout-check oauth-check stores-check netpol-check
help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

cluster: ## create the k3d cluster (ports 80/443 -> host; k3s enforces NetworkPolicies)
	k3d cluster create $(CLUSTER) -p "80:80@loadbalancer" -p "443:443@loadbalancer" --wait $(K3D_ARGS)

build: ## build the 4 images
	docker build -t urumi/woo-mcp:$(TAG)   services/woo-mcp
	docker build -t urumi/demo-mcp:$(TAG)  services/demo-mcp
	docker build -t urumi/gateway:$(TAG)   services/gateway
	docker build -t urumi/dashboard:$(TAG) services/dashboard

import: ## load images into the cluster (no registry needed locally)
	k3d image import -c $(CLUSTER) urumi/woo-mcp:$(TAG) urumi/demo-mcp:$(TAG) urumi/gateway:$(TAG) urumi/dashboard:$(TAG)

# The K8s API endpoint, needed by the NetworkPolicies of the two components that talk to it (stores / hosted)
APISERVER = $$(kubectl get endpoints kubernetes -o jsonpath='{.subsets[0].addresses[0].ip}')/32
install: ## helm install, local values. Options: LLM_API_KEY=... STORES=1 (bonus) HOSTED=1 (hosted MCP servers) HELM_ARGS=...
	helm upgrade --install urumi $(CHART) -n $(NS) --create-namespace -f $(CHART)/values-local.yaml \
	  $(if $(LLM_API_KEY),--set secrets.llmApiKey=$(LLM_API_KEY),) \
	  $(if $(STORES),--set stores.enabled=true --set stores.apiServerCIDR=$(APISERVER),) \
	  $(if $(HOSTED),--set hosted.enabled=true --set hosted.apiServerCIDR=$(APISERVER) --set 'hosted.allowedImagePrefixes={urumi/}',) \
	  $(HELM_ARGS)

upgrade: install ## alias: helm upgrade is idempotent; `helm rollback urumi <rev> -n urumi` to undo

uninstall: ## remove the release AND its data (PVCs incl. backups, secret, provisioned stores) for a clean slate
	-kubectl get ns -l urumi.io/store -o name | xargs -r kubectl delete --wait=false
	-helm uninstall urumi -n $(NS)
	-kubectl -n $(NS) delete pvc --all
	-kubectl -n $(NS) delete secret urumi-secrets

status: ## pods + ingress
	kubectl -n $(NS) get pods,svc,ingress,pvc

logs-setup: ## follow WooCommerce bootstrap
	kubectl -n $(NS) logs deploy/wordpress -c wp-setup -f

verify: ## lint + render the chart (needs helm). Catches template/YAML mistakes before a deploy
	helm lint $(CHART) -f $(CHART)/values-local.yaml
	helm lint $(CHART) -f $(CHART)/values-prod.yaml
	helm template urumi $(CHART) -n $(NS) -f $(CHART)/values-local.yaml > /tmp/urumi-local.yaml
	helm template urumi $(CHART) -n $(NS) -f $(CHART)/values-prod.yaml > /tmp/urumi-prod.yaml
	helm template urumi $(CHART) -n $(NS) -f $(CHART)/values-prod.yaml --set stores.enabled=true --set stores.apiServerCIDR=10.0.0.1/32 \
	  --set hosted.enabled=true --set hosted.apiServerCIDR=10.0.0.1/32 --set 'hosted.allowedImagePrefixes={urumi/}' > /tmp/urumi-all.yaml
	@echo rendered OK: /tmp/urumi-local.yaml /tmp/urumi-prod.yaml /tmp/urumi-all.yaml

venv: ## python venv with every service's pinned test dependencies (+ deps for the live check scripts)
	python3 -m venv .venv && .venv/bin/pip install -q -r services/gateway/requirements-dev.txt \
	  -r services/woo-mcp/requirements-dev.txt -r services/demo-mcp/requirements-dev.txt

PY ?= $(if $(wildcard .venv/bin/python),$(CURDIR)/.venv/bin/python,python3)
test: ## unit + end-to-end tests (run `make venv` once first)
	cd services/woo-mcp && $(PY) -m pytest -q
	cd services/demo-mcp && $(PY) -m pytest -q
	cd services/gateway && $(PY) -m pytest -q

ADMIN_PW = $$(kubectl -n $(NS) get secret urumi-secrets -o jsonpath='{.data.bootstrap-admin-password}' | base64 -d)
dod-check: ## the assignment's definition of done, scripted, against the running install (needs an LLM key in the chart)
	ADMIN_PW=$(ADMIN_PW) $(PY) scripts/dod_check.py
standout-check: ## tool permissions, tool search, observability against the running install
	ADMIN_PW=$(ADMIN_PW) $(PY) scripts/standout_check.py
oauth-check: ## OAuth flow with the official MCP SDK client (browser step automated)
	EMAIL=admin@example.com PASSWORD=$(ADMIN_PW) $(PY) scripts/oauth_check.py
netpol-check: ## are NetworkPolicies actually ENFORCED right now? (probe pod-to-pod paths; run before a demo)
	./scripts/netpol-check.sh

stores-check: ## bonus: two stores, cross-store chat, isolation, delete (needs stores.enabled=true)
	ADMIN_PW=$(ADMIN_PW) $(PY) scripts/stores_check.py

helm-test: ## in-cluster smoke test (gateway ready + OAuth metadata served)
	helm test urumi -n $(NS)

scale-demo: ## 3 gateway replicas, requests spread across pods (needs KEY=umk_...)
	./scripts/scale-demo.sh

rollback-demo: ## upgrade, failed upgrade (auto rollback), manual rollback
	./scripts/upgrade-rollback-demo.sh

nuke: ## delete the whole k3d cluster
	k3d cluster delete $(CLUSTER)
