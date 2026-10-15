# Deploying to a real VPS (Azure for Students + DuckDNS)

This is the click-by-click path used for the public deployment. It costs nothing if you have Azure for Students credit
and needs no domain purchase. Any other Ubuntu VPS works the same way from step 3 on.

The end result: `https://store.<you>.duckdns.org` (the WooCommerce store), `https://app.<you>.duckdns.org` (dashboard,
chat, MCP endpoint at `/mcp`), and every store you create gets `https://<name>.<you>.duckdns.org`, all with real
Let's Encrypt certificates.

## 1. Create the VM (Azure portal, about 5 minutes)

[portal.azure.com](https://portal.azure.com) → **Virtual machines** → **Create** → **Azure virtual machine**.

| Field | Value |
|---|---|
| Subscription | Azure for Students |
| Resource group | Create new: `urumi` (deleting this group later deletes everything) |
| Virtual machine name | `urumi` |
| Region | One near you, e.g. *(Asia Pacific) Central India*. Student subscriptions only allow some regions: if you get *RequestDisallowedByAzure* or the size is unavailable, try another region. |
| Availability options | No infrastructure redundancy required |
| Security type | Standard |
| Image | **Ubuntu Server 24.04 LTS - x64 Gen2** |
| Size | **Standard_B2ms** (2 vCPU, 8 GB). Fallback: Standard_B2s (2 vCPU, 4 GB) runs the core and one extra store, but it's tight. |
| Authentication type | SSH public key |
| Username | `azureuser` |
| SSH public key source | Generate new key pair, key pair name `urumi-key` |
| Public inbound ports | Allow selected ports: **SSH (22), HTTP (80), HTTPS (443)** |

**Disks** tab: OS disk size **64 GiB**, type **Standard SSD**. (The default 30 GB fills up with images and builds.)

**Management** tab: leave auto-shutdown **off** while reviewers may be looking at it.

**Review + create** → **Create** → **Download private key and create resource**. Keep the `urumi-key.pem` file.
When it finishes, **Go to resource** and copy the **Public IP address**. It's static: it survives stop/start.

## 2. Get a free domain (DuckDNS, 2 minutes)

1. Go to [duckdns.org](https://www.duckdns.org) and sign in (GitHub or Google).
2. Type a subdomain, e.g. `urumi-yourname`, and click **add domain**.
3. In the **current ip** box next to it, paste the VM's public IP and click **update ip**.

DuckDNS also answers for every name under yours (`app.urumi-yourname.duckdns.org`, `shop2.urumi-yourname.duckdns.org`…),
which is exactly what the multi-store bonus needs. `duckdns.org` is on the Public Suffix List, so Let's Encrypt rate limits
are per your subdomain, not shared with other DuckDNS users.

Check it from your laptop: `nslookup app.urumi-yourname.duckdns.org` should print the VM's IP.

## 3. Get the code onto the VM

From the folder with the key (on Windows, PowerShell has `ssh` built in):

```bash
chmod 600 urumi-key.pem             # macOS/Linux only
ssh -i urumi-key.pem azureuser@<PUBLIC_IP>
```

Then, on the VM, either clone your GitHub repo:

```bash
git clone https://github.com/<you>/urumi-mcp-gateway.git && cd urumi-mcp-gateway
```

or copy the zip from your laptop (`scp -i urumi-key.pem urumi-mcp-gateway.zip azureuser@<PUBLIC_IP>:`) and on the VM:
`sudo apt-get install -y unzip && unzip urumi-mcp-gateway.zip && cd urumi-mcp-gateway`.

## 4. Run one command

```bash
sudo DOMAIN=urumi-yourname.duckdns.org EMAIL=you@example.com LLM_API_KEY=gsk_... ./deploy/vps/bootstrap.sh
```

- `EMAIL` is used for the Let's Encrypt account and as the gateway admin login.
- `LLM_API_KEY` is your Groq key (it goes only into a Kubernetes Secret on the VM, never into a file in the repo).
  Omit it and chat is disabled; everything else works.
- `STORES=1` is the default (multi-store bonus on). `HOSTED=1` also enables hosted MCP servers.

It takes about 15–20 minutes the first time, most of it building the images. It runs 9 numbered steps: checks
(RAM, and that DNS points at this VM), packages, k3s, images, cert-manager + Let's Encrypt, secrets, Helm install,
certificates, and finally the same checks as the local demo over HTTPS. At the end it prints the URLs and the admin
password. **Save the password.**

The script is safe to re-run (it only changes what's missing). To deploy new code: `git pull` and run the same command.

## 5. If something fails

| Symptom | Cause and fix |
|---|---|
| `DNS for app.… does not point at this server` | DuckDNS IP not updated, or not propagated yet. Fix the IP, wait a minute, re-run. |
| Certificates not ready after 6 minutes | Port 80 not open (VM **Networking** → add an inbound rule for 80), or DNS wrong. The script prints the ACME challenge state. |
| Let's Encrypt `rateLimited` | You re-created the cert too often. Wait, or test with `ACME_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory` (browser warnings, but it proves the path). |
| Pods `Pending`, `Insufficient memory` | The VM is too small for the number of stores. Delete a store or resize to B2ms. |
| Anything else | `sudo kubectl -n urumi get pods`, `sudo kubectl -n urumi logs deploy/gateway`. |

## 6. Saving credit

A B2ms uses roughly $2 of credit a day while it runs. When nobody needs it, **Stop** the VM in the portal (status
*Stopped (deallocated)*): compute billing stops, the disk and the IP are kept, and **Start** brings everything back
(k3s starts on boot; give it 2–3 minutes). When you're completely done, delete the `urumi` resource group.
