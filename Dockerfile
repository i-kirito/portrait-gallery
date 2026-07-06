FROM python:3.11-slim

WORKDIR /app

# 复制依赖文件并安装
COPY app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir -r /app/app/requirements.txt

# 复制应用源码，保留仓库内 app/ 路径，匹配配置里的 app/zhuzhu 和 app/references
COPY app/ /app/app/

# 复制配置
COPY config/ ./config/
COPY VERSION /app/VERSION

# 创建数据目录
RUN mkdir -p /app/data/images

# 暴露端口
EXPOSE 18889

# 环境变量
ENV CONFIG_PATH=/app/config/config.yaml
ENV PYTHONPATH=/app/app
ENV ZHUZHU_MEDIA_DIR=/app/data/images

# 运行
WORKDIR /app/app
CMD ["python3", "-m", "main"]
