# IT-Copilot 后端镜像：API（默认）、独立 worker 与初始化任务共用，由 compose 的 command 区分。
FROM python:3.14.10-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_NO_CACHE=1 \
    PYDANTIC_AI_NO_BANNER=1 \
    FASTEMBED_CACHE_PATH=/home/app/.cache/fastembed \
    HOME=/home/app

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY requirements.txt ./
RUN uv pip install --system -r requirements.txt

# data/ 不进入构建上下文（.dockerignore）；运行时数据目录需在切换到非 root 用户前创建并授权。
RUN mkdir -p /app/data/chroma && chown -R app:app /app/data

# 构建期预下载中文 embedding 模型（与 core/embedding.py 默认值一致），避免首次请求时在线下载导致超时。
USER app
RUN python -c "from fastembed import TextEmbedding; list(TextEmbedding('BAAI/bge-small-zh-v1.5').embed(['预热']))"

COPY --chown=app:app . .

EXPOSE 8001
HEALTHCHECK --interval=15s --timeout=3s --start-period=40s --retries=5 \
  CMD python -c "import json,os,urllib.request; p=os.getenv('HEALTH_PORT','8001'); assert json.load(urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=2))['status']=='ok'"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8001"]
