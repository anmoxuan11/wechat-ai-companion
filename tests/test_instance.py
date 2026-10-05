# -*- coding: utf-8 -*-
"""core.instance / core.proclog 轻量测试。

只测不依赖外部环境的部分：端口探测、锁文件读写、自身进程探测。
涉及微信的连接一律不测（需要真微信，自动测试里没意义）。
"""

import os
import socket
import time

from core import instance, proclog


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------- 端口 ----------------

def test_free_port_not_in_use():
    assert instance.port_in_use(_free_port()) is False


def test_listening_port_reported_in_use():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        assert instance.port_in_use(port) is True
    finally:
        s.close()


def test_bind_free_true_on_idle_port():
    assert instance.bind_free(_free_port()) is True


def test_bind_free_false_on_occupied_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        assert instance.bind_free(port) is False
    finally:
        s.close()


def test_resolve_conflict_start_when_idle():
    r = instance.resolve_conflict("C:\\dummy", _free_port())
    assert r["action"] == "start"


def test_resolve_conflict_warn_when_foreign_program():
    """端口被占但不是自家面板 → 必须明确告警，不能静默退出。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        r = instance.resolve_conflict("C:\\dummy", port)
        assert r["action"] == "warn"
        assert "其他程序" in r["msg"]
    finally:
        s.close()


# ---------------- 单实例锁 ----------------

def test_lock_roundtrip(tmp_path):
    d = str(tmp_path)
    assert instance.read_lock(d) is None

    instance.write_lock(d, 8765)
    data = instance.read_lock(d)
    assert data is not None
    assert data["pid"] == os.getpid()
    assert data["port"] == 8765
    assert data["base_dir"] == d

    assert instance.remove_lock(d) is True
    assert instance.read_lock(d) is None


def test_stale_lock_detected_for_dead_pid(tmp_path):
    d = str(tmp_path)
    instance.write_lock(d, 8765)
    # 强行改成一个不存在的 PID，模拟上次崩溃留下的锁
    import json
    p = instance.lock_path(d)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["pid"] = 999999
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)

    stale = instance.stale_lock(d, proclog.alive)
    assert stale is not None


def test_fresh_lock_is_not_stale(tmp_path):
    d = str(tmp_path)
    instance.write_lock(d, 8765)
    assert instance.stale_lock(d, proclog.alive) is None


# ---------------- 进程探测（纯 ctypes，不再调 PowerShell） ----------------

def test_current_process_is_alive():
    assert proclog.alive(os.getpid()) is True


def test_dead_pid_not_alive():
    assert proclog.alive(999999) is False


def test_proc_name_of_self_is_python():
    assert "python" in proclog.proc_name(os.getpid()).lower()


def test_proc_start_time_reasonable():
    t = proclog.proc_start_time(os.getpid())
    assert t is None or (0 < t <= time.time())


def test_proc_mem_positive():
    mb = proclog.proc_mem_mb(os.getpid())
    assert mb is None or mb > 0


def test_tail_text_on_missing_file():
    assert proclog.tail_text("C:\\no\\such\\file.txt") == ""


def test_tail_text_reads_last_lines(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("\n".join("line%d" % i for i in range(50)), encoding="utf-8")
    out = proclog.tail_text(str(p), n=3)
    assert "line49" in out
    assert "line10" not in out


def test_kill_tree_on_missing_pid_is_safe():
    proclog.kill_tree(999999)  # 不应抛异常


def test_rotate_noop_on_small_file(tmp_path):
    p = tmp_path / "small.log"
    p.write_text("x" * 100, encoding="utf-8")
    proclog.rotate_if_needed(str(p))
    assert p.read_text(encoding="utf-8") == "x" * 100
