"""数据集查找与少量取样（供 scout 的 `dataset.fetch` 使用）。

为什么返回**内联 CSV 文本**而不是 URL：下游 GPU 沙盒是 `--network none`（断网），
里面的实验代码无法自己下载；数据集必须以文本形式沿 AIP 的 dataItems 传到沙盒输入目录。
所以这里只取"少量"（默认 100 行）——足够跑通一个演示实验，又不至于把上下文撑爆。

数据源用 OpenML（无需鉴权；HF datasets-server 在本网络不可达，实测超时）。
注意 OpenML 的 WAF 对无 User-Agent 的请求回 412，必须带 UA。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

_OPENML_API = "https://www.openml.org/api/v1/json"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; yanshu-scout/1.0)"}
_TIMEOUT = 30.0
# 只取"少量样本"却整包下载会出事：mnist_784 的 ARFF 约 50MB，实测对端直接断连
# （RemoteProtocolError: peer closed connection）。改为**流式读取 + 字节上限**：
# 解析只需要 @data 之后的头几百行，攒够 _PARSE_BYTES 就够了，剩下的不再读。
_PARSE_BYTES = 2 * 1024 * 1024      # 解压后累计到这个量就停
_MAX_READ_BYTES = 8 * 1024 * 1024   # 网络侧保护上限（压缩流的读取量）
_MAX_CANDIDATES = 3                 # 同名多命中时逐个试，避免第一个恰好拿不到


@dataclass
class DatasetSlice:
    source: str          # "openml"
    dataset_id: int
    name: str
    url: str
    columns: list[str] = field(default_factory=list)
    rows: int = 0
    csv: str = ""
    note: str = ""


async def _search(query: str, limit: int = 5) -> list[dict]:
    """OpenML 的 data_name 是精确匹配；先按原样、再按小写试一次。"""
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        for name in (query.strip(), query.strip().lower()):
            if not name:
                continue
            resp = await client.get(f"{_OPENML_API}/data/list/data_name/{name}/limit/{limit}")
            if resp.status_code != 200:
                continue
            found = (resp.json().get("data") or {}).get("dataset") or []
            if found:
                return found
    return []


def _parse_arff(text: str, max_rows: int) -> tuple[list[str], str]:
    """从 ARFF 里取属性名与前 max_rows 行，拼成 CSV（OpenML 的稠密 ARFF 数据段本就是逗号分隔）。"""
    columns: list[str] = []
    data_lines: list[str] = []
    in_data = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("%"):
            continue
        low = line.lower()
        if not in_data:
            if low.startswith("@attribute"):
                parts = line.split(None, 2)
                if len(parts) >= 2:
                    columns.append(parts[1].strip("'\""))
            elif low.startswith("@data"):
                in_data = True
            continue
        data_lines.append(line)
        if len(data_lines) >= max_rows:
            break
    header = ",".join(columns) if columns else ""
    body = "\n".join(data_lines)
    return columns, (f"{header}\n{body}" if header else body)


async def _download_head(client: httpx.AsyncClient, url: str) -> str:
    """流式取回数据集头部（够解析即可），支持 .arff.gz 且不会把整包读进内存。"""
    import zlib

    out = bytearray()
    raw_bytes = 0
    decompressor = None
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            return ""
        async for chunk in response.aiter_bytes():
            raw_bytes += len(chunk)
            if decompressor is None and not out and chunk[:2] == b"\x1f\x8b":
                decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip
            try:
                out += decompressor.decompress(chunk) if decompressor is not None else chunk
            except zlib.error:
                break
            if len(out) >= _PARSE_BYTES or raw_bytes >= _MAX_READ_BYTES:
                break
    return out.decode("utf-8", "replace")


async def fetch_dataset(query: str, *, max_rows: int = 100) -> DatasetSlice | None:
    """按关键词找数据集并取回前 max_rows 行；找不到返回 None。"""
    candidates = await _search(query)
    if not candidates:
        return None

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        for chosen in candidates[:_MAX_CANDIDATES]:
            detail = await client.get(f"{_OPENML_API}/data/{chosen.get('did')}")
            if detail.status_code != 200:
                continue
            desc = detail.json().get("data_set_description") or {}
            url = str(desc.get("url") or "")
            if not url:
                file_id = desc.get("file_id")
                if not file_id:
                    continue
                url = f"https://www.openml.org/data/v1/download/{file_id}"

            try:
                text = await _download_head(client, url)
            except (httpx.HTTPError, OSError):
                continue  # 换下一个候选（大文件/网络抖动都会落到这里）
            if not text or "@data" not in text.lower():
                continue

            columns, csv_text = _parse_arff(text, max_rows)
            if not csv_text:
                continue
            rows = max(0, len(csv_text.splitlines()) - 1)
            return DatasetSlice(
                source="openml",
                dataset_id=int(chosen.get("did")),
                name=str(desc.get("name") or chosen.get("name") or ""),
                url=url,
                columns=columns,
                rows=rows,
                csv=csv_text,
                note=(
                    f"OpenML 数据集 #{chosen.get('did')} 的前 {rows} 行（完整数据见 url，"
                    "为避免拖垮接口只流式读取了头部）。"
                    "沙盒断网，请把 csv 原文作为输入文件交给实验代码使用。"
                ),
            )
    return None
