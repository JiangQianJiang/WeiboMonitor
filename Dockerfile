FROM python:3.12-slim

WORKDIR /app

# 先装依赖（这层会缓存，只有 requirements.txt 变了才重建）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 代码通过 volume 挂载，不 COPY，方便频繁改动
# 只需要 docker compose restart，不用 rebuild

CMD ["python", "-m", "core.main"]
