# System design and tradeoffs

## Components

```
 Browser ──► Ingress (Traefik) ─┬─ store.<domain> ─► WordPress + WooCommerce (+ wp-setup sidecar) ─► MariaDB (StatefulSet, PVC)
                                └─ app.<domain> ─┬─ /                              ─► dashboard (nginx, static React)
 MCP client ─────────────────────────────────────┴─ /api /mcp /oauth /.well-known  ─► gateway (Deployment, 1..N pods)
 (Claude Code / Cursor / Inspector)                                                       │  Streamable HTTP + per-server credential
                                                                     ┌────────────────────┼──────────────────────┐
                                                                     ▼                    ▼                      ▼
                                                        woo-mcp ─REST wc/v3─► WP    notes-mcp (PVC)        utils-mcp     <store>/woo-mcp …
 gateway ─► Postgres (StatefulSet, PVC): users, API-key hashes, OAuth, servers + encrypted credentials, tool policies, audit,
            rate counters, stores
 store-provisioner (bonus) ─► Kubernetes API: one namespace per created store
```

| Piece | Responsibility |
|---|---|
| WordPress + WooCommerce | The store. A `wp-setup` sidecar (WP-CLI) installs WordPress and the pinned WooCommerce version, enables Cash on delivery, installs the REST API key from the Secret, and converges versions on upgrade. The pod is Ready only after setup completes. |
| woo-mcp | Our MCP server: 5 tools over the WooCommerce REST API. ClusterIP only; only the gateway may call it. |
| notes-mcp, utils-mcp | Two more upstreams, so the gateway really fans out. Both expose `ping`, to make name collisions visible. |
| gateway | MCP client to every upstream **and** MCP server for clients. Users, roles, API keys, OAuth, encrypted upstream credentials, tool policies, audit, rate limit, chat agent, tool search, hosted servers, store API. |
| dashboard | Static React app on the same origin as the API (cookie session, no tokens in JS). |
| store-provisioner | (bonus) A reconcile loop turning rows of the `stores` table into namespaces. The only component with cluster-scoped rights. |

## Key decisions

**The MCP server talks to WooCommerce through the REST API (`wc/v3`) with a scoped consumer key.** It's the supported interface: it validates input and fires WooCommerce hooks (stock, emails, order notes) exactly like the admin UI. It works over the network, so the MCP pod needs neither the WordPress filesystem nor database credentials, and the key can be revoked in WooCommerce. The alternatives:
- **WP-CLI** would need `exec` or shared-volume access into the WordPress pod.
- **Direct SQL** bypasses business rules and needs DB credentials.

WooCommerce only accepts Basic-auth keys on "HTTPS" requests, so in-cluster calls send `X-Forwarded-Proto: https` and `wp-config.php` honours it; that's the same mechanism a TLS-terminating ingress uses. `WC_AUTH_MODE=query` is a fallback. The key itself is generated into the Secret and inserted by the setup sidecar (WooCommerce stores only its HMAC).

**Streamable HTTP, stateless, one short-lived session per operation.** The gateway opens `initialize → request → close` per upstream call.
- There's no long-lived connection to go stale, and a dead upstream can't wedge the gateway.
- Any number of gateway replicas need no shared session state.
- The cost is a few extra in-cluster round trips per call (milliseconds).

The gateway's own `/mcp` is also stateless (`stateless_http`), so any replica serves any request behind a plain Service. Verified: SDK sessions whose requests were spread over 3 replicas all succeeded.

**Tool aggregation and naming.** The exposed name is `<server-slug>__<tool>`. Slugs are unique, so collisions are impossible by construction (`notes__ping` vs `utils__ping`, `woocommerce__list_orders` vs `chai-corner__list_orders`). The prefix is always applied, not only on collision, so names don't change when another server is added. Names over 64 characters (a common client limit) get a short hash suffix, and the routing table maps exposed name → (server, original tool). Descriptions are prefixed with `[Server name]`, so the model can tell stores apart.

**Failure isolation.**
- **Listing.** `tools/list` is served from a cache refreshed by a background loop (every 10 s, every 2 s for a pod's first 30 s), plus an immediate probe of never-seen servers. Each probe has its own timeout and they run concurrently, so one slow server never delays the others. A gateway pod isn't Ready until its first check finishes.
- **Calling.** A call has `UPSTREAM_CALL_TIMEOUT` (30 s by default). On timeout or connection failure the caller gets an MCP tool error such as *"Upstream 'notes' is unreachable (connection refused or host unreachable)"*, and the server flips to Unreachable at once.
- **Unreachable servers.** Their tools are withheld from the list, but a call to one still gets the clear "unreachable (reason)" error, and the dashboard shows the reason.
- **Redirects** from upstreams are never followed (SSRF).

**Auth model.**
- **Dashboard:** email + password (scrypt) → an HMAC-signed, HttpOnly, SameSite=Lax cookie (Secure behind TLS).
- **MCP clients:** `Authorization: Bearer umk_…` API keys, stored as SHA-256 (keys are 256-bit random, so a fast hash is correct), **or** OAuth access tokens (below).

Revocation and user-disable take effect on the next request. The client's token is never forwarded upstream; each upstream has its own stored credential (see the threat model).

**The chat agent is "through the gateway" in the sense that matters.** It runs in the gateway process but calls the same `GatewayService` with the logged-in user's `Principal` and `source="chat"`, so roles, tool policies, rate limits, timeouts and audit are identical to an external client, and nothing else talks to upstreams. The loop:
1. Fetch the user's visible tools (or the 3 search meta-tools above the threshold).
2. Send them to the LLM.
3. Execute each `tool_call` through the service and append the results.
4. Repeat, up to 6 steps.

Schemas are normalized to what every OpenAI-compatible provider accepts. The browser can only send user and assistant text, so it can't inject system or tool messages.

Real-provider behaviour the loop handles, all seen with Groq's free tier:
- **Rate limits.** A 429 is waited out using the provider's reset time, up to 65 s.
- **Rejected tool calls.** A tool call the provider refuses as invalid (`tool_use_failed`, e.g. a missing required argument) is retried with a hint naming the required arguments.
- **Token cost.** Tool results are minified and capped, since every later step re-sends them.
- **Multiple stores.** The system prompt lists every store namespace the user can see, so "compare my stores" covers all of them.

**Persistence.** MariaDB and the WordPress files on PVCs, Postgres on a PVC, notes on a PVC, store DB dumps on their own PVC.

## Stand-out designs

**Tool permissions.** A `ToolPolicy` row restricts one upstream tool to admins. Restricted tools are dropped from that user's `tools/list`, from the chat agent's tool list and from search results, and a direct call is rejected and audited as `denied`. With no row, the tool is open to every authenticated user.

**OAuth.** The gateway is its own authorization server for its own resource: one audience, no token passthrough. The flow:
1. A client gets a `401` with `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"` (RFC 9728).
2. It reads the authorization-server metadata (RFC 8414).
3. It registers dynamically (RFC 7591; public clients, exact redirect URIs).
4. It runs authorization code + PKCE (S256 only). A `resource` other than this gateway's `/mcp` is rejected (RFC 8707).

The browser page signs in with the dashboard email and password on every authorization (never a cookie), so it can't be driven by CSRF. Access tokens (1 h) and refresh tokens (30 d, **rotated** on every use; a reused one is refused) are opaque and stored hashed. API keys and OAuth tokens resolve to the same `Principal`.

**Many tools.** In search mode, `tools/list` returns three meta-tools: `gateway__search_tools(query)`, `gateway__describe_tool(name)` and `gateway__call_tool(name, arguments)`. `call_tool` re-enters `GatewayService.call_tool`, so permissions, credentials, timeouts, rate limit and audit are unchanged, and audit rows show the real server and tool. Search only ranks the caller's already-filtered tools, using keyword scoring on names and descriptions: predictable and dependency-free. Embeddings would be the next step for fuzzy queries. Mode is per connection (`X-Gateway-Tools: search|full` or `?tools=`) with a chart default (`auto` switches above 40 visible tools), and the chat agent uses the same logic.

**Hosted MCP servers.** The gateway creates a Secret, NetworkPolicy, Deployment and Service in its own namespace via server-side apply. Server-side apply is idempotent, so a failed create is simply retried, and a failure triggers best-effort teardown. The workload is non-root, read-only, capability-free, without a service-account token, resource-limited, reachable only from gateway pods, and its egress is limited to DNS and the public internet. Images must match an allowlisted prefix. While the pod boots the server shows *Starting*, not *Unreachable*. Delete removes the four objects first and keeps the registration if teardown fails, so it can be retried. It's off by default because it gives the gateway a namespaced Role.

**Security hardening.**
- **SSRF guard** on every admin-supplied URL, re-checked before every connection:
  - http(s) only, no userinfo
  - every resolved address checked, with IPv4-mapped IPv6 judged by its IPv4
  - link-local and metadata always blocked
  - private and loopback blocked unless the operator allow-listed that host pattern
- **Upstream credentials** encrypted at rest with Fernet; the key lives in a Secret.
- **Default-deny ingress NetworkPolicies** plus explicit egress policies per workload.
- **Containers** run non-root with a read-only root filesystem, all capabilities dropped and seccomp RuntimeDefault. The exception is WordPress, which needs root to start Apache on :80 and keeps a minimal capability set.
- **No service-account tokens**, except where a component really talks to the API.

**Scaling.**
- Stateless MCP sessions.
- A **rate limit shared across replicas**: a fixed one-minute counter row per user in Postgres, one atomic `UPDATE … count+1` per call (verified: exactly one budget across 3 replicas). If the DB write fails, the replica falls back to its own in-memory limit rather than blocking traffic.
- An HPA (prod) with a PDB and topology spread.
- Startup schema creation and bootstrap serialized by a Postgres advisory lock (without it, two fresh replicas raced to create the admin and one crashed).
- Zero-downtime rollouts: `maxUnavailable: 0`, a `preStop` sleep so the endpoint leaves Traefik before the server stops, and a 5 s readiness delay so the CNI has programmed the new pod's NetworkPolicy before traffic arrives. Measured: 0 failed requests out of about 5,500 across rolling restarts.

**Observability.**
- `/metrics` in Prometheus format, scraped in-cluster and not routed by the Ingress: `gateway_tool_calls_total{server,tool,outcome}`, `gateway_tool_call_seconds{server}`, `gateway_upstream_up{server}`, `gateway_upstream_tools{server}`.
- The *Health* page: calls, error rate, p50/p95 per server from the audit log.
- Every unreachable server and failed call carries a human reason.
- Optional ServiceMonitor and alert rules for the Prometheus Operator.

**Upgrades and rollback.**
- `helm upgrade` is the only deploy path, and `--atomic` rolls back a failed upgrade automatically. The rollback reuses `--timeout` and runs the backup hook first, so allow about 2 minutes.
- **The store is declarative.** The WordPress core on the PVC follows `images.wordpress`: the WordPress container records its image's version on the volume and the sidecar runs `wp core update` to it. WooCommerce follows `store.woocommerceVersion`, and the sidecar installs that version and runs `wp wc update` for DB migrations. Both work up and down.
- **Backups.** A `pre-upgrade,pre-rollback` hook Job dumps the store database to the `store-backups` PVC. It fails the upgrade if the dump fails, rather than writing an empty file. `scripts/restore-store.sh` restores a dump.
- **Helm subtleties handled.** The backup PVC is a normal resource with `resource-policy: keep`. A hook PVC would be re-created on rollback and fail with "already exists". The dump Job is only rendered once that PVC exists, because hooks run before resources are applied. A per-revision bind Job binds it, so `--wait` doesn't hang on a WaitForFirstConsumer volume.
- Schema changes are additive (`create_all`). A destructive change would need a migration first.

## Bonus: multi-store provisioning

**The gateway records intent; a separate controller acts.** `POST /api/stores` validates the name, enforces *max stores per user*, generates the store's secrets (encrypted in the row) and inserts a `stores` row in `provisioning` with a **deadline**. The internet-facing gateway never gets cluster-wide permissions.

The **store-provisioner** (same image, `python -m app.provisioner`, its own ServiceAccount) runs a reconcile loop every 5 s over all rows, concurrently:

| Row status | What the loop does |
|---|---|
| `provisioning` | Server-side apply of every object (below). When MariaDB, WordPress (setup complete) and woo-mcp are Ready, it **registers the store's MCP server** in the gateway's `servers` table (slug = store slug, bearer credential = the store's generated MCP token, encrypted) and marks the row `ready`. If the deadline passes, the row becomes `failed` with a reason built from pod states and warning events, e.g. *"exceeded quota: store-quota, requested: requests.cpu=125m"* or *"ImagePullBackOff"*. |
| `ready` | Re-applied every 5 minutes (self-healing). If the namespace vanished, the row becomes `failed`. |
| `deleting` | Removes the registration first (the API already disabled it, so the tools vanish at once), then deletes the namespace. That cascades to every object, including PVCs (whose volumes are deleted) and Secrets. Once the namespace is gone, the row is removed. |
| `failed` | Nothing. The user can Retry (new deadline) or Delete. |

**Per store, in namespace `store-<slug>`:**
- the Namespace, labelled with Pod Security `baseline` enforced
- a **ResourceQuota** (CPU, memory, pods, PVCs, storage; no LoadBalancers or NodePorts) and a **LimitRange**
- a Secret and a ConfigMap; the bootstrap scripts are the **same files** (`deploy/charts/urumi/files/`) the main store uses
- **NetworkPolicies** (default deny):
  - WordPress accepts traffic from Traefik and its own woo-mcp, and may reach its own MariaDB, the internet (plugin downloads) and the ingress controller (calls to its own URL: wp-cron, Action Scheduler)
  - with TLS on, cert-manager's HTTP-01 solver pod accepts traffic from the ingress controller on 8089
  - MariaDB accepts traffic from its own WordPress only
  - woo-mcp accepts traffic only from gateway pods in the platform namespace, and may reach only its own WordPress
- a MariaDB StatefulSet and PVC, a WordPress Deployment, PVC and setup sidecar, a woo-mcp Deployment, Services, and an Ingress for `<slug>.<domain>` (with TLS if enabled)

**Why it's safe to retry and to restart.**
- Every write is a full server-side apply, so it's idempotent.
- Secrets are generated once and persisted, so re-applying never rotates them.
- The deadline is persisted, so a provisioner restarted mid-provisioning resumes and the store either completes or fails with a reason (verified by killing it).
- A Postgres **advisory lock** makes one replica the leader.
- **Optimistic locking** (a version column) on the row means "user clicked Delete" and "provisioner marks Ready" can't overwrite each other; the loser retries on the next loop (unit-tested).

**Least privilege for the one component with cluster-scoped rights.** Its ClusterRole can create, patch and delete the kinds a store needs, but **can't read Secrets** anywhere (it only writes them). RBAC can't say "only namespaces named `store-*`", so a **ValidatingAdmissionPolicy** rejects any create, update or delete by that ServiceAccount outside `store-*` namespaces (verified: `kube-system`, `urumi` and new non-store namespaces are denied).

**Tool naming.** A store's tools are `<slug>__list_orders` and so on, with descriptions prefixed `[Store: <name>]`, so it's clear which store a tool acts on. The chat agent's system prompt says to call the same tool on each store to compare them.

## What changes for production (values only)

| Concern | Local (`values-local.yaml`) | VPS (`values-prod.yaml`) |
|---|---|---|
| DNS | `*.urumi.localtest.me` → 127.0.0.1 (public wildcard DNS) | A records `store.`, `app.` (and `*.` for stores) → VPS IP |
| Ingress / TLS | k3d's Traefik on host :80 | k3s Traefik, TLS via cert-manager (`ingress.tls.*`), `scheme: https` → Secure cookies, https OAuth metadata |
| Storage | `local-path` default | `storage.className`, larger sizes |
| Secrets | chart generates random values, kept stable across upgrades via `lookup` | `secrets.create=false`; `deploy/prod/create-secrets.sh` (or SOPS / sealed-secrets) |
| Images | `urumi/*:local`, loaded with `k3d image import` | registry tags |
| Gateway | 1 replica | 2 replicas, HPA 2–6, PDB |

Verified by installing `values-prod.yaml` next to the local release on k3s with cert-manager. Only the domain, image tags and issuer were overridden for the test environment.

## Known tradeoffs and limits
- Fixed-window rate limit: one DB write per call, and a burst of up to 2× at a window boundary. A token bucket in Redis would remove both.
- The tool-list cache is per replica, so replicas may disagree for up to one health interval.
- WordPress runs as root to bind :80 (official image) with a minimal capability set; our own containers are non-root and read-only.
- One WordPress replica per store (ReadWriteOnce volume), so a store upgrade means a short storefront outage. Scaling WordPress would need ReadWriteMany or object storage for uploads.
- Schema management is `create_all` (additive only).
- The chat agent doesn't stream; history lives in the browser tab.
- Store teardown takes as long as the namespace's finalizers (about 45 s here).
