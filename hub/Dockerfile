# Build from the repository root:  docker build -f hub/Dockerfile -t newvms-hub .
# Stage 1: the two UI bundles (site UI served under /s/<site>/, hub UI at /)
FROM node:22-alpine AS ui
WORKDIR /src
COPY frontend/package*.json frontend/
RUN cd frontend && npm ci --no-audit --no-fund
COPY frontend frontend
RUN cd frontend && npm run build
COPY hub/ui/package*.json hub/ui/
RUN cd hub/ui && npm ci --no-audit --no-fund
COPY hub/ui hub/ui
RUN cd hub/ui && npm run build

# Stage 2: the hub service
FROM python:3.12-slim
WORKDIR /app
COPY tunnelproto tunnelproto
COPY hub/pyproject.toml hub/pyproject.toml
COPY hub/hub hub/hub
RUN pip install --no-cache-dir ./tunnelproto ./hub
COPY --from=ui /src/frontend/dist frontend/dist
COPY --from=ui /src/hub/ui/dist hub/ui/dist
ENV HUB_SITE_UI_DIR=/app/frontend/dist HUB_UI_DIR=/app/hub/ui/dist HUB_HOST=0.0.0.0 HUB_PORT=8000
EXPOSE 8000
CMD ["python", "-m", "hub"]
