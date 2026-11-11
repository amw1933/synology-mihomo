# 群晖 mihomo 网页管理台
# 管理台需要调用宿主机的 docker（校验配置 / 热重载 / 重启容器），
# 所以用多阶段构建把官方 docker CLI 拷进来（容器里挂 /var/run/docker.sock）。
FROM docker:27-cli AS dockercli

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    SB_BASE=/data \
    ADMIN_PORT=9093

WORKDIR /app
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY app.py sub_convert.py /app/

EXPOSE 9093

CMD ["python", "/app/app.py"]
