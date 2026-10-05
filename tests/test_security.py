# -*- coding: utf-8 -*-
"""core.security 测试：密钥解析优先级 + 面板令牌。

P0-2：密钥必须支持「环境变量优先」，且占位符要被当成未配置。
P0-3：写操作必须校验一次性令牌，跨站/无令牌一律拒绝。
"""

import os

import pytest

from core import security


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for n in (security.ENV_API_KEY, security.ENV_VISION_KEY):
        monkeypatch.delenv(n, raising=False)


# ---------------- 占位符识别 ----------------

@pytest.mark.parametrize("val", ["", "   ", "sk-xxxx", "sk-xxxx123",
                                 "YOUR_API_KEY", "xxx"])
def test_placeholder_detected(val):
    assert security.is_placeholder(val) is True


@pytest.mark.parametrize("val", ["sk-abcdef123456", "sk-real-key"])
def test_real_key_not_placeholder(val):
    assert security.is_placeholder(val) is False


# ---------------- 解析优先级 ----------------

def test_env_wins_over_config(monkeypatch):
    monkeypatch.setenv(security.ENV_API_KEY, "sk-ENVKEY1234")
    key, src = security.resolve_api_key("sk-CFGKEY5678")
    assert key == "sk-ENVKEY1234"
    assert "环境变量" in src


def test_falls_back_to_config():
    key, src = security.resolve_api_key("sk-CFGKEY5678")
    assert key == "sk-CFGKEY5678"
    assert src == "config.json"


def test_nothing_configured():
    key, src = security.resolve_api_key("")
    assert key == ""
    assert src == "未配置"


def test_placeholder_in_config_is_not_a_key():
    key, src = security.resolve_api_key("sk-xxxx-your-key")
    assert key == ""
    assert src == "未配置"


def test_vision_falls_back_to_generic_env(monkeypatch):
    """视觉 Key 常与对话 Key 是同一个，不能因为没单独配就失效。"""
    monkeypatch.setenv(security.ENV_API_KEY, "sk-SHARED9999")
    key, src = security.resolve_api_key("", (security.ENV_VISION_KEY,
                                             security.ENV_API_KEY))
    assert key == "sk-SHARED9999"
    assert security.ENV_API_KEY in src


# ---------------- 脱敏 ----------------

def test_mask_key_never_leaks_middle():
    m = security.mask_key("sk-abcdefghij1234")
    assert m.startswith("sk-abc")
    assert m.endswith("1234")
    assert "defghij" not in m


def test_mask_empty():
    assert security.mask_key("") == "(未配置)"


# ---------------- 审计 ----------------

def test_audit_flags_plaintext_in_config():
    cfg = {"ai": {"api_key": "sk-real1234567"}, "vision": {"api_key": ""}}
    report = security.audit_config_secrets(cfg)
    by_name = {r["name"]: r for r in report}
    assert by_name["对话 Key"]["plaintext_in_config"] is True
    assert by_name["视觉 Key"]["plaintext_in_config"] is False


def test_audit_clean_when_env_used(monkeypatch):
    monkeypatch.setenv(security.ENV_API_KEY, "sk-env1234567")
    cfg = {"ai": {"api_key": ""}, "vision": {"api_key": ""}}
    report = security.audit_config_secrets(cfg)
    assert all(r["plaintext_in_config"] is False for r in report)
    assert any(r["source"].startswith("环境变量") for r in report)


# ---------------- 令牌 ----------------

def test_guard_rejects_missing_token():
    g = security.TokenGuard(enabled=True)
    ok, reason = g.check({})
    assert ok is False
    assert reason


def test_guard_rejects_wrong_token():
    g = security.TokenGuard(enabled=True)
    ok, _ = g.check({security.TOKEN_HEADER: "wrong"})
    assert ok is False


def test_guard_accepts_correct_token():
    g = security.TokenGuard(enabled=True)
    ok, reason = g.check({security.TOKEN_HEADER: g.token})
    assert ok is True
    assert reason == ""


def test_guard_rejects_cross_origin():
    g = security.TokenGuard(enabled=True)
    ok, reason = g.check({security.TOKEN_HEADER: g.token},
                         origin_host="http://evil.example.com")
    assert ok is False
    assert "跨站" in reason


def test_guard_allows_localhost_origin():
    g = security.TokenGuard(enabled=True)
    for host in ("http://127.0.0.1:8765", "http://localhost:8765"):
        ok, _ = g.check({security.TOKEN_HEADER: g.token}, origin_host=host)
        assert ok is True, host


def test_guard_tokens_are_unique_per_launch():
    a = security.TokenGuard(enabled=True)
    b = security.TokenGuard(enabled=True)
    assert a.token != b.token


def test_meta_tag_contains_token():
    g = security.TokenGuard(enabled=True)
    tag = g.meta_tag()
    assert g.token in tag
    assert tag.startswith("<meta")


# ---------------- 破坏性操作确认 ----------------

def test_destructive_requires_confirm():
    for p in security.DESTRUCTIVE:
        assert security.require_confirm(p, {}) is False
        assert security.require_confirm(p, {"confirm": True}) is True


def test_normal_endpoint_no_confirm_needed():
    assert security.require_confirm("/api/pause", {}) is True


def test_protected_post_is_a_superset_of_destructive():
    assert security.DESTRUCTIVE <= security.PROTECTED_POST
