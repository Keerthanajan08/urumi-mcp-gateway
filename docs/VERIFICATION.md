# What was verified, and how

This file records what was **actually run**, where, and what it showed, so you know what you're claiming. Re-run the checks yourself before recording the demo: `make test`, then `make netpol-check`, `make dod-check`, `make standout-check`, `make oauth-check` and `make stores-check` against your own cluster.

## Environment used
Three rounds of verification on a 2-CPU / 7 GB Linux VM:

1. **k3d, with a real LLM (final round).** `make cluster`, `make import` and `make install STORES=1 HOSTED=1 LLM_API_KEY=…` from the README, on k3d 5.8 / k3s v1.31.5. The chat agent used **Groq, `openai/gpt-oss-120b`**, with a real free-tier key. Every check script, the bonus and hosted servers passed in this round. The cluster also survived two full reboots of the VM (`k3d cluster start`): data intact, pods back.
2. **k3s directly on the VM (first round),** while k3d couldn't start pods yet. It covered everything that round 1 doesn't repeat: the browser checkout, the upgrade/rollback demo, backup/restore, the scaling and zero-downtime measurements, and the production values with TLS.

3. **The VPS bootstrap against an ACME server (latest round).** `deploy/vps/bootstrap.sh` run on k3s with cert-manager pointed at **Pebble**, Let's Encrypt's own ACME test server, with a DNS test server answering for the domain. This runs the real HTTP-01 flow: Pebble connects to port 80 through Traefik and fetches the challenge from cert-manager's solver pod.

- **Images:** built from the repo's Dockerfiles and loaded with `make import` (k3d) or the k3s equivalent.
- **Sandbox-only adaptations (not in the repo; nothing in the chart depends on them):**
  - The VM's outbound HTTPS goes through a TLS-intercepting proxy, so image builds used CA-trusting base images, and local overlays of the WordPress and WP-CLI images trusted that CA (to download WooCommerce).
  - The VM forbids negative `oom_score_adj`, so a small runc wrapper inside the k3s / k3d node clamps it. This is what first stopped k3d from starting pods.
  - Kubelet image-GC thresholds were adjusted for the VM's disk quota (`K3D_ARGS`).
- **LLM in round 2:** a deterministic stub. Round 1 replaced it with the real Groq model.

## Results

| What | How | Result |
|---|---|---|
| Unit and end-to-end tests | `make venv && make test` from a clean checkout | 39 passed (woo-mcp 4, demo 3, gateway 32) |
| **k3d + real LLM** | `make cluster import install STORES=1 HOSTED=1`, then `make netpol-check dod-check standout-check oauth-check stores-check helm-test` with Groq `openai/gpt-oss-120b` | all pass. The real model created the product, found today's orders, summarised them into a note across two servers (6 of 6 runs), and compared today's orders across 3 stores in a table |
| Chart | `make verify`, plus kubeconform `-strict` (k8s 1.31) on local, prod, and prod + hosted + stores + ServiceMonitor | 119 resources valid |
| Dashboard build | `npm install && npm run build`; every page opened in Chromium (Playwright) | builds; no page errors |
| **Definition of done**, two users, through the Ingress | `scripts/dod_check.py` | all checks pass (list below) |
| Storefront checkout in a real browser | Playwright: shop → product → cart → checkout → Cash on delivery → order received; then WP admin → Orders | order visible in WooCommerce admin |
| External clients | **MCP Inspector CLI** (URL + key: tools/list, tools/call); **Claude Code** `claude mcp add … --header` → `claude mcp list` | Inspector lists 19 tools and calls them; Claude Code reports **Connected** (prompting through Claude Code needs model credentials: do it on camera) |
| Tool permissions | `scripts/standout_check.py` | hidden from list, search and chat; rejected on call; audited `denied` |
| Tool search | same | 3 meta-tools; search → describe → call; audit shows the real tool; no bypass of policies |
| OAuth | `scripts/oauth_check.py` (the official MCP SDK `OAuthClientProvider`; browser step automated), over HTTP locally and over **HTTPS** on the prod install | discovery, registration, PKCE, tokens, tool call, refresh rotation (reused refresh token → `invalid_grant`) |
| NetworkPolicies | pod-to-pod connection probes | all as designed: e.g. woo-mcp ✗ MariaDB/Postgres/internet, ✓ WordPress; notes ✗ everything; gateway ✗ MariaDB/WordPress/K8s API |
| SSRF guard | admin API with metadata IP, cluster DNS names, `10.43.0.1`, `127.0.0.1`, `[::1]`, `[::ffff:169.254.169.254]`, decimal/hex IPs, userinfo, `file://` | all rejected with a reason |
| Hosted MCP server | deploy `urumi/demo-mcp` by image via the API | Secret, NetworkPolicy, Deployment and Service created; tools `hutils__*` listed; pod non-root/read-only/no token; can't reach DBs, WordPress or the K8s API; delete removes all four |
| Scaling | `scripts/scale-demo.sh` with 3 replicas; 10 MCP SDK sessions | requests spread over all pods; every SDK session spanned several pods and succeeded; rate limit 120/min: exactly 120 calls allowed in the minute across 3 replicas |
| Zero-downtime | a loop of `/api` + `/mcp` requests during rolling restarts of gateway + dashboard | 0 failures in about 5,500 requests (3 rounds); 0 failures during the whole upgrade/rollback demo |
| Upgrade / rollback | `scripts/upgrade-rollback-demo.sh` with `WC=10.4.3 WP_IMAGE=wordpress:6.8` | store went WP 6.7.2 → 6.8.3 and WooCommerce 10.3.0 → 10.4.3 with a gateway config change in one `helm upgrade`; a bad gateway image was rolled back by `--atomic`; `helm rollback` restored config, images, and store versions (6.7.2 / 10.3.0); `helm test` passed |
| Backup / restore | product created after a dump, then `restore-store.sh <dump>` | product gone, orders intact |
| Observability | `/metrics` in-cluster; `/api/stats`; `/metrics` via Ingress | metrics present; stats per server; not exposed publicly |
| **VPS bootstrap + ACME** | `bootstrap.sh` with `ACME_SERVER=<pebble>`, run twice (second run: no changes), then two stores created from the dashboard API | certificates issued over HTTP-01 for `app.` and `store.`, and automatically for each new store (store ready in about 90 s, certificate a few seconds later); all served over HTTPS with a chain to the ACME CA; `http://` → 301 to `https://`; WordPress's loopback to its own HTTPS URL works (wp-cron 200) in the main store and in provisioned stores; `helm test` and `netpol-check` pass with three stores |
| Production values | `values-prod.yaml` installed in a 2nd namespace with cert-manager (private CA instead of Let's Encrypt) + `create-secrets.sh` | certificates issued; HTTPS storefront order read back through the gateway; Secure cookie; https OAuth metadata; 2 replicas + HPA + PDB; fresh install with 0 restarts |
| **Bonus: stores** | `scripts/stores_check.py`, plus manual failure and recovery runs | two stores provisioned concurrently (about 2.5 min); tools auto-registered; chat compared today's orders across stores; isolation probes blocked; delete removed tools at once, then namespace, PVs and registration in about 45 s; a store killed by a too-small quota failed with *"exceeded quota … requested: requests.cpu=125m"* and became Ready on Retry after the quota was fixed; the provisioner pod killed mid-provisioning resumed and finished the store; per-user limit enforced; provisioner RBAC/admission: no Secret reads, denied in `kube-system`, `urumi` and non-store namespaces |

### Definition of done (`scripts/dod_check.py` output, summarized)
- Shopper checks out with Cash on delivery (order #N).
- **User A, chat:** `woocommerce__create_product` → product on the storefront, and its page renders. Order placed by hand. "What orders came in today?" → `woocommerce__list_orders` → the answer includes the order.
- **User B**, invited and with their own key, URL + key only:
  - sees the tools of every enabled server
  - `notes__ping` vs `utils__ping` are namespaced
  - the orders question works
  - one task across woocommerce + notes works
  - no URL or credential fields are visible
- Revoked key → the next MCP session is rejected; raw request → HTTP 401.
- Disabled `utils` → its tools vanish from the client, from the chat agent's tools, and from what the agent can call. Re-enabled → connected.
- `notes-mcp` scaled to 0 → the dashboard says *Unreachable (connection refused or host unreachable)*, a call returns *"Upstream 'notes' is unreachable (…)"*, other servers keep working, and its tools are withheld.
- **Audit:**
  - A's chat calls are attributed to A with source `chat`; B's MCP calls to B with source `mcp`.
  - The failed call to the dead server is logged with its outcome.
  - Members see only their own rows.

## Bugs found and fixed while verifying
The code as received had never been run on a cluster. These came up only when it was:
1. **Chart failed to render** (`gateway.yaml`: a block mapping inside a flow mapping).
2. **The gateway image would crash on start.** Its Dockerfile didn't install `python-multipart`, which the OAuth form endpoints require. All Dockerfiles now install pinned `requirements.txt` files, so images match tests.
3. **The store never finished setting up.** The newest WooCommerce requires WordPress 7.0 but the chart ships 6.7. WooCommerce is now pinned (`store.woocommerceVersion`). WP-CLI also had no writable `HOME`.
4. **`values-local.yaml` used `pullPolicy: Never` for every image,** including WordPress, MariaDB and Postgres, which `make import` doesn't load. A fresh k3d install would have been stuck in `ErrImageNeverPull`. It's now `IfNotPresent`.
5. **Requests dropped during rolling restarts** (18 × 502 in one run): the endpoint was removed after the server stopped, and a new pod was Ready before the CNI allowed Traefik to reach it. Fixed with a `preStop` drain plus a readiness delay; 0 failures after.
6. **Two fresh gateway replicas raced on bootstrap,** and one crashed on a duplicate admin. Startup is now serialized by a Postgres advisory lock.
7. **A brand-new gateway pod could hide a healthy upstream for about 10 s.** Its first probe hit the same CNI timing. It now probes every 2 s for its first 30 s and isn't Ready before its first check.
8. **Calls to a known-dead upstream said "unknown tool".** They now say *"Upstream 'x' is unreachable (reason)"*.
9. **Upgrading WooCommerce or WordPress through Helm did nothing,** because the setup only installed when absent and core files live on the PVC. Both now converge to the configured versions, up or down.
10. **The backup hook:**
    - It hung on the first upgrade because hooks run before the PVC exists.
    - A hook-created PVC then broke `helm rollback` ("already exists").
    - A failed dump left an empty 20-byte archive.

    All three are fixed (see DESIGN → Upgrades).
11. **`restore-store.sh` was broken:** invalid JSON overrides, plus the same first-connection NetworkPolicy timing. Fixed and tested.
12. **`--atomic` rollback timed out** with a 90 s budget, because the rollback also runs the backup hook. The demo uses 2 minutes.
13. **`scale-demo.sh` claimed to show the shared rate limit but never tested it.** It now does.
14. **SSRF:** loopback was reported as "reserved", and IPv4-mapped IPv6 handling depended on the Python version. Now normalized explicitly and tested.
15. **OAuth ignored the RFC 8707 `resource` parameter.** It now rejects foreign resources.
16. **Chat:** tool schemas are normalized for stricter OpenAI-compatible providers, and in tool-search mode the trace now shows the real tool instead of `gateway__call_tool`.

### Found once a real LLM was connected (round 1)
17. **The chart's default model, Groq `llama-3.3-70b-versatile`, no longer exists,** so the chat agent would have failed on its first message. The default is now `openai/gpt-oss-120b` (`gpt-oss-20b` and `qwen3.8-27b` also verified to call tools).
18. **Groq's free tier allows 8,000 tokens per minute,** and a few quick multi-step chat turns hit it. The result was a bare "HTTP 502". The agent now waits out a 429 using the provider's own reset time, up to 65 s, instead of failing, and says so clearly if it can't. Tool results are minified JSON capped at 4,000 characters, since every later step re-sends them.
19. **Groq validates the model's tool calls against the schema** and returns HTTP 400 `tool_use_failed` when the model omits a required argument (seen with `notes__add_note` missing `title`). The agent now asks the model again, naming the tool and its required arguments, up to 3 times. The cross-server prompt went from 2 of 3 to 6 of 6 successful runs. Provider error text is now shown instead of a bare status code.
20. **Asked to "compare today's orders across my stores", the real model stopped after two of three stores.** The agent's system prompt now lists every WooCommerce store namespace the user can see.
21. **NetworkPolicies were not enforced after the VM rebooted** and the k3d cluster restarted: the policies existed, but k3s's policy controller programmed no rules, so store isolation silently failed. `docker restart k3d-urumi-server-0` fixed it, and `make netpol-check` now detects it in seconds. Run it before recording.
22. **`stores_check.py` expected raw order IDs in the answer.** A real model summarises, so the check now verifies the tool results and that the answer covers each store.

### Found while building the VPS deploy (round 3)
23. **Let's Encrypt certificates would never have been issued.** The chart's default-deny NetworkPolicies blocked Traefik from reaching cert-manager's temporary HTTP-01 solver pod, both in `urumi` and in every store namespace. Both now allow exactly that (port 8089, from the ingress controller's namespace, only for pods with cert-manager's solver label).
24. **Store certificates would fail even then:** cert-manager's solver creates a NodePort Service by default, and the store quota allows no NodePorts. The issuer now uses `serviceType: ClusterIP`.
25. **No HTTP→HTTPS redirect.** Session cookies are `Secure`, so logging in over plain `http://` silently failed. A Traefik middleware now redirects (only rendered when TLS is on and the Traefik CRDs exist).
26. **Hairpin NAT.** cert-manager checks the challenge URL from inside the cluster before asking the CA, and WordPress calls its own public URL for wp-cron and WooCommerce's Action Scheduler. On many clouds a pod can't reach the node's own public IP. The bootstrap adds a CoreDNS rule sending `*.<domain>` to Traefik's ClusterIP.
27. **WordPress couldn't reach its own URL** even with the DNS rule: its egress policy allowed only the public internet. It may now also reach the ingress controller (and nothing else in-cluster).
28. **Domain choice:** `sslip.io` (the usual no-DNS trick) isn't on the Public Suffix List, so every user shares one Let's Encrypt rate limit. The guide uses DuckDNS, which is.

### Found on the first run on a fresh Windows laptop (WSL + Docker Desktop)
29. **"Today's orders" missed orders placed between midnight and 05:30 in India.** The WooCommerce MCP server computed today's midnight in the store timezone (Asia/Kolkata) but sent it without an offset, and WooCommerce reads such a date in the WordPress site timezone (UTC). After local midnight, "today" therefore started at 00:00 UTC, five and a half hours too late. It now sends local midnight converted to UTC with `dates_are_gmt=true`. Checked against a real WooCommerce with the site timezone set to both UTC and Asia/Kolkata: the old request depends on the site timezone, the new one doesn't.

## Not verified (be honest about these)
- **A fresh internet-facing VPS with Let's Encrypt production.** Everything from cert-manager onward ran against Pebble (the same ACME protocol and HTTP-01 flow). The script's first steps (apt packages, installing k3s, building images on the server) couldn't run in this sandbox. The recorded demo runs on k3d; the VPS path is ready to run with one command and a step-by-step guide (`docs/DEPLOY_VPS.md`).
- **A real prompt in Claude Code or Cursor.** Claude Code connected and listed the server, but no prompt ran (no model credentials for it here). MCP Inspector covered the calls. Do this on camera.
- **Gemini or other providers.** Only Groq was run for real. The schema normalization is meant to keep stricter providers happy, but it hasn't been observed.
- **The browser checkout, upgrade/rollback, scaling and prod-values runs were done on k3s, not repeated on k3d.** It's the same chart and the same k3s inside.
