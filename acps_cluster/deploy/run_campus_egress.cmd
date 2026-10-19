@echo off
rem 校园网出口启动包装（供手动计划任务调用；日志落同目录 _campus_egress.log）
cd /d %~dp0
"D:\Git\bin\bash.exe" ./start_campus_egress.sh >> _campus_egress.log 2>&1
