# SHKeeper TRON Deployment Runbook

This guide records the deployment procedure used for the re:Fee-enabled
`tron-shkeeper` fork. It is written so the same process can be repeated for
production without relying on chat history.

Do not commit real API keys, wallet passwords, GitHub tokens, or generated
Kubernetes secrets. Keep `/root/shkeeper-values.yaml` on the target server or in
a private secret store.

## Deployment Shape

Use the official SHKeeper deployment model:

- k3s on the VPS
- Helm chart `vsys-host/shkeeper`
- custom private GHCR image for `tron-shkeeper`
- Kubernetes `imagePullSecret` for the private image
- re:Fee as the TRC20 energy provider

The chart runs the TRON sidecar as one pod with three containers:

- `app`: `gunicorn run:server`
- `tasks`: `celery -A celery_worker.celery worker ...`
- `redis`: local Redis for the sidecar

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
    ENERGY_SOURCE: refee
    REFEE: '{"api_key":"REPLACE_WITH_REFEE_API_KEY","rent_duration_label":"1h"}'
    REFEE_FIXED_ENERGY_ORDER_AMOUNT: "65000"
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

- `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=false` prevents fallback to
  funding onetime wallets for TRC20 transfer fee burn if re:Fee fails.
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

## re:Fee Requirements

The re:Fee API key must allow requests from the VPS public IP. Get the IP:

```bash
curl -4 ifconfig.me
```

Add that IP to the re:Fee whitelist. Without this, energy rental fails with:

```text
403 {"detail":"Your IP is not on the user's whitelist"}
```

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

Expected successful flow:

```text
Balance OK
Activating ... by sending 0.1 TRX
0.1 TRX sent
Requesting re:Fee energy rental
re:Fee energy successfully delegated
... USDT sent to fee_deposit
```

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
TRX burn for the USDT sweep itself when re:Fee fails.

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
- re:Fee API key
- GHCR pull token or replacement deployment token

Do not rely only on container images; wallet state lives in persistent volumes.
