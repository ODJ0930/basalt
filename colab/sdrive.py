#!/usr/bin/env python3
"""小网盘（simple-drive）命令行工具 v2：只用标准库，Mac / 63 / Colab 都能跑。

小网盘接口（见 <服务地址>/api.md）：POST（新增，同名 409）/ PUT（原子覆盖，带 ETag、If-Match）上传、
GET / HEAD 下载和查信息、DELETE 删除、GET /api/files?prefix= 分页列文件、DELETE /api/files?prefix= 批量删除、
POST /api/rename 改名、GET /api/stats 空间。

- 逻辑路径 a/b/c.ext 存成扁平文件名 a__b__c.ext（网盘的文件名只能是一段）；
- 大于 4 GiB 的文件分块存：<扁平名>.p0000、.p0001 …（每块 2 GiB），上传断了从断的那块续传，下载多路并行；
- 列表直接用网盘的列文件接口（v1 的 _index-*.json 索引已不再需要）。

密钥：环境变量 SIMPLE_DRIVE_URL / SIMPLE_DRIVE_KEY，没有就依次读
~/.config/simple-drive/env、/content/simple_drive.env（Colab 笔记本从 Colab Secrets 的 SIMPLE_DRIVE_KEY 写出）
（格式：export SIMPLE_DRIVE_KEY='…'）。密钥不会打印。

用法：
  sdrive.py ls [前缀] [--json]
  sdrive.py put 本地文件 逻辑路径 [--overwrite]
  sdrive.py get 逻辑路径 本地文件
  sdrive.py cat 逻辑路径                       # 小文件内容打到标准输出
  sdrive.py rm 逻辑路径 [--prefix]             # --prefix：删掉这个前缀下的全部文件
  sdrive.py mv 旧逻辑路径 新逻辑路径 [--overwrite]
  sdrive.py sync-up 本地目录 逻辑前缀 [--match 正则] [--min-age 秒] [--watch 秒] [--overwrite]
  sdrive.py sync-down 逻辑前缀 本地目录 [--match 正则]
  sdrive.py stats
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_FILES = ["~/.config/simple-drive/env", "/content/simple_drive.env",
             "/content/drive/MyDrive/comfy_prod/simple_drive.env"]  # 最后一个只为过渡期兼容
CHUNK = 4 << 20
SPLIT = 4 << 30
PART = 2 << 30
PART_RE = re.compile(r"^(.*)\.p(\d{4})$")
SKIP_NAMES = re.compile(r"^_index-\d{6}\.json$")  # v1 留下的索引文件
_cfg: dict[str, str] = {}


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


class DriveError(RuntimeError):
    pass


def config() -> tuple[str, str]:
    if not _cfg:
        url, key = os.environ.get("SIMPLE_DRIVE_URL", ""), os.environ.get("SIMPLE_DRIVE_KEY", "")
        for f in ENV_FILES:
            if url and key:
                break
            p = os.path.expanduser(f)
            if os.path.exists(p):
                for line in open(p, encoding="utf-8"):
                    m = re.match(r"\s*(?:export\s+)?(SIMPLE_DRIVE_URL|SIMPLE_DRIVE_KEY)=['\"]?([^'\"\s]+)", line)
                    if m and m.group(1) == "SIMPLE_DRIVE_URL" and not url:
                        url = m.group(2)
                    elif m and m.group(1) == "SIMPLE_DRIVE_KEY" and not key:
                        key = m.group(2)
        if not key:
            raise DriveError("找不到小网盘密钥（SIMPLE_DRIVE_KEY 或 " + " / ".join(ENV_FILES) + "）")
        _cfg.update(url=(url or "https://ovh.527930.xyz:8443").rstrip("/"), key=key)
    return _cfg["url"], _cfg["key"]


def flat_name(path: str) -> str:
    path = path.strip("/")
    if not path or any(s in ("", ".", "..") for s in path.split("/")):
        raise ValueError(f"路径不对：{path!r}")
    return "__".join(path.split("/"))


def logical(name: str) -> str:
    return "/".join(name.split("__"))


def _url(kind: str, name: str = "", query: dict | None = None) -> str:
    base, _ = config()
    u = f"{base}/{kind}" + (f"/{urllib.parse.quote(name, safe='')}" if name else "")
    return u + ("?" + urllib.parse.urlencode(query) if query else "")


def _req(method: str, url: str, headers: dict | None = None, data=None, timeout: float = 60):
    _, key = config()
    h = {"Authorization": f"Bearer {key}", "User-Agent": "sdrive/2"}
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=timeout)


def _retry(fn, what: str, tries: int = 5, ok_codes: tuple = ()):
    """网络抖动重试；401 / 507 / 业务状态码直接抛。ok_codes 里的 HTTPError 原样抛给调用方处理。"""
    err = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in ok_codes:
                raise
            if e.code == 401:
                raise DriveError("小网盘密钥不对（401）") from None
            if e.code == 507:
                raise DriveError("小网盘硬盘空间不足（507）") from None
            if e.code not in (500, 502, 503, 504):
                raise DriveError(f"{what}：HTTP {e.code} {e.read()[:200]!r}") from None
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            err = str(e)
        if attempt < tries:
            time.sleep(min(60, 3 * attempt))
    raise DriveError(f"{what} 失败：{err}")


# ---------------------------------------------------------------- 底层接口

def head(name: str) -> int | None:
    """远端文件大小；不存在返回 None。"""
    try:
        return _retry(lambda: int(_req("HEAD", _url("files", name), timeout=30).headers.get("Content-Length", "0")),
                      f"HEAD {name}", ok_codes=(404,))
    except urllib.error.HTTPError:
        return None


def stat(name: str) -> dict | None:
    try:
        r = _retry(lambda: _req("HEAD", _url("files", name), timeout=30), f"HEAD {name}", ok_codes=(404,))
        return {"size": int(r.headers.get("Content-Length", "0")), "etag": r.headers.get("ETag")}
    except urllib.error.HTTPError:
        return None


def list_names(prefix: str = "") -> list[dict]:
    """网盘上以 prefix（扁平名前缀）开头的全部文件：[{name, size, mtime, etag}]。"""
    out, after = [], None
    while True:
        q = {"prefix": prefix, "limit": 1000}
        if after:
            q["after"] = after
        d = _retry(lambda: json.load(_req("GET", _url("api/files", query=q), timeout=60)), "列文件")
        out += d["files"]
        after = d.get("next")
        if not after:
            return out


def stats() -> dict:
    return _retry(lambda: json.load(_req("GET", _url("api/stats"), timeout=30)), "查空间")


def delete_name(name: str) -> bool:
    try:
        _retry(lambda: _req("DELETE", _url("api/files", name), timeout=60).close(), f"删除 {name}", ok_codes=(404,))
        return True
    except urllib.error.HTTPError:
        return False


def delete_prefix_names(prefix: str) -> dict:
    if not prefix.strip():
        raise ValueError("批量删除的前缀不能为空")
    return _retry(lambda: json.load(_req("DELETE", _url("api/files", query={"prefix": prefix}), timeout=600)),
                  f"批量删除 {prefix}")


class _Reader:
    """边读边报进度的上传体（只读 size 字节）。"""

    def __init__(self, f, size: int, label: str):
        self.f, self.size, self.label = f, size, label
        self.done, self.t0, self.last = 0, time.time(), 0.0

    def read(self, n: int = -1) -> bytes:
        b = self.f.read(min(CHUNK, self.size - self.done))  # http.client 每次只要 8 KB，这里一次给 4 MB
        self.done += len(b)
        now = time.time()
        if self.size > 256 << 20 and now - self.last > 30:
            self.last = now
            log(f"  {self.label} {self.done / self.size:.0%} {self.done / 2**20 / max(now - self.t0, 1e-3):.0f} MB/s")
        return b


def upload(local: str, name: str, offset: int = 0, length: int | None = None, overwrite: bool = False,
           if_match: str | None = None) -> str | None:
    """上传一个文件（或其中 offset 起的 length 字节），返回新 ETag。
    overwrite=False 用 POST，同名已有抛 FileExistsError；True 用 PUT 原子覆盖（if_match 不符抛 FileExistsError）。"""
    size = os.path.getsize(local) - offset if length is None else length
    err = None
    for attempt in range(1, 6):
        with open(local, "rb") as f:
            f.seek(offset)
            hdr = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
            if if_match:
                hdr["If-Match"] = if_match
            try:
                with _req("PUT" if overwrite else "POST", _url("api/files", name), data=_Reader(f, size, name),
                          timeout=600, headers=hdr) as r:
                    return r.headers.get("ETag")
            except urllib.error.HTTPError as e:
                if e.code in (409, 412):
                    raise FileExistsError(name) from None
                if e.code == 401:
                    raise DriveError("小网盘密钥不对（401）") from None
                if e.code == 507:
                    raise DriveError("小网盘硬盘空间不足（507）") from None
                err = f"HTTP {e.code} {e.read()[:200]!r}"
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
                err = str(e)
        log(f"  上传 {name} 第 {attempt} 次失败：{err}；稍后重试")
        time.sleep(min(60, 10 * attempt))
    raise DriveError(f"上传 {name} 失败：{err}")


def put_bytes(path: str, data: bytes, if_match: str | None = None) -> str | None:
    """把一小段内容原子写到逻辑路径（覆盖），返回新 ETag。if_match 不符时抛 FileExistsError。"""
    name = flat_name(path)
    hdr = {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))}
    if if_match:
        hdr["If-Match"] = if_match
    try:
        r = _retry(lambda: _req("PUT", _url("api/files", name), data=data, headers=hdr, timeout=60),
                   f"写 {path}", ok_codes=(412,))
        return r.headers.get("ETag")
    except urllib.error.HTTPError:
        raise FileExistsError(path) from None


def read_bytes(path: str) -> tuple[bytes, str | None] | None:
    """小文件的内容和 ETag；不存在返回 None。"""
    try:
        r = _retry(lambda: _req("GET", _url("files", flat_name(path)), timeout=60), f"读 {path}", ok_codes=(404,))
        return r.read(), r.headers.get("ETag")
    except urllib.error.HTTPError:
        return None


def upload_split(local: str, name: str) -> int:
    """大文件分块上传；网盘上已有、大小对的块跳过（续传）。返回块数。"""
    size = os.path.getsize(local)
    n = (size + PART - 1) // PART
    t0 = time.time()
    for i in range(n):
        pname, off = f"{name}.p{i:04d}", i * PART
        plen = min(PART, size - off)
        have = head(pname)
        if have == plen:
            continue
        if have is not None:  # 上次没传完的残块（PUT 是原子的，一般不会有）：删掉重传
            delete_name(pname)
        log(f"  块 {i + 1}/{n}（{plen / 2**30:.1f} GB）")
        upload(local, pname, off, plen, overwrite=True)
        log(f"  块 {i + 1}/{n} 完成；累计 {(off + plen) / 2**20 / max(time.time() - t0, 1e-3):.0f} MB/s")
    return n


def download_parts(e: dict, dst: str, workers: int | None = None) -> None:
    """分块文件并行下载到 dst（先写稀疏的 dst.part；完成的块记在 dst.part.done，断了只补没完成的块）。"""
    from concurrent.futures import ThreadPoolExecutor
    size, ps, n = e["size"], e["part_size"], e["parts"]
    workers = workers or int(os.environ.get("SDRIVE_WORKERS", "4"))
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    part, state = dst + ".part", dst + ".part.done"
    done: set[int] = set()
    if os.path.exists(part) and os.path.exists(state):
        done = set(json.load(open(state)))
    else:
        open(part, "wb").close()
    os.truncate(part, size)
    lock = threading.Lock()
    todo = [i for i in range(n) if i not in done]
    prog = {"bytes": 0, "last": time.time()}
    t0 = time.time()

    def fetch(i: int) -> None:
        pname, off = f"{e['name']}.p{i:04d}", i * ps
        plen = min(ps, size - off)
        for attempt in range(1, 9):
            got = 0
            try:
                with _req("GET", _url("files", pname), timeout=120) as r, open(part, "r+b") as f:
                    f.seek(off)
                    while True:
                        b = r.read(CHUNK)
                        if not b:
                            break
                        f.write(b)
                        got += len(b)
                        with lock:
                            prog["bytes"] += len(b)
                            if time.time() - prog["last"] > 30:
                                prog["last"] = time.time()
                                log(f"  {e['path']} {(len(done) * ps + prog['bytes']) / size:.0%}，"
                                    f"{prog['bytes'] / 2**20 / max(time.time() - t0, 1e-3):.0f} MB/s")
                if got != plen:
                    raise DriveError(f"{pname} 只收到 {got}/{plen}")
                with lock:
                    done.add(i)
                    json.dump(sorted(done), open(state, "w"))
                return
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError, DriveError) as ex:
                with lock:
                    prog["bytes"] -= got
                log(f"  {pname} 第 {attempt} 次中断：{ex}；重下这块")
                time.sleep(min(60, 5 * attempt))
        raise DriveError(f"下载 {pname} 失败")

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(fetch, todo))
    if len(done) != n:
        raise DriveError(f"下载 {e['path']} 不完整：{len(done)}/{n} 块")
    os.remove(state)
    os.replace(part, dst)
    log(f"  下载完成 {e['path']} {prog['bytes'] / 2**20 / max(time.time() - t0, 1e-3):.0f} MB/s（{workers} 路并行）")


def download(name: str, dst: str, size: int | None = None) -> None:
    """断点续传下载到 dst（先写 dst.part）。"""
    size = head(name) if size is None else size
    if size is None:
        raise FileNotFoundError(name)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    part = dst + ".part"
    t0, start0 = time.time(), (os.path.getsize(part) if os.path.exists(part) else 0)
    for attempt in range(1, 9):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > size:
            os.remove(part)
            have = 0
        if have == size:
            break
        try:
            hdr = {"Range": f"bytes={have}-"} if have else {}
            with _req("GET", _url("files", name), headers=hdr, timeout=120) as r, \
                    open(part, "ab" if have and r.status == 206 else "wb") as f:
                last = time.time()
                while True:
                    b = r.read(CHUNK)
                    if not b:
                        break
                    f.write(b)
                    if size > 256 << 20 and time.time() - last > 30:
                        last = time.time()
                        log(f"  {name} {f.tell() / size:.0%}")
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            log(f"  下载 {name} 第 {attempt} 次中断：{e}；续传")
            time.sleep(min(60, 5 * attempt))
    if os.path.getsize(part) != size:
        raise DriveError(f"下载 {name} 大小不对：{os.path.getsize(part)} != {size}")
    os.replace(part, dst)
    if size > 64 << 20:
        log(f"  下载完成 {name} {(size - start0) / 2**20 / max(time.time() - t0, 1e-3):.0f} MB/s")


# ---------------------------------------------------------------- 逻辑路径

def catalog(prefix: str = "") -> dict[str, dict]:
    """逻辑路径 -> {path, name, size, mtime, etag[, parts, part_size]}；分块文件合成一条。"""
    pre = flat_name(prefix) if prefix.strip("/") else ""
    files = list_names(pre)
    cat: dict[str, dict] = {}
    groups: dict[str, list] = {}
    for f in files:
        if SKIP_NAMES.match(f["name"]):
            continue
        m = PART_RE.match(f["name"])
        if m:
            groups.setdefault(m.group(1), []).append((int(m.group(2)), f))
            continue
        cat[logical(f["name"])] = {"path": logical(f["name"]), **f}
    for base, parts in groups.items():
        parts.sort()
        if [i for i, _ in parts] != list(range(len(parts))):
            continue  # 还在上传、块不全：当作不存在
        cat[logical(base)] = {"path": logical(base), "name": base, "size": sum(f["size"] for _, f in parts),
                              "mtime": max(f["mtime"] for _, f in parts), "etag": None,
                              "parts": len(parts), "part_size": parts[0][1]["size"]}
    return cat


def _in_prefix(path: str, prefix: str) -> bool:
    prefix = prefix.strip("/")
    return not prefix or path == prefix or path.startswith(prefix + "/")


def put(local: str, path: str, cat: dict | None = None, overwrite: bool = False, **_ignored) -> dict:
    """上传到逻辑路径。已有同大小的跳过；大小不同时 overwrite=False 报错，True 覆盖。"""
    path = path.strip("/")
    name, size = flat_name(path), os.path.getsize(local)
    cat = catalog(path) if cat is None else cat
    old = cat.get(path)
    if old and old["size"] == size and not overwrite:
        return old
    if old and not overwrite:
        raise DriveError(f"{path} 已在网盘上但大小不同（{old['size']} != {size}）；要覆盖加 --overwrite")
    if old and bool(old.get("parts")) != (size > SPLIT):  # 分块 / 不分块形态变了：先删旧的
        remove(path, cat)
    t0 = time.time()
    if size > SPLIT:
        log(f"上传 {path}（{size / 2**30:.1f} GB，分 {(size + PART - 1) // PART} 块）")
        if old and old.get("parts"):
            delete_prefix_names(name + ".p")
        n = upload_split(local, name)
        e = {"path": path, "name": name, "size": size, "mtime": int(time.time()), "etag": None,
             "parts": n, "part_size": PART}
    else:
        log(f"上传 {path}（{size / 2**20:.1f} MB）")
        try:
            etag = upload(local, name, overwrite=overwrite or bool(old))
        except FileExistsError:  # 别的进程刚传了同名文件
            st = stat(name)
            if not st or st["size"] != size:
                raise DriveError(f"{path} 被别的进程同时写入了不同的内容") from None
            etag = st["etag"]
        if size > 64 << 20:
            log(f"  完成，{size / 2**20 / max(time.time() - t0, 1e-3):.0f} MB/s")
        e = {"path": path, "name": name, "size": size, "mtime": int(time.time()), "etag": etag}
    cat[path] = e
    return e


def get(path: str, dst: str, cat: dict | None = None, verify: bool = False) -> None:
    path = path.strip("/")
    cat = catalog(path) if cat is None else cat
    e = cat.get(path)
    if not e:
        raise FileNotFoundError(f"网盘上没有 {path}")
    if os.path.exists(dst) and os.path.getsize(dst) == e["size"]:
        return
    if e.get("parts"):
        download_parts(e, dst)
    else:
        download(e["name"], dst, e["size"])


def remove(path: str, cat: dict | None = None) -> bool:
    path = path.strip("/")
    cat = catalog(path) if cat is None else cat
    e = cat.pop(path, None)
    if not e:
        return False
    if e.get("parts"):
        delete_prefix_names(e["name"] + ".p")
    else:
        delete_name(e["name"])
    return True


def remove_prefix(prefix: str) -> dict:
    prefix = prefix.strip("/")
    if not prefix:
        raise ValueError("前缀不能为空")
    return delete_prefix_names(flat_name(prefix) + "__")


def rename(src: str, dst: str, overwrite: bool = False) -> None:
    body = json.dumps({"from": flat_name(src), "to": flat_name(dst), "overwrite": overwrite}).encode()
    try:
        _retry(lambda: _req("POST", _url("api/rename"), data=body, timeout=60,
                            headers={"Content-Type": "application/json"}).close(), f"改名 {src}", ok_codes=(409,))
    except urllib.error.HTTPError:
        raise FileExistsError(dst) from None


def sync_up(src: str, prefix: str, match: str | None = None, min_age: float = 0, overwrite: bool = False) -> int:
    rx = re.compile(match) if match else None
    cat, n = catalog(prefix), 0
    for base, dirs, files in os.walk(src):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(base, f)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            if f.startswith(".") or f.endswith((".part", ".tmp", ".aria2")) or (rx and not rx.search(rel)):
                continue
            if min_age and time.time() - os.path.getmtime(p) < min_age:
                continue
            path = f"{prefix.strip('/')}/{rel}" if prefix.strip("/") else rel
            old = cat.get(path)
            if old and old["size"] == os.path.getsize(p) and not (overwrite and os.path.getmtime(p) > old["mtime"]):
                continue
            try:
                put(p, path, cat, overwrite=overwrite)
                n += 1
            except DriveError as e:
                log(f"跳过：{e}")
    return n


def sync_down(prefix: str, dst: str, match: str | None = None) -> int:
    rx = re.compile(match) if match else None
    prefix = prefix.strip("/")
    n = 0
    for path, e in sorted(catalog(prefix).items()):
        if not _in_prefix(path, prefix):
            continue
        rel = path[len(prefix):].lstrip("/") if prefix else path
        if rx and not rx.search(rel):
            continue
        out = os.path.join(dst, rel or os.path.basename(path))
        if os.path.exists(out) and os.path.getsize(out) == e["size"]:
            continue
        log(f"下载 {path}（{e['size'] / 2**20:.1f} MB）")
        get(path, out, {path: e})
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="小网盘工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ls"); s.add_argument("prefix", nargs="?", default=""); s.add_argument("--json", action="store_true")
    s = sub.add_parser("put"); s.add_argument("local"); s.add_argument("path"); s.add_argument("--overwrite", action="store_true")
    s = sub.add_parser("get"); s.add_argument("path"); s.add_argument("local")
    s = sub.add_parser("cat"); s.add_argument("path")
    s = sub.add_parser("rm"); s.add_argument("path"); s.add_argument("--prefix", action="store_true")
    s = sub.add_parser("mv"); s.add_argument("src"); s.add_argument("dst"); s.add_argument("--overwrite", action="store_true")
    s = sub.add_parser("sync-up"); s.add_argument("src"); s.add_argument("prefix")
    s.add_argument("--match"); s.add_argument("--min-age", type=float, default=0); s.add_argument("--watch", type=float, default=0)
    s.add_argument("--overwrite", action="store_true")
    s = sub.add_parser("sync-down"); s.add_argument("prefix"); s.add_argument("dst"); s.add_argument("--match")
    sub.add_parser("stats")
    a = ap.parse_args()
    try:
        if a.cmd == "ls":
            rows = [e for p, e in sorted(catalog(a.prefix).items()) if _in_prefix(p, a.prefix)]
            if a.json:
                print(json.dumps(rows, ensure_ascii=False, indent=1))
            else:
                for e in rows:
                    print(f"{e['size']:>16,}  {time.strftime('%m-%d %H:%M', time.localtime(e['mtime']))}  {e['path']}")
                print(f"共 {len(rows)} 个，{sum(e['size'] for e in rows) / 2**30:.2f} GB", file=sys.stderr)
        elif a.cmd == "put":
            print(json.dumps(put(a.local, a.path, overwrite=a.overwrite), ensure_ascii=False))
        elif a.cmd == "get":
            get(a.path, a.local)
        elif a.cmd == "cat":
            r = read_bytes(a.path)
            if r is None:
                sys.exit(f"网盘上没有 {a.path}")
            sys.stdout.buffer.write(r[0])
        elif a.cmd == "rm":
            if a.prefix:
                print(json.dumps(remove_prefix(a.path)))
            elif not remove(a.path):
                sys.exit(f"网盘上没有 {a.path}")
        elif a.cmd == "mv":
            rename(a.src, a.dst, a.overwrite)
        elif a.cmd == "stats":
            s = stats()
            print(f"总 {s['total'] / 2**30:.0f} GB，已用 {s['used'] / 2**30:.0f} GB，剩 {s['free'] / 2**30:.0f} GB")
        elif a.cmd == "sync-up":
            while True:
                n = sync_up(a.src, a.prefix, a.match, a.min_age, a.overwrite)
                if n or not a.watch:
                    log(f"sync-up {a.src} → {a.prefix}：上传 {n} 个")
                if not a.watch:
                    break
                time.sleep(a.watch)
        elif a.cmd == "sync-down":
            log(f"sync-down {a.prefix} → {a.dst}：下载 {sync_down(a.prefix, a.dst, a.match)} 个")
    except (DriveError, ValueError, FileNotFoundError, FileExistsError) as e:
        sys.exit(f"失败：{e}")


if __name__ == "__main__":
    main()
