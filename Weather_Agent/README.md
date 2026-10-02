# Weather Agent

A small agentic AI app: a Claude tool-calling loop that answers weather questions using live data, behind a React UI, deployed on a local k3d (Kubernetes) cluster.

- **Direct path:** pick a country and city in the UI, get live weather (no LLM call).
- **Agentic path:** "Ask the AI" sends a question to a Claude agent that decides whether to call the weather tool.

Design documentation (HLD and LLD, with diagrams): [docs/HLD_LLD.md](docs/HLD_LLD.md)

## Architecture

Two request paths share one weather tool. The direct path serves UI clicks with no LLM call; only the agentic path (blue) calls Claude.

![Architecture: two request paths, one shared tool](docs/diagrams/architecture.png)

## Deployment

Everything runs as two Deployments in one k3d cluster. nginx is the only workload exposed through the ingress, and it adds the backend key server-side so browsers never see it.

![Deployment: k3d cluster, two pods per service](docs/diagrams/deployment.png)

## Agent loop

The agent loop is about 50 lines of plain Python. Claude decides on each pass whether another tool call is needed; everything else is fixed code with three guards (step limit, deadline, provider errors).

![Agent loop: one decision, three guards](docs/diagrams/agent-loop.png)

## Layout

| Folder | Contents |
| --- | --- |
| `weather_service/` | FastAPI backend, agent loop, tests, behavioral evals, load test, Kubernetes manifest |
| `weather_ui/` | React (Vite) UI, nginx config, Kubernetes manifest |
| `docs/` | HLD and LLD document and diagrams |

## Prerequisites

- Docker, `kubectl`, and a k3d cluster with Traefik (the k3d default). The manifests assume a cluster named `dev` and the `default` namespace.
- Python 3.12+ and Node 22+ (for local development and tests)
- An Anthropic API key in `ANTHROPIC_API_KEY`

## Deploy to k3d

Run from this folder (`Weather_Agent/`).

1. Create the Secrets. Nothing secret is stored in this repository.

```bash
# backend key + the shared service key (nginx injects the service key; browsers never see it)
kubectl create secret generic weather-agent-secrets \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --from-literal=WX_API_KEYS="$(openssl rand -hex 16)"

# login for the UI (user: weather). Save the printed password somewhere safe.
PASS=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)
kubectl create secret generic weather-ui-basic-auth \
  --from-literal=.htpasswd="weather:$(openssl passwd -apr1 "$PASS")"
echo "UI password: $PASS"
```

2. Build the images and load them into the cluster.

```bash
docker build -t weather-agent:local weather_service
docker build -t weather-ui:local weather_ui
k3d image import weather-agent:local weather-ui:local -c dev
```

3. Deploy.

```bash
kubectl apply -f weather_service/k8s/weather-agent.yaml
kubectl apply -f weather_ui/k8s/weather-ui.yaml
kubectl rollout status deploy/weather-agent deploy/weather-ui
```

4. Open `http://weather.localtest.me:8080` (the k3d load balancer maps port 8080 to Traefik) and sign in as `weather`.

## Local development

Backend:

```bash
cd weather_service
pip install -r requirements.txt
python -m pytest -q                     # 43 offline tests, no network
python -m evals.run_evals               # behavioral evals against the real model (needs ANTHROPIC_API_KEY)
WX_API_KEYS=dev-key uvicorn app.main:app --port 8000
```

UI (proxies `/api` to the backend and adds the key server-side):

```bash
cd weather_ui
npm install
WX_API_KEY=dev-key npm run dev          # http://localhost:5173
```

## Configuration

All backend settings are environment variables with the `WX_` prefix (see `weather_service/app/config.py` and `.env.example`), for example `WX_MODEL`, `WX_MAX_STEPS`, `WX_CACHE_TTL_S` and `WX_RATE_LIMIT_PER_MIN`.

## Status

Production-style, not production-ready: see section 10 of the design document for known gaps (shared rate limits, circuit breaker, tracing, CI, registry).
