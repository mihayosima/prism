# syntax=docker/dockerfile:1
#
# prism —— 单端口 agent harness 的生产镜像。
#
# 为什么是多阶段：前端（Vue3 + Vite）需要 Node 工具链，运行时只需要 Python。
# 把 Node 留在构建阶段，运行时镜像不带 npm/node_modules。
#
# 用法：
#   docker build -t prism:latest .
#   docker run --rm -p 8720:8720 \
#     -v "$PWD/data:/app/data" \
#     -e DEEPSEEK_API_KEY=... \
#     prism:latest
#
# 生产加固（deny-by-default + 强制 token）——覆盖 CMD 追加参数即可：
#   docker run ... prism:latest \
#     python -m agent.run.chat --host 0.0.0.0 --port 8720 --hardened --token <TOKEN>

# =============================================================================
# 阶段 1 · 构建前端产物（web/dist）
# =============================================================================
FROM node:22-alpine AS web

WORKDIR /build/web

# 先只拷贝清单与锁文件 ⇒ 依赖层单独缓存，改业务源码不会触发 npm ci 重跑
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
# 构建脚本自带类型检查（vue-tsc --noEmit && vite build）⇒ 类型错误会让镜像构建失败
RUN npm run build


# =============================================================================
# 阶段 2 · 运行时
# =============================================================================
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 只安装**第三方依赖**（从 pyproject.toml 读取，保持单一真相源）。
# 刻意不 `pip install .`：那会把 agent 包装进 site-packages，使 run/chat.py 的
# __file__ 落到 site-packages 下，而它用 Path(__file__).parents[2] / "web" / "dist"
# 定位前端产物 ⇒ 会找不到 dist。保持源码布局（从 /app 以 cwd 方式运行）才正确。
COPY pyproject.toml ./
RUN python -c "import tomllib, subprocess, pathlib; \
    d = tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8')); \
    subprocess.check_call(['pip', 'install', *d['project']['dependencies']])"

# 源码 + 前端产物：两者必须同在 /app 下以维持 __file__ → web/dist 的相对定位
COPY agent/ ./agent/
COPY --from=web /build/web/dist ./web/dist

# 数据目录：SQLite 库、插件期望态、配置覆盖都落在这里 —— 必须挂卷持久化，
# 不写进镜像层（.dockerignore 已排除本机 data/，避免把本地状态烤进镜像）。
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 非 root 运行
RUN useradd --create-home --uid 10001 prism \
    && chown -R prism:prism /app
USER prism

EXPOSE 8720

# 健康探针端点是服务自带的（run/chat.py 的 /health），用标准库 urllib 探测，
# 免装 curl 之类的额外依赖
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8720/health', timeout=4).status == 200 else 1)"

# 默认监听 0.0.0.0（容器内必须，否则宿主机映射不进来）
CMD ["python", "-m", "agent.run.chat", "--host", "0.0.0.0", "--port", "8720"]
