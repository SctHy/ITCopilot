# IT-Copilot 后端镜像：API（默认）、独立 worker 与初始化任务共用，由 compose 的 command 区分。
FROM python:3.14.10-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    PYDANTIC_AI_NO_BANNER=1 \
    FASTEMBED_CACHE_PATH=/home/app/.cache/fastembed \
    HOME=/home/app

RUN useradd --create-home --uid 10001 app
WORKDIR /app

ARG BUILD_SHA
ENV IT_COPILOT_BUILD_SHA=${BUILD_SHA}

COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
RUN uv sync --locked --no-dev --no-install-project

# data/ 不进入构建上下文（.dockerignore）；运行时数据目录需在切换到非 root 用户前创建并授权。
RUN mkdir -p /app/data && chown -R app:app /app/data

# 构建期预下载 embedding 模型（与 it_copilot.agent.embedding 默认值一致），避免首次请求时在线下载导致超时。
COPY --chown=app:app src ./src
RUN uv sync --locked --no-dev

USER app
RUN python -c "from fastembed import TextEmbedding; list(TextEmbedding('jinaai/jina-embeddings-v2-base-zh').embed(['预热']))"

COPY --chown=app:app . .

EXPOSE 8001
HEALTHCHECK --interval=15s --timeout=3s --start-period=40s --retries=5 \
  CMD python -c "import os,urllib.request; p=os.getenv('HEALTH_PORT','8001'); urllib.request.urlopen(f'http://127.0.0.1:{p}/readyz', timeout=2)"

CMD ["python", "-m", "it_copilot.cmd.api"]
