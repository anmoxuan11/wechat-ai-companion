# -*- coding: utf-8 -*-
"""core.atomicio 测试：原子写 + 备份轮转。

1.0 的痛：写一半崩了文件就坏了，且没有备份。2.0 必须做到
「要么写完，要么原文件不动」，并保留最近 N 个备份。
"""

import json
import os

import pytest

from core import atomicio


@pytest.fixture
def tmpfile(tmp_path):
    return tmp_path / "data.json"


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def test_atomic_write_json_roundtrip(tmpfile):
    ok, err = atomicio.atomic_write_json(tmpfile, {"a": 1, "b": "中文"})
    assert ok, err
    assert _read(tmpfile) == {"a": 1, "b": "中文"}


def test_write_creates_backup_and_prunes(tmpfile):
    """连续写 8 次，最多保留 DEFAULT_KEEP 个 .bak。"""
    keep = atomicio.DEFAULT_KEEP
    for i in range(8):
        ok, err = atomicio.atomic_write_json(tmpfile, {"i": i})
        assert ok, err
    baks = sorted(tmpfile.parent.glob(tmpfile.name + ".*.bak"))
    assert len(baks) > 0
    assert len(baks) <= keep


def test_backup_content_is_previous_version(tmpfile):
    atomicio.atomic_write_json(tmpfile, {"v": 1})
    atomicio.atomic_write_json(tmpfile, {"v": 2})
    assert _read(tmpfile)["v"] == 2
    baks = sorted(tmpfile.parent.glob(tmpfile.name + ".*.bak"))
    assert baks, "应至少产生一个备份"
    # 最新备份里应该是上一个版本
    assert _read(baks[-1])["v"] == 1


def test_atomic_write_text_encoding(tmp_path):
    p = tmp_path / "t.txt"
    ok, err = atomicio.atomic_write_text(p, "你好，世界\nabc")
    assert ok, err
    with open(p, "r", encoding="utf-8") as f:
        assert f.read() == "你好，世界\nabc"


def test_no_temp_file_left_behind(tmpfile):
    """原子写完成后不能留下 .tmp 残留。"""
    atomicio.atomic_write_json(tmpfile, {"x": 1})
    leftovers = [p.name for p in tmpfile.parent.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_read_json_missing_file_returns_default(tmp_path):
    assert atomicio.read_json(tmp_path / "nope.json", {"fallback": True}) == {
        "fallback": True}


def test_retry_stops_on_success():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("busy")
        return "ok"

    assert atomicio.retry(flaky, times=5, delay=0) == "ok"
    assert calls["n"] == 3


def test_retry_returns_none_after_exhausted():
    def always_fail():
        raise OSError("still busy")

    assert atomicio.retry(always_fail, times=3, delay=0) is None


def test_write_to_unwritable_dir_returns_error(tmp_path):
    """写不进去时必须返回错误，而不是抛异常炸掉面板。"""
    bad = tmp_path / "no_such_dir" / "x.json"
    ok, err = atomicio.atomic_write_json(bad, {"a": 1})
    # 允许两种表现：返回错误，或自动建目录成功；关键是都不能抛异常
    assert isinstance(ok, bool)
    if not ok:
        assert err
