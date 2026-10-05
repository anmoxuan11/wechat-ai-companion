# -*- coding: utf-8 -*-
"""ui_server 纯逻辑测试（不启 HTTP 服务）。

重点覆盖：暂停开关的读写闭环、配置读写、面板状态字段完整性。
这些是 1.0 出过事的地方（控制文件写坏 / 状态字段缺失导致前端白屏）。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ui_server  # noqa: E402


@pytest.fixture
def tmp_control(tmp_path, monkeypatch):
    p = tmp_path / "bot_control.json"
    monkeypatch.setattr(ui_server, "CONTROL_PATH", str(p))
    return p


def test_set_paused_roundtrip(tmp_control):
    assert ui_server.set_paused(True) is True
    assert ui_server.read_control()["paused"] is True

    assert ui_server.set_paused(False) is True
    assert ui_server.read_control()["paused"] is False


def test_set_paused_coerces_to_bool(tmp_control):
    ui_server.set_paused("yes")     # 真值字符串
    assert ui_server.read_control()["paused"] is True
    ui_server.set_paused(0)
    assert ui_server.read_control()["paused"] is False


def test_set_paused_preserves_other_fields(tmp_control):
    tmp_control.write_text(json.dumps({"paused": False, "note": "keep me"}),
                           encoding="utf-8")
    ui_server.set_paused(True)
    ctrl = ui_server.read_control()
    assert ctrl["paused"] is True
    assert ctrl["note"] == "keep me"


def test_read_control_on_missing_file(tmp_control):
    assert ui_server.read_control() == {}


def test_read_control_on_corrupt_file(tmp_control):
    tmp_control.write_text("{broken json", encoding="utf-8")
    assert ui_server.read_control() == {}


def test_version_is_3():
    assert ui_server.VERSION.startswith("3")


def test_identity_fields():
    """前端和单实例锁都依赖这几个字段，缺一个就会误判。"""
    for attr in ("BASE_DIR", "PORT", "VERSION"):
        assert hasattr(ui_server, attr), attr
    assert isinstance(ui_server.PORT, int)


def _fake_self_test_kwargs():
    """构造一组「bot 未运行」的最小输入，体检不应因此崩掉。"""
    cfg = {"listen": ["测试"], "ai": {"api_key": ""}, "vision": {"api_key": ""}}
    return dict(
        cfg=cfg,
        ctrl={"paused": False},
        bot_status={},
        pid=None,
        log_paths=[],
        control_roundtrip=lambda: True,
    )


def test_self_test_items_have_expected_shape():
    """体检结果必须是 {ok, passed, total, items:[{name, ok, ...}]}，前端按这个渲染。"""
    from web import state as web_state
    res = web_state.self_test_items(**_fake_self_test_kwargs())
    assert isinstance(res, dict)
    for key in ("ok", "passed", "total", "items"):
        assert key in res, key
    assert res["items"], "体检项不能为空"
    for it in res["items"]:
        assert "name" in it and "ok" in it, it
        assert isinstance(it["ok"], bool)
    assert 0 <= res["passed"] <= res["total"]


def test_self_test_covers_key_and_interpreter():
    """2.0 新增的两项必须存在：解释器可移植性 + 密钥是否明文落盘。"""
    from web import state as web_state
    res = web_state.self_test_items(**_fake_self_test_kwargs())
    names = [i["name"] for i in res["items"]]
    assert "Python 解释器" in names
    assert "API 密钥" in names


def test_self_test_text_report_not_empty():
    from web import state as web_state
    res = web_state.self_test_items(**_fake_self_test_kwargs())
    txt = web_state.text_report(res, {"running": False})
    assert isinstance(txt, str) and txt.strip()
    assert "自检报告" in txt
