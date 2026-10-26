"""P0-5 回归测试：投递失败时**不得**推进位点（否则心跳被永久跳过、平台判离线）。

原缺陷（2026-10-11 审查 P0-5）：`producer.send()` 的 Future 被丢弃、`flush()` 返回值不检查，
随后**无条件**推进 `offsets[file.name]`。kafka-python 的记录级错误挂在 Future 上不抛出，
于是 broker 拒收时心跳被永久跳过，进程仍 active、日志零报错。

覆盖：
1. 投递失败 → 该条位点**不推进**（下轮会重试）；
2. 投递成功 → 位点正常推进；
3. 非心跳行（log_type 不符 / 坏 JSON）→ 位点推进（不需要投递）；
4. 失败后恢复 → 之前失败的那条会被补投。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

CL = Path(__file__).resolve().parents[1]          # acps_cluster
SENDER = CL / "deploy" / "amp_heartbeat_sender.py"

spec = importlib.util.spec_from_file_location("hb_sender", SENDER)
mod = importlib.util.module_from_spec(spec)
sys.modules["hb_sender"] = mod
spec.loader.exec_module(mod)


class _Meta:
    """模拟 kafka-python 的 RecordMetadata Future。"""

    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    def get(self, timeout=None):  # noqa: ANN001, ANN202
        if self._fail:
            raise RuntimeError("broker 拒收（模拟）")
        return {"offset": 1, "partition": 0}


class _FakeProducer:
    def __init__(self, fail_first: int = 0) -> None:
        self.sent: list[str] = []
        self.fail_first = fail_first
        self.attempts = 0

    def send(self, topic, payload):  # noqa: ANN001, ANN202
        self.attempts += 1
        fail = self.attempts <= self.fail_first
        if not fail:
            self.sent.append(payload.decode("utf-8"))
        return _Meta(fail=fail)

    def close(self, timeout=None):  # noqa: ANN001, ANN202
        pass


def _write(path: Path, lines: list[dict | str]) -> None:
    out = []
    for ln in lines:
        out.append(ln if isinstance(ln, str) else json.dumps(ln, ensure_ascii=False))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _hb(n: int) -> dict:
    return {"log_type": "heartbeat", "aic": f"aic-{n}", "timestamp": "2026-10-11T00:00:00Z"}


def main() -> int:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="hb-sender-test-"))
    mirror = tmp / "mirror"
    (mirror / "yanshu").mkdir(parents=True)
    f = mirror / "yanshu" / "amp_heartbeat_demo.jsonl"
    offsets_path = mirror / ".sender-offsets.json"

    # 本机没装 kafka-python（它在服务器 venv 里），所以先把 make_producer 换成假的，
    # 测试只验证「位点推进语义」，不需要真 broker。
    orig_make = mod.make_producer

    # ── 场景 1：投递失败 → 位点不推进 ─────────────────────────────
    _write(f, [_hb(1), _hb(2), _hb(3)])
    offsets_path.write_text(json.dumps({f.name: 0}), encoding="utf-8")
    fail_prod = _FakeProducer(fail_first=10**9)          # 永远失败
    mod.make_producer = lambda broker: fail_prod         # type: ignore[assignment]
    try:
        rc = mod.main(["--mirror-dir", str(mirror), "--send", "--once", "--watch",
                       "--offsets", str(offsets_path), "--send-timeout", "1"])
    finally:
        mod.make_producer = orig_make                    # type: ignore[assignment]
    off = json.loads(offsets_path.read_text(encoding="utf-8"))[f.name]
    size = f.stat().st_size
    print(f"  [1] 全部投递失败 → 位点={off}（期望 0，不推进）  文件大小={size}  rc={rc}")
    if off != 0:
        failures.append(f"[1] 失败仍推进位点：{off}")

    # ── 场景 2：投递恢复 → 位点推进到文件末尾 ─────────────────────
    ok_prod = _FakeProducer()
    mod.make_producer = lambda broker: ok_prod           # type: ignore[assignment]
    try:
        rc2 = mod.main(["--mirror-dir", str(mirror), "--send", "--once", "--watch",
                        "--offsets", str(offsets_path), "--send-timeout", "1"])
    finally:
        mod.make_producer = orig_make                    # type: ignore[assignment]
    off2 = json.loads(offsets_path.read_text(encoding="utf-8"))[f.name]
    print(f"  [2] 投递恢复 → 位点={off2}（期望 {size}）  已投 {len(ok_prod.sent)} 条  rc={rc2}")
    if off2 != size:
        failures.append(f"[2] 恢复后位点未追平：{off2} != {size}")
    if len(ok_prod.sent) != 3:
        failures.append(f"[2] 应补投 3 条（含之前失败那条），实际 {len(ok_prod.sent)}")

    # ── 场景 3：非心跳行 → 位点推进、不投递 ───────────────────────
    _write(f, [_hb(9), "这不是 JSON", {"log_type": "system", "aic": "x"}])
    offsets_path.write_text(json.dumps({f.name: 0}), encoding="utf-8")
    p3 = _FakeProducer()
    mod.make_producer = lambda broker: p3                # type: ignore[assignment]
    try:
        mod.main(["--mirror-dir", str(mirror), "--send", "--once", "--watch",
                  "--offsets", str(offsets_path), "--send-timeout", "1"])
    finally:
        mod.make_producer = orig_make                    # type: ignore[assignment]
    off3 = json.loads(offsets_path.read_text(encoding="utf-8"))[f.name]
    print(f"  [3] 混合内容 → 位点={off3}（期望追平 {f.stat().st_size}）  实投 {len(p3.sent)} 条（期望 1）")
    if off3 != f.stat().st_size:
        failures.append(f"[3] 位点未追平：{off3}")
    if len(p3.sent) != 1:
        failures.append(f"[3] 非心跳行被误投：{len(p3.sent)}")

    print()
    if failures:
        for x in failures:
            print("  ✘", x)
        return 1
    print("✔ 通过：失败不推进位点（可重试）、成功追平、非心跳行正常跳过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
