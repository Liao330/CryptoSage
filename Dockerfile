# CryptoSage — Multi-Agent Crypto Analysis Docker Image
# 构建: docker build -t cryptosage .
# 运行: docker run -p 8000:8000 --env-file .env cryptosage

FROM node:20-alpine AS frontend

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS backend

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（先复制 requirements 利用缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 源码
COPY backend/ backend/
COPY .env.example .env.example
COPY --from=frontend /frontend/dist frontend/dist/

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
