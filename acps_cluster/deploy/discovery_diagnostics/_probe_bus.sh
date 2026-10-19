#!/bin/sh
# 探测平台 AMP 总线（Redpanda/Kafka）与本机转发器状态（只读）
echo "=== 本机是否有 fluent-bit / vector 在跑 ==="
(command -v fluent-bit && echo "fluent-bit: 已安装") || echo "fluent-bit: 未安装"
ps -ef | grep -iE 'fluent|vector|filebeat|promtail' | grep -v grep | head
systemctl list-units --all --no-pager 2>/dev/null | grep -iE 'fluent|forward|amp' | head

echo
echo "=== 平台 Kafka/Redpanda 端口可达性（wt.ioa.pub = 192.0.2.20）==="
for port in 9092 19092 29092 5671; do
  timeout 6 sh -c "echo > /dev/tcp/192.0.2.20/$port" 2>/dev/null \
    && echo "  192.0.2.20:$port  可达" \
    || echo "  192.0.2.20:$port  不可达/超时"
done

echo
echo "=== 本机 AMP 日志目录与文件 ==="
ls -la /opt/wit/my-project/acps_cluster/ws-*/logs/ 2>/dev/null | head -40

echo
echo "=== 所有 agent 的心跳文件是否在增长（最近 2 分钟是否更新）==="
find /opt/wit/my-project/acps_cluster -name 'amp_heartbeat_*.jsonl' -newermt '-2 minutes' 2>/dev/null | sed 's|.*/ws-|ws-|' | head -20

echo
echo "=== 是否配置过 AMP_LOG_DIR ==="
grep -rn 'AMP_LOG_DIR' /opt/wit/my-project/acps_cluster/deploy/ /etc/systemd/system/wit-yanshu@.service 2>/dev/null | head
