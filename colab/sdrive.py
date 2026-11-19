#!/usr/bin/env python3
"""小网盘（simple-drive）命令行工具：只用标准库，Mac / 63 / Colab 都能跑。

小网盘只有三个接口：POST /api/files/{名} 上传（同名 409，不能覆盖）、GET /files/{名} 下载
（支持 Range）、HEAD /files/{名} 查大小。没有目录、没有列表、不能删除，所以这里：

- 逻辑路径 a/b/c.ext 存成扁平文件名 a__b__c.ext；
- 另存一串只追加的索引文件 _index-000001.json、_index-000002.json …，每次上传后追加一份，
  记下逻辑路径、文件名、大小、sha256。读索引时从本地缓存往后 HEAD 探测到第一个 404；
  索引文件一旦写入就不会变，所以本地缓存永久有效；
- 同一路径再次上传：大小相同视为已有，跳过；大小不同直接报错（网盘不能覆盖，换个文件名）。

密钥：环境变量 SIMPLE_DRIVE_URL / SIMPLE_DRIVE_KEY，没有就依次读
~/.config/simple-drive/env、/content/simple_drive.env、/content/drive/MyDrive/comfy_prod/simple_drive.env
（格式：export SIMPLE_DRIVE_KEY='…'）。密钥不会打印。

用法：
  sdrive.py ls [前缀] [--json]
  sdrive.py put 本地文件 逻辑路径
  sdrive.py get 逻辑路径 本地文件 [--verify]
  sdrive.py sync-up 本地目录 逻辑前缀 [--match 正则] [--min-age 秒] [--watch 秒]
  sdrive.py sync-down 逻辑前缀 本地目录 [--match 正则] [--verify]
  sdrive.py head 逻辑路径
"""
from __future__ import annotations

import argparse
import hashlib
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
             "/content/drive/MyDrive/comfy_prod/simple_drive.env"]
INDEX_FMT = "_index-{:06d}.json"
CHUNK = 4 << 20
SPLIT = 4 << 30   # 大于 4 GiB 的文件分块存：每块 2 GiB，名字 <扁平名>.p0000 …，断了从断的那块续传
PART = 2 << 30
_cfg: dict[str, str] = {}


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


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
            sys.exit("找不到小网盘密钥（SIMPLE_DRIVE_KEY 或 " + " / ".join(ENV_FILES) + "）")
        _cfg.update(url=(url or "https://ovh.527930.xyz:8443").rstrip("/"), key=key)
    return _cfg["url"], _cfg["key"]


def flat_name(path: str) -> str:
    path = path.strip("/")
    if not path or any(s in ("", ".", "..") for s in path.split("/")):
        raise ValueError(f"路径不对：{path!r}")
    return "__".join(path.split("/"))


def _url(kind: str, name: str) -> str:
    base, _ = config()
    return f"{base}/{kind}/{urllib.parse.quote(name, safe='')}"


def _req(method: str, url: str, headers: dict | None = None, data=None, timeout: float = 60):
    _, key = config()
    h = {"Authorization": f"Bearer {key}", "User-Agent": "sdrive/1"}
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=timeout)


def head(name: str) -> int | None:
    """远端文件大小；不存在返回 None。"""
    for attempt in range(5):
        try:
            with _req("HEAD", _url("files", name), timeout=30) as r:
                return int(r.headers.get("Content-Length", "0"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 401:
                sys.exit("小网盘密钥不对（401）")
            err = e
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"HEAD {name} 失败：{err}")


class _Reader:
    """边读边算 sha256、边报进度的上传体（只读 size 字节）。whole 给了就同时喂整文件的 sha256。"""

    def __init__(self, f, size: int, label: str, whole=None):
        self.f, self.size, self.label, self.whole = f, size, label, whole
        self.h, self.done, self.t0, self.last = hashlib.sha256(), 0, time.time(), 0.0

    def read(self, n: int = -1) -> bytes:
        b = self.f.read(min(CHUNK, self.size - self.done))  # http.client 每次只要 8 KB，这里一次给 4 MB
        self.h.update(b)
        if self.whole is not None:
            self.whole.update(b)
        self.done += len(b)
        now = time.time()
        if self.size > 256 << 20 and now - self.last > 30:
            self.last = now
            log(f"  {self.label} {self.done / self.size:.0%} {self.done / 2**20 / max(now - self.t0, 1e-3):.0f} MB/s")
        return b


def upload(local: str, name: str, offset: int = 0, length: int | None = None, whole=None) -> str:
    """上传一个文件（或其中 offset 起的 length 字节），返回这段的 sha256。同名已有时抛 FileExistsError。
    whole 是整文件的 hashlib 对象：成功才把这段算进去。"""
    size = os.path.getsize(local) - offset if length is None else length
    for attempt in range(1, 6):
        with open(local, "rb") as f:
            f.seek(offset)
            w = whole.copy() if whole is not None else None
            rd = _Reader(f, size, name, w)
            try:
                with _req("POST", _url("api/files", name), data=rd, timeout=600,
                          headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)}) as r:
                    if r.status not in (200, 201):
                        raise RuntimeError(f"上传 {name}：HTTP {r.status}")
                if whole is not None:
                    _hash_state[id(whole)] = w
                return rd.h.hexdigest()
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    raise FileExistsError(name) from None
                if e.code == 401:
                    sys.exit("小网盘密钥不对（401）")
                if e.code == 507:
                    raise RuntimeError("小网盘硬盘空间不足（507）") from None
                err = f"HTTP {e.code} {e.read()[:200]!r}"
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
                err = str(e)
        log(f"  上传 {name} 第 {attempt} 次失败：{err}；稍后重试")
        time.sleep(min(60, 10 * attempt))
    raise RuntimeError(f"上传 {name} 失败")


_hash_state: dict = {}


def upload_split(local: str, name: str) -> tuple[str, int]:
    """大文件分块上传；已经在网盘上的块（大小对）跳过，只在本地读一遍算校验和。返回 (整文件 sha256, 块数)。"""
    size = os.path.getsize(local)
    n = (size + PART - 1) // PART
    whole = hashlib.sha256()
    t0 = time.time()
    for i in range(n):
        pname, off = f"{name}.p{i:04d}", i * PART
        plen = min(PART, size - off)
        have = head(pname)
        if have == plen:
            with open(local, "rb") as f:
                f.seek(off)
                left = plen
                while left:
                    b = f.read(min(CHUNK, left))
                    whole.update(b)
                    left -= len(b)
            continue
        if have is not None:
            raise RuntimeError(f"{pname} 已在网盘上但大小不对（{have} != {plen}）；网盘不能覆盖")
        log(f"  块 {i + 1}/{n}（{plen / 2**30:.1f} GB）")
        try:
            upload(local, pname, off, plen, whole)
            whole = _hash_state.pop(id(whole))
        except FileExistsError:  # 别的进程刚传了这块
            raise RuntimeError(f"{pname} 被别的进程同时上传，请重跑") from None
        log(f"  块 {i + 1}/{n} 完成；累计 {(off + plen) / 2**20 / max(time.time() - t0, 1e-3):.0f} MB/s")
    return whole.hexdigest(), n


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
                    raise RuntimeError(f"{pname} 只收到 {got}/{plen}")
                with lock:
                    done.add(i)
                    json.dump(sorted(done), open(state, "w"))
                return
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError, RuntimeError) as ex:
                with lock:
                    prog["bytes"] -= got
                log(f"  {pname} 第 {attempt} 次中断：{ex}；重下这块")
                time.sleep(min(60, 5 * attempt))
        raise RuntimeError(f"下载 {pname} 失败")

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(fetch, todo))
    if len(done) != n:
        raise RuntimeError(f"下载 {e['path']} 不完整：{len(done)}/{n} 块")
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
            with _req("GET", _url("files", name), headers=hdr, timeout=120) as r, open(part, "ab" if have and r.status == 206 else "wb") as f:
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
        raise RuntimeError(f"下载 {name} 大小不对：{os.path.getsize(part)} != {size}")
    os.replace(part, dst)
    dt = max(time.time() - t0, 1e-3)
    if size > 64 << 20:
        log(f"  下载完成 {name} {(size - start0) / 2**20 / dt:.0f} MB/s")


# ---------------------------------------------------------------- 索引

def _cache_dir() -> str:
    for d in (os.environ.get("SDRIVE_CACHE"), "/content/.sdrive_cache" if os.path.isdir("/content") else None,
              os.path.expanduser("~/.cache/sdrive")):
        if d:
            os.makedirs(d, exist_ok=True)
            return d
    raise RuntimeError("no cache dir")


_index_lock = threading.Lock()


def index_files() -> list[dict]:
    """全部索引文件的内容（按序号），新的从网盘拉、旧的读本地缓存。"""
    cd = _cache_dir()
    out, n = [], 1
    while True:
        name = INDEX_FMT.format(n)
        p = os.path.join(cd, name)
        if not os.path.exists(p):
            if head(name) is None:
                break
            download(name, p)
        out.append(json.load(open(p, encoding="utf-8")))
        n += 1
    return out


def catalog() -> dict[str, dict]:
    """逻辑路径 -> 最新一条记录。"""
    cat: dict[str, dict] = {}
    for doc in index_files():
        for e in doc.get("entries", []):
            cat[e["path"]] = e
    return cat


def append_index(entries: list[dict]) -> None:
    if not entries:
        return
    with _index_lock:
        n = len(index_files()) + 1
        body = json.dumps({"v": 1, "host": socket.gethostname(), "time": int(time.time()), "entries": entries},
                          ensure_ascii=False, indent=1).encode()
        cd = _cache_dir()
        while True:
            name = INDEX_FMT.format(n)
            tmp = os.path.join(cd, name + ".new")
            open(tmp, "wb").write(body)
            try:
                upload(tmp, name)
                os.replace(tmp, os.path.join(cd, name))
                return
            except FileExistsError:  # 别的机器刚写了这个序号：读进来，换下一个
                os.remove(tmp)
                if not os.path.exists(os.path.join(cd, name)):
                    download(name, os.path.join(cd, name))
                n += 1


def put(local: str, path: str, cat: dict | None = None, defer: list | None = None) -> dict:
    """上传并登记索引；已经在网盘上（同大小）就只补登记。defer 给了就先攒着，最后一起写索引。"""
    path = path.strip("/")
    name, size = flat_name(path), os.path.getsize(local)
    cat = catalog() if cat is None else cat
    old = cat.get(path)
    if old and old["size"] == size:
        return old
    if old:
        raise RuntimeError(f"{path} 已在网盘上但大小不同（{old['size']} != {size}）；网盘不能覆盖，请换个文件名")
    if size > SPLIT:
        log(f"上传 {path}（{size / 2**30:.1f} GB，分 {(size + PART - 1) // PART} 块）")
        sha, n = upload_split(local, name)
        e = {"path": path, "name": name, "size": size, "sha256": sha, "parts": n, "part_size": PART,
             "time": int(time.time()), "mtime": int(os.path.getmtime(local)), "host": socket.gethostname()}
        cat[path] = e
        (defer.append(e) if defer is not None else append_index([e]))
        return e
    remote = head(name)
    t0 = time.time()
    if remote is None:
        log(f"上传 {path}（{size / 2**20:.1f} MB）")
        try:
            sha = upload(local, name)
            log(f"  完成，{size / 2**20 / max(time.time() - t0, 1e-3):.0f} MB/s")
        except FileExistsError:
            remote = head(name)
            sha = None
    else:
        sha = None
    if remote is not None and remote != size:
        raise RuntimeError(f"{path} 网盘上已有同名文件但大小不同（{remote} != {size}）")
    e = {"path": path, "name": name, "size": size, "sha256": sha, "time": int(time.time()),
         "mtime": int(os.path.getmtime(local)), "host": socket.gethostname()}
    cat[path] = e
    if defer is not None:
        defer.append(e)
    else:
        append_index([e])
    return e


def get(path: str, dst: str, cat: dict | None = None, verify: bool = False) -> None:
    path = path.strip("/")
    cat = catalog() if cat is None else cat
    e = cat.get(path)
    if not e:
        raise FileNotFoundError(f"索引里没有 {path}")
    if os.path.exists(dst) and os.path.getsize(dst) == e["size"] and not verify:
        return
    if e.get("parts"):
        download_parts(e, dst)
    else:
        download(e["name"], dst, e["size"])
    if verify and e.get("sha256"):
        h = hashlib.sha256()
        with open(dst, "rb") as f:
            for b in iter(lambda: f.read(CHUNK), b""):
                h.update(b)
        if h.hexdigest() != e["sha256"]:
            os.rename(dst, dst + ".bad")
            raise RuntimeError(f"{path} 校验和不对（已改名 .bad）")


def sync_up(src: str, prefix: str, match: str | None = None, min_age: float = 0) -> int:
    rx = re.compile(match) if match else None
    cat, batch, n = catalog(), [], 0
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
            if old and old["size"] == os.path.getsize(p):
                continue
            try:
                put(p, path, cat, defer=batch)
                n += 1
            except RuntimeError as e:
                log(f"跳过：{e}")
            if len(batch) >= 20:
                append_index(batch)
                batch = []
    append_index(batch)
    return n


def sync_down(prefix: str, dst: str, match: str | None = None, verify: bool = False) -> int:
    rx = re.compile(match) if match else None
    prefix = prefix.strip("/")
    n = 0
    cat = catalog()
    for path, e in sorted(cat.items()):
        if prefix and not (path == prefix or path.startswith(prefix + "/")):
            continue
        rel = path[len(prefix):].lstrip("/") if prefix else path
        if rx and not rx.search(rel):
            continue
        out = os.path.join(dst, rel or os.path.basename(path))
        if os.path.exists(out) and os.path.getsize(out) == e["size"] and not verify:
            continue
        log(f"下载 {path}（{e['size'] / 2**20:.1f} MB）")
        get(path, out, cat, verify)
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="小网盘工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ls"); s.add_argument("prefix", nargs="?", default=""); s.add_argument("--json", action="store_true")
    s = sub.add_parser("head"); s.add_argument("path")
    s = sub.add_parser("put"); s.add_argument("local"); s.add_argument("path")
    s = sub.add_parser("get"); s.add_argument("path"); s.add_argument("local"); s.add_argument("--verify", action="store_true")
    s = sub.add_parser("sync-up"); s.add_argument("src"); s.add_argument("prefix")
    s.add_argument("--match"); s.add_argument("--min-age", type=float, default=0); s.add_argument("--watch", type=float, default=0)
    s = sub.add_parser("sync-down"); s.add_argument("prefix"); s.add_argument("dst")
    s.add_argument("--match"); s.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    if a.cmd == "ls":
        pre = a.prefix.strip("/")
        rows = [e for p, e in sorted(catalog().items()) if not pre or p == pre or p.startswith(pre + "/")]
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for e in rows:
                print(f"{e['size']:>14,}  {time.strftime('%m-%d %H:%M', time.localtime(e['time']))}  {e['path']}")
            print(f"共 {len(rows)} 个，{sum(e['size'] for e in rows) / 2**30:.2f} GB", file=sys.stderr)
    elif a.cmd == "head":
        e = catalog().get(a.path.strip("/"))
        size = head(flat_name(a.path))
        print(json.dumps({"index": e, "remote_size": size}, ensure_ascii=False))
    elif a.cmd == "put":
        print(json.dumps(put(a.local, a.path), ensure_ascii=False))
    elif a.cmd == "get":
        get(a.path, a.local, verify=a.verify)
    elif a.cmd == "sync-up":
        while True:
            n = sync_up(a.src, a.prefix, a.match, a.min_age)
            if n or not a.watch:
                log(f"sync-up {a.src} → {a.prefix}：新上传 {n} 个")
            if not a.watch:
                break
            time.sleep(a.watch)
    elif a.cmd == "sync-down":
        log(f"sync-down {a.prefix} → {a.dst}：新下载 {sync_down(a.prefix, a.dst, a.match, a.verify)} 个")


if __name__ == "__main__":
    main()
