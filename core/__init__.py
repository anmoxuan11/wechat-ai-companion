# -*- coding: utf-8 -*-
"""微信 AI 陪聊 · 2.0 核心公共模块

1.0 把 8 类职责塞在单个 ui_server.py（1503 行）里，重复代码量化结果：
    JSON 读写 9 处 / `for attempt in range(4)` 5 处 / subprocess.Popen 3 处
2.0 把这些收敛到这里，让 web 层只负责路由。
"""

from .atomicio import (  # noqa: F401
    atomic_write_bytes, atomic_write_json, atomic_write_text,
    read_json, read_text, retry, DEFAULT_KEEP,
)
from .instance import (  # noqa: F401
    LOCK_NAME, ask_identity, bind_free, lock_path, port_in_use,
    read_lock, remove_lock, resolve_conflict, stale_lock, write_lock,
)
from .paths import (  # noqa: F401
    ENV_PYTHON, base_dir, data_dir, ensure_dir, find_python, log_dir,
    meipass, python_hint, resource,
)
from .proclog import (  # noqa: F401
    alive, children_of, cleanup_temp_ps1, kill_tree, proc_mem_mb,
    proc_name, proc_start_time, proc_tree_mem_mb, prune_dir,
    rotate_if_needed, tail_text,
)
from .security import (  # noqa: F401
    CONFIRM_FIELD, DESTRUCTIVE, ENV_API_KEY, ENV_VISION_KEY, PROTECTED_POST,
    TOKEN_HEADER, TokenGuard, audit_config_secrets, is_placeholder,
    mask_key, require_confirm, resolve_api_key,
)

__all__ = [
    "atomic_write_bytes", "atomic_write_json", "atomic_write_text",
    "read_json", "read_text", "retry", "DEFAULT_KEEP",
    "LOCK_NAME", "ask_identity", "bind_free", "lock_path", "port_in_use",
    "read_lock", "remove_lock", "resolve_conflict", "stale_lock", "write_lock",
    "ENV_PYTHON", "base_dir", "data_dir", "ensure_dir", "find_python",
    "log_dir", "meipass", "python_hint", "resource",
    "alive", "children_of", "cleanup_temp_ps1", "kill_tree", "proc_mem_mb",
    "proc_name", "proc_start_time", "proc_tree_mem_mb", "prune_dir",
    "rotate_if_needed", "tail_text",
    "CONFIRM_FIELD", "DESTRUCTIVE", "ENV_API_KEY", "ENV_VISION_KEY",
    "PROTECTED_POST", "TOKEN_HEADER", "TokenGuard", "audit_config_secrets",
    "is_placeholder", "mask_key", "require_confirm", "resolve_api_key",
]
