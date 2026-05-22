# coding=utf-8
"""记录因 API 限流失败的用户，下次 main_sync 优先处理。"""

import json
import os
from datetime import datetime

PRIORITY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "twitter",
    "sync_priority.json",
)


def _load():
    if os.path.isfile(PRIORITY_FILE):
        with open(PRIORITY_FILE, "r", encoding="utf8") as f:
            data = json.load(f)
    else:
        data = {}
    pending = data.get("rate_limit_pending", [])
    if not isinstance(pending, list):
        pending = []
    return data, pending


def _save(pending):
    os.makedirs(os.path.dirname(PRIORITY_FILE), exist_ok=True)
    with open(PRIORITY_FILE, "w", encoding="utf8") as f:
        json.dump(
            {
                "rate_limit_pending": pending,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def get_pending():
    _, pending = _load()
    return list(pending)


def mark_rate_limited(screen_name: str):
    _, pending = _load()
    if screen_name not in pending:
        pending.append(screen_name)
        _save(pending)
        print("已标记限流优先: @%s（下次将优先更新）" % screen_name)


def clear_rate_limited(screen_name: str):
    _, pending = _load()
    if screen_name in pending:
        pending.remove(screen_name)
        _save(pending)


def sort_users(users):
    """限流待重试用户排在前面，其余保持原顺序。"""
    pending = set(get_pending())
    if not pending:
        return users
    front = [u for u in users if u in pending]
    rest = [u for u in users if u not in pending]
    return front + rest
