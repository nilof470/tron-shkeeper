# SHKeeper TRON Deployment Runbook

This guide records the deployment procedure used for the resource-provider
enabled `tron-shkeeper` fork. It is written so the same process can be repeated
for production without relying on chat history.

Do not commit real API keys, wallet passwords, GitHub tokens, or generated
Kubernetes secrets. Keep `/root/shkeeper-values.yaml` on the target server or in
a private secret store.

## Deployment Shape

Use the official SHKeeper deployment model:

- k3s on the VPS
- Helm chart `vsys-host/shkeeper`
- custom private GHCR image for `tron-shkeeper`
- Kubernetes `imagePullSecret` for the private image
- re:Fee or ProfeeX as the TRC20 energy provider
- ProfeeX as the onetime-wallet bandwidth provider

The base chart runs the TRON sidecar as one pod with three containers:

- `app`: `gunicorn run:server`
- `tasks`: `celery -A celery_worker.celery worker ...`
- `redis`: local Redis for the sidecar

When `TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED=true`, run one additional
Celery worker slot that consumes only `tron_usdt_fee_payouts` with
`--concurrency=1 --prefetch-multiplier=1`. The normal `tasks` worker must keep
consuming the default `celery` queue for scanner, sweep, AML, and non-USDT
payout work.

## Local Release Build

Run from the local repository checkout.

```bash
cd /Users/test/PycharmProjects/tron-shkeeper
git checkout master
git pull origin master
git status --short --branch
TAG=$(git rev-parse --short HEAD)
echo "$TAG"
```

Run tests before building:

```bash
/tmp/tron-shkeeper-py312-venv/bin/python -m unittest discover -s tests
```

Log in to GHCR once per workstation session if needed. Use a GitHub token with
`repo`, `write:packages`, and `read:packages` for private packages.

```bash
docker login ghcr.io -u nilof470
```

Build and push the `linux/amd64` image:

```bash
docker buildx build \
  --platform linux/amd64 \
  -t ghcr.io/nilof470/tron-shkeeper:${TAG} \
  --push .
```

Verify the remote manifest:

```bash
docker buildx imagetools inspect ghcr.io/nilof470/tron-shkeeper:${TAG}
```

Record the tag and digest in the release notes. Example:

```text
ghcr.io/nilof470/tron-shkeeper:5a6133b
sha256:48fbe2727c428965e4b74baccb29bd3aefcbdba3c0b15aeee57c134e04cef281
```

## VPS Preflight

If replacing another stack such as Bitcart, stop and remove it before
installing SHKeeper. These commands are destructive for that old stack.

```bash
docker ps -a
docker compose ls
systemctl stop bitcart.service || true
systemctl disable bitcart.service || true
rm -f /etc/systemd/system/bitcart.service
rm -f /etc/profile.d/bitcart-env.sh
systemctl daemon-reload
rm -rf /root/bitcart-docker
```

Check that required ports are free and the server has enough disk and memory:

```bash
ss -ltnp | grep -E ':(80|443|5000)\b' || true
df -h
free -h
```

## Install k3s and Helm

Run as `root` on the VPS.

```bash
curl -sfL https://get.k3s.io | sh -
mkdir -p /root/.kube
ln -sf /etc/rancher/k3s/k3s.yaml /root/.kube/config
chmod 600 /etc/rancher/k3s/k3s.yaml
kubectl get nodes
```

Install Helm and add chart repositories:

```bash
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm version

helm repo add vsys-host https://vsys-host.github.io/helm-charts
helm repo add mittwald https://helm.mittwald.de
helm repo update
```

Install the secret generator used by the official chart:

```bash
helm install kubernetes-secret-generator mittwald/kubernetes-secret-generator
helm list -A
```

## Namespace and Private GHCR Pull Secret

The chart creates a namespace object, but the pull secret must exist before the
TRON sidecar pod pulls the private image. Pre-create the namespace and annotate
it so Helm can adopt it.

```bash
kubectl create namespace shkeeper --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace shkeeper app.kubernetes.io/managed-by=Helm --overwrite
kubectl annotate namespace shkeeper \
  meta.helm.sh/release-name=shkeeper \
  meta.helm.sh/release-namespace=default \
  --overwrite
```

Create the GHCR pull secret. Paste the GitHub token when prompted; it will not
be displayed.

```bash
read -s GHCR_TOKEN

kubectl -n shkeeper create secret docker-registry ghcr-nilof470 \
  --docker-server=ghcr.io \
  --docker-username=nilof470 \
  --docker-password="$GHCR_TOKEN" \
  --docker-email=none@example.com \
  --dry-run=client -o yaml | kubectl apply -f -

unset GHCR_TOKEN
kubectl get secret -n shkeeper ghcr-nilof470
```

## Helm Values

Create `/root/shkeeper-values.yaml`. Replace placeholders before installing.

For production, set `domain` to the public hostname and point DNS to the VPS.
For a temporary dev install, `domain: ""` and direct port `5000` access are
acceptable.

```yaml
namespace: shkeeper
storageClassName: local-path
domain: ""

dev:
  imagePullSecrets:
    - name: ghcr-nilof470

btc:
  enabled: false
ltc:
  enabled: false
doge:
  enabled: false

tron_fullnode:
  enabled: false
  url: http://fullnode.tron.shkeeper.io
  mainnet: true

tron_shkeeper:
  image: ghcr.io/nilof470/tron-shkeeper:REPLACE_WITH_TAG
  extraEnv:
    ENERGY_PROVIDER: profeex
    BANDWIDTH_PROVIDER: profeex
    PROFEEX: '{"api_key":"REPLACE_WITH_PROFEEX_API_KEY","energy_duration_label":"1h","bandwidth_duration_label":"1h","currency":"TRX","fixed_energy_order_amount":65000,"fixed_bandwidth_order_amount":350}'
    TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED: "true"
    TRON_USDT_PAYOUT_QUEUE: tron_usdt_fee_payouts
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT: "false"
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH: "true"
    USDT_MIN_TRANSFER_THRESHOLD: "0.5"
    TRX_MIN_TRANSFER_THRESHOLD: "1.01"

trx:
  enabled: true
usdt:
  enabled: true
usdc:
  enabled: false
```

Notes:

- `ENERGY_PROVIDER=refee` rents TRC20 transfer energy from re:Fee.
- `ENERGY_PROVIDER=profeex` rents TRC20 transfer energy from ProfeeX.
- `BANDWIDTH_PROVIDER=profeex` rents onetime-wallet bandwidth from ProfeeX only
  when the wallet does not already have enough bandwidth for the TRC20 transfer.
- `BANDWIDTH_PROVIDER=disabled` preserves the old behavior: the sweep uses only
  bandwidth already available on the onetime wallet and retries naturally after
  TRON restores daily bandwidth.
- ProfeeX ordinary energy rental uses `/api/v1/delegation/buyenergy`.
- ProfeeX ordinary bandwidth rental uses `/api/v1/delegation/buybandwidth`.
  Flash resources are not used because they require the target address to have
  its own consumed staked resources.
- `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=false` prevents fallback to
  funding onetime wallets for TRC20 transfer fee burn if re:Fee fails. This
  fallback remains re:Fee-only and is not used for ProfeeX failures.
- `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH=true` allows TRX burn for
  account activation bandwidth. Keep this only if activation burn is acceptable.
- `REFEE_FIXED_ENERGY_ORDER_AMOUNT=65000` ensures at least 65k energy is
  available before a USDT sweep. Set it to `0` to return to fullnode
  estimate-based sizing. Nonzero values must be greater than or equal to the
  configured re:Fee `min_energy_order_amount`.
- `USDT_MIN_TRANSFER_THRESHOLD` must be lower than the smallest USDT payment that
  should be swept. The TRC20 sweep check requires `balance > threshold`.
- `TRX_MIN_TRANSFER_THRESHOLD` prevents sweeping activation dust. TRX sweep uses
  `balance >= threshold`, so use a value above dust, for example `1.01`.

### TRON resource provider environment variables

Use these variables in `tron_shkeeper.extraEnv` to control energy and bandwidth
provisioning independently:

| Env var | Default | Required when | Meaning |
| --- | --- | --- | --- |
| `ENERGY_PROVIDER` | `staking` | Always optional | Energy provider selector for TRC20 sweeps. Allowed values: `staking`, `refee`, `profeex`. `staking` is active only when `ENERGY_DELEGATION_MODE=true`; with the default `ENERGY_DELEGATION_MODE=false`, the sidecar uses the legacy TRX burn funding flow. |
| `BANDWIDTH_PROVIDER` | `disabled` | Always optional | Bandwidth provider for the onetime wallet before energy provisioning. Allowed values: `disabled`, `refee`, `profeex`. |
| `REFEE` | empty | `ENERGY_PROVIDER=refee` or `BANDWIDTH_PROVIDER=refee` | re:Fee JSON config with `api_key` and optional duration/order settings. |
| `PROFEEX` | empty | `ENERGY_PROVIDER=profeex` or `BANDWIDTH_PROVIDER=profeex` | ProfeeX JSON config with `api_key`, duration, currency, and fixed order settings. |
| `REFEE_FIXED_ENERGY_ORDER_AMOUNT` | `65000` | Optional | Fixed re:Fee energy order amount. Use `0` to size from the fullnode estimate. |
| `TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED` | `false` | Optional | Enables fee-deposit resource estimation and provisioning before single USDT payout from the TRON fee wallet. Requires `PROFEEX` because ProfeeX provides the destination-specific USDT energy estimate. |
| `TRON_USDT_PAYOUT_QUEUE` | `tron_usdt_fee_payouts` | Optional | Dedicated Celery queue for single USDT payouts from the fee wallet. The queue must have exactly one worker slot. |
| `TRON_USDT_PAYOUT_RESOURCE_LOCK_TTL_SEC` | `900` | Optional | Redis lock TTL for serializing single USDT payout resource provisioning and transfer. |
| `TRON_USDT_PAYOUT_RESOURCE_LOCK_WAIT_SEC` | `900` | Optional | Maximum time a concurrent single USDT payout task waits for the resource lock before failing. |

`BANDWIDTH_PROVIDER=disabled` is the old no-rental behavior: the sidecar uses
only bandwidth already available on the onetime wallet. If there is not enough
bandwidth, the sweep stops before energy provisioning and retries later after
TRON daily bandwidth recovery or manual delegation.

`ENERGY_PROVIDER=profeex` uses ordinary ProfeeX energy delegation. With the
default `fixed_energy_order_amount=65000`, the app treats `64500` available
energy as sufficient to avoid duplicate fixed rentals.

`BANDWIDTH_PROVIDER=profeex` uses ordinary ProfeeX bandwidth delegation. It
does not rent bandwidth when the onetime wallet already has enough bandwidth.

When `TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED=true`, single USDT payouts
are routed to `TRON_USDT_PAYOUT_QUEUE`. A dedicated worker for this queue is
the primary ordering mechanism; the task also uses a Redis lock around
`ensure fee-deposit resources -> transfer` as a defensive guard against worker
misconfiguration. Do not enable the feature flag until a dedicated single-slot
worker consumes this queue.

Dedicated worker command:

```bash
celery -A celery_worker.celery worker -E --loglevel=info \
  -Q tron_usdt_fee_payouts --concurrency=1 --prefetch-multiplier=1 \
  -n tron-usdt-payouts@%h
```

The normal worker should consume the default queue explicitly when the payout
worker is split out:

```bash
celery -A celery_worker.celery worker -E --loglevel=info -Q celery
```

Do not use the old unshipped names `ENERGY_SOURCE` or `REFEE_RENT_BANDWIDTH` in
this build.

`PROFEEX` JSON fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `api_base_url` | `https://api.profeex.io/api/v1` | ProfeeX API base URL. Must be HTTPS. |
| `api_key` | required | ProfeeX API key. |
| `currency` | `TRX` | Payment currency for the order. Allowed values: `TRX`, `USDT`. |
| `energy_duration_label` | `1h` | Energy rental duration. Allowed values: `1h`, `1d`, `3d`, `7d`, `14d`. |
| `bandwidth_duration_label` | `1h` | Bandwidth rental duration. Allowed values: `1h`, `1d`, `3d`, `7d`, `14d`. |
| `fixed_energy_order_amount` | `65000` | Actual energy order size sent to ProfeeX. This is not an API min/max field. |
| `fixed_bandwidth_order_amount` | `350` | Actual bandwidth order size sent to ProfeeX. This is not an API min/max field. |
| `poll_interval_sec` | `2.0` | Poll interval while waiting for the ProfeeX task status. |
| `timeout_sec` | `60` | Timeout while waiting for the order to become `ACTIVE`. |

## Install SHKeeper

```bash
helm install -f /root/shkeeper-values.yaml shkeeper vsys-host/shkeeper
```

Watch startup:

```bash
kubectl get pods -n shkeeper
kubectl get pvc -n shkeeper
kubectl get svc -n shkeeper
kubectl get pods -n shkeeper -w
```

Expected core pods:

```text
mariadb                 1/1 Running
shkeeper-deployment     1/1 Running
tron-shkeeper           3/3 Running
```

The official chart can leave old failed `create-db-bitcoin-shkeeper` retry pods
even with BTC disabled. If the job has one `Completed` pod and the core pods are
running, those old failed pods are not a blocker.

Check local access from the VPS:

```bash
curl -I http://127.0.0.1:5000/
```

For dev direct access, open inbound TCP `5000` in the cloud firewall/security
group and browse:

```text
http://PUBLIC_VPS_IP:5000/wallets
```

For production, prefer DNS + HTTPS through the chart's Traefik ingress. Set
`domain` in `shkeeper-values.yaml`, open `80` and `443`, and avoid exposing
`5000` publicly.

## First-Time Admin Setup

Open the SHKeeper UI and set:

1. admin password
2. wallet encryption password

The wallet encryption password is stored only in RAM by SHKeeper. Save it in a
password manager. After SHKeeper restarts, the UI may ask for it again before
sidecars can decrypt wallet keys.

Verify the TRON sidecar received the key:

```bash
kubectl logs -n shkeeper deployment/tron-shkeeper -c app --tail=80
kubectl logs -n shkeeper deployment/tron-shkeeper -c tasks --tail=80
```

Expected lines:

```text
Wallet encryption is enabled, encryption key is set!
Encryption settings are valid.
celery@... ready.
```

## Fee Deposit Wallet

Get the TRON `fee_deposit` address:

```bash
kubectl exec -n shkeeper deployment/tron-shkeeper -c app -- python -c 'import os, requests; r=requests.post("http://127.0.0.1:6000/TRX/fee-deposit-account", auth=(os.environ["BTC_USERNAME"], os.environ["BTC_PASSWORD"]), timeout=20); print(r.status_code, r.text)'
```

Fund this address with TRX before testing or going live. It is used for
activation transfers and TRX payouts. In dev we used about `30 TRX`; production
should use an operator-defined reserve and monitoring.

## Resource Provider API Requirements

These checks apply only to the provider configured in `tron_shkeeper.extraEnv`.

### re:Fee

The re:Fee API key must allow requests from the VPS public IP. Get the IP:

```bash
curl -4 ifconfig.me
```

Add that IP to the re:Fee whitelist. Without this, energy rental fails with:

```text
403 {"detail":"Your IP is not on the user's whitelist"}
```

### ProfeeX

The ProfeeX API key is sent as `X-API-Key`. Verify it from the VPS or from the
TRON tasks container before testing sweeps:

```bash
curl -i \
  -H "X-API-Key: REPLACE_WITH_PROFEEX_API_KEY" \
  "https://api.profeex.io/api/v1/balance"
```

Expected response is HTTP `200` with a JSON `balances` object. HTTP `401`
means the key is invalid; HTTP `403` means the request was rejected before
credential validation, for example by upstream access policy.

## Create a Test USDT Deposit

In the SHKeeper UI, get the API key from the wallet management screen. Then
create a payment request from the VPS:

```bash
read -s SHKEEPER_API_KEY

curl -sS -X POST 'http://127.0.0.1:5000/api/v1/USDT/payment_request' \
  -H "X-Shkeeper-Api-Key: ${SHKEEPER_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "external_id": "dev-usdt-001",
    "fiat": "USD",
    "amount": "1",
    "callback_url": "https://example.com/shkeeper-callback"
  }'
```

The response contains a `wallet` field. Send the exact returned `amount` to that
address. SHKeeper may return a value such as `1.02` even when the requested fiat
amount is `1`.

Watch worker logs:

```bash
kubectl logs -n shkeeper deployment/tron-shkeeper -c tasks -f
```

Expected successful flow with `ENERGY_PROVIDER=profeex`:

```text
Balance OK
Activating ... by sending 0.1 TRX
0.1 TRX sent
Requesting ProfeeX energy rental
ProfeeX energy successfully delegated
... USDT sent to fee_deposit
```

With `ENERGY_PROVIDER=refee`, the provider-specific lines say
`Requesting re:Fee energy rental` and `re:Fee energy successfully delegated`.

If a retry is needed without waiting for the periodic scanner:

```bash
kubectl exec -n shkeeper deployment/tron-shkeeper -c tasks -- python -c 'from app.tasks import transfer_trc20_from; transfer_trc20_from.delay("ONETIME_ADDRESS", "USDT"); print("queued")'
```

The periodic balance scanner also retries stuck balances. Default interval:

```text
BALANCES_RESCAN_PERIOD=3600
```

## Updating an Existing VPS

After building and pushing a new image tag locally, update the VPS values file:

```bash
NEW_TAG=REPLACE_WITH_TAG

sed -i "s|image: ghcr.io/nilof470/tron-shkeeper:.*|image: ghcr.io/nilof470/tron-shkeeper:${NEW_TAG}|" /root/shkeeper-values.yaml

helm upgrade -f /root/shkeeper-values.yaml shkeeper vsys-host/shkeeper
kubectl rollout status deployment/tron-shkeeper -n shkeeper
```

Verify the deployed image:

```bash
kubectl get deployment tron-shkeeper -n shkeeper -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
kubectl get pods -n shkeeper
```

Expected image output shape:

```text
ghcr.io/nilof470/tron-shkeeper:TAG ghcr.io/nilof470/tron-shkeeper:TAG redis:7
```

## Useful Diagnostics

General state:

```bash
kubectl get pods -n shkeeper
kubectl get svc -n shkeeper
kubectl get pvc -n shkeeper
kubectl get events -n shkeeper --sort-by=.lastTimestamp | tail -80
```

Logs:

```bash
kubectl logs -n shkeeper deployment/shkeeper-deployment --tail=100
kubectl logs -n shkeeper deployment/tron-shkeeper -c app --tail=120
kubectl logs -n shkeeper deployment/tron-shkeeper -c tasks --tail=120
```

Sidecar API health:

```bash
kubectl exec -n shkeeper deployment/tron-shkeeper -c app -- python -c 'import os, requests; r=requests.post("http://127.0.0.1:6000/TRX/status", auth=(os.environ["BTC_USERNAME"], os.environ["BTC_PASSWORD"]), timeout=20); print(r.status_code, r.text)'
kubectl exec -n shkeeper deployment/tron-shkeeper -c app -- python -c 'import os, requests; r=requests.post("http://127.0.0.1:6000/USDT/balance", auth=(os.environ["BTC_USERNAME"], os.environ["BTC_PASSWORD"]), timeout=20); print(r.status_code, r.text)'
```

## Troubleshooting

### ImagePullBackOff for `ghcr.io/nilof470/tron-shkeeper`

Check the pull secret and image tag:

```bash
kubectl get secret -n shkeeper ghcr-nilof470
kubectl describe pod -n shkeeper -l app=tron-shkeeper
```

Confirm the GitHub token has `repo`, `write:packages`, and `read:packages` for a
private GHCR package.

### Wallet encryption waits forever

Logs show:

```text
Waiting for encryption key...
```

Open the SHKeeper UI and enter the wallet encryption password. Then re-check the
sidecar logs.

### `Threshold not reached`

Example:

```text
Has: 1 USDT need: 1 USDT
```

For TRC20 sweeps, the code requires `balance > threshold`. Set
`USDT_MIN_TRANSFER_THRESHOLD` below the smallest amount to sweep.

### Activation burns TRX

When an onetime address is not active, the sidecar sends `0.1 TRX` from
`fee_deposit` to activate it. If `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH`
is `true`, TRX may burn for the activation transfer bandwidth.

Keep `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=false` to prevent fallback
TRX burn for the USDT sweep itself when re:Fee fails. ProfeeX failures terminate
without using this re:Fee-only fallback.

### re:Fee 403 whitelist error

Add the VPS public IP from `curl -4 ifconfig.me` to the re:Fee whitelist, then
retry the sweep.

### `One-time account has no bandwidth`

The onetime address is active but lacks bandwidth for the TRC20 transfer. Wait
for bandwidth to recover, manually delegate/rent bandwidth to that address, or
retry after activation has settled.

### `UNIQUE constraint failed: settings.name` on first startup

This can appear during first scanner startup when the app initializes
`last_seen_block_num` concurrently. If later logs show scanner stats with
`eta=in sync`, it recovered and is not blocking.

### USDC encryption warnings while USDC is disabled

Warnings like this are noisy but not blocking when `usdc.enabled=false`:

```text
Ignoring notification for USDC: crypto is not available for processing
```

## Backup Notes

Before production traffic, define and test a backup procedure for:

- SHKeeper MariaDB data
- `tron-shkeeper` SQLite data under the sidecar PVC
- `/root/shkeeper-values.yaml`
- wallet encryption password
- admin password
- Resource provider API keys, for example re:Fee and ProfeeX
- GHCR pull token or replacement deployment token

Do not rely only on container images; wallet state lives in persistent volumes.
