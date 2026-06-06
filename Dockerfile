FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制 Python 模块（app/ 下的 .py 文件到 /app/ 根目录）
COPY app/__init__.py app/main.py app/data.py app/scheduler.py app/image_gen.py app/web_server.py app/store.py app/updater.py /app/
COPY app/web/ /app/web/
COPY app/zhuzhu/ /app/zhuzhu/
COPY app/references/ /app/references/

# 复制依赖文件并安装
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制配置
COPY config/ ./config/

# 创建数据目录
RUN mkdir -p /app/data/images

# 暴露端口
EXPOSE 18889

# 环境变量
ENV CONFIG_PATH=/app/config/config.yaml
ENV PYTHONPATH=/app
ENV OPENCODE_API_KEY=""
ENV ZHUZHU_PRIMARY_API_KEY=""
ENV ZHUZHU_MEDIA_DIR=/app/data/images

# 运行
CMD ["python3", "-m", "main"]
