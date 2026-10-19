#!/bin/sh
# 选项 A 落地：安装并启动心跳镜像（供平台 amp_forwarder 采集）
set -e
CL=/opt/wit/my-project/acps_cluster

echo "=== 1) 准备镜像目录 ==="
mkdir -p /var/log/acps-amp
chown -R wit:wit /var/log/acps-amp
ls -ld /var/log/acps-amp

echo
echo "=== 2) 安装 systemd 单元 ==="
install -m 644 "$CL/deploy/wit-amp-mirror.service" /etc/systemd/system/wit-amp-mirror.service
systemctl daemon-reload
systemctl enable --now wit-amp-mirror.service
sleep 20
systemctl is-active wit-amp-mirror.service

echo
echo "=== 3) 镜像目录内容（平台 Forwarder 要 tail 的就是这里）==="
ls -la /var/log/acps-amp/yanshu/ | sed 's/^/  /'
echo
echo "  单个文件样例（最后一行）："
tail -n 1 /var/log/acps-amp/yanshu/amp_heartbeat_scout.jsonl | cut -c1-200 | sed 's/^/    /'

echo
echo "=== 4) 单元日志 ==="
journalctl -u wit-amp-mirror.service -n 12 --no-pager | tail -12
