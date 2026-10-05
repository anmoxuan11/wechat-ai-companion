# -*- coding: utf-8 -*-
"""原子写入与自动备份（2.0）

解决 1.0 的两个问题：
  1. 非原子写：写一半被杀/断电 → 文件半截损坏
  2. 无备份：人设卡/记忆是不可再生资产，覆盖即永久丢失

做法：
  - 所有写都走「写临时文件 → os.replace() 原子替换」
  - 覆盖前自动转存 .bak，按日期+序号保留最近 N 份
"""

import json
import os
import time

DEFAULT_KEEP = 5


def _bak_name(path, keep=DEFAULT_KEEP):
    """生成备份文件名：<name>.<YYYYMMDD>_<n>.bak，返回未被占用的那个。"""
    base = os.path.basename(path)
    stamp = time.strftime("%Y%m%d")
    for i in range(keep):
        cand = "%s.%s_%d.bak" % (base, stamp, i)
        if not os.path.exists(os.path.join(os.path.dirname(path), cand)):
            return cand
    # 全占满：覆盖最老的那个
    return "%s.%s_%d.bak" % (base, stamp, keep - 1)


def _prune_backups(path, keep=DEFAULT_KEEP):
    """只保留最近 keep 份备份，其余删除。"""
    d = os.path.dirname(path)
    base = os.path.basename(path)
    try:
        baks = sorted(
            (f for f in os.listdir(d) if f.startswith(base) and f.endswith(".bak")),
            key=lambda f: os.path.getmtime(os.path.join(d, f)),
            reverse=True,
        )
        for old in baks[keep:]:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass
    except OSError:
        pass


def atomic_write_bytes(path, data, backup=True, keep=DEFAULT_KEEP):
    """原子写二进制。data 为 bytes。返回 (ok, error)。

    path 可以是 str 或 pathlib.Path（2.0：统一 fspath 化，避免调用方踩坑）。
    """
    path = os.fspath(path)
    tmp = path + ".tmp"
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            return False, e
    try:
        if backup and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    old = f.read()
                bak = os.path.join(d, _bak_name(path, keep))
                tmp = bak + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(old)
                os.replace(tmp, bak)
                _prune_backups(path, keep)
            except OSError:
                pass  # 备份失败不阻塞主写入
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True, None
    except OSError as e:
        # 清理可能残留的临时文件
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except (OSError, UnboundLocalError):
            pass
        return False, e


def atomic_write_text(path, text, encoding="utf-8", backup=True, keep=DEFAULT_KEEP):
    """原子写文本，UTF-8 无 BOM。返回 (ok, error)。"""
    return atomic_write_bytes(path, text.encode(encoding), backup=backup, keep=keep)


def atomic_write_json(path, obj, backup=True, keep=DEFAULT_KEEP, indent=2):
    """原子写 JSON。返回 (ok, error)。"""
    body = json.dumps(obj, ensure_ascii=False, indent=indent)
    return atomic_write_text(path, body, backup=backup, keep=keep)


def read_json(path, default=None):
    """读 JSON，任何异常都返回 default（配置文件损坏不应导致程序崩溃）。"""
    path = os.fspath(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_text(path, default=""):
    """读文本，失败返回 default。"""
    path = os.fspath(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def retry(fn, times=4, delay=0.3, on_fail=None):
    """统一重试包装：取代 1.0 里散落各处的 `for attempt in range(4)`。

    fn 返回真值即成功；全部失败返回 None 并调用 on_fail(最后一次异常)。
    """
    last = None
    for i in range(times):
        try:
            r = fn()
            if r:
                return r
        except Exception as e:  # noqa: BLE001
            last = e
        if i < times - 1:
            time.sleep(delay)
    if on_fail and last is not None:
        on_fail(last)
    return None
