# 精灵日程系统 - 应用镜像
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 系统依赖（构建 asyncpg、bcrypt 等需要）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       libpq-dev \
       curl \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖清单，利用层缓存
COPY requirements.txt .
RUN pip install -r requirements.txt

# 拷贝应用代码
COPY . .

EXPOSE 8000

# 默认启动 API（celery 在 docker-compose 里 override command）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
