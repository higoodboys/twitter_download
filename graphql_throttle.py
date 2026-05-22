# coding=utf-8
"""GraphQL 请求节流，降低 Rate limit exceeded 概率（仅用于 main_sync）。"""

import time

_last_graphql_at = 0.0
_min_interval_sec = 2.0
_pause_after_user_sec = 3.0


def configure(settings: dict):
    global _min_interval_sec, _pause_after_user_sec
    _min_interval_sec = float(settings.get("graphql_min_interval_sec", 2) or 0)
    _pause_after_user_sec = float(settings.get("graphql_pause_after_user_sec", 3) or 0)


def _is_graphql_url(url) -> bool:
    u = str(url)
    return "/i/api/graphql" in u or "x.com/i/api/graphql" in u or "twitter.com/i/api/graphql" in u


def wait_before_graphql(url=None):
    """两次 GraphQL 请求之间至少间隔 min_interval 秒。"""
    global _last_graphql_at
    if _min_interval_sec <= 0:
        if url is None or _is_graphql_url(url):
            _last_graphql_at = time.time()
        return
    if url is not None and not _is_graphql_url(url):
        return
    now = time.time()
    if _last_graphql_at > 0:
        elapsed = now - _last_graphql_at
        if elapsed < _min_interval_sec:
            time.sleep(_min_interval_sec - elapsed)
    _last_graphql_at = time.time()


def pause_after_user():
    """每个用户处理完后的额外等待（仅 main_sync 主循环调用）。"""
    if _pause_after_user_sec > 0:
        time.sleep(_pause_after_user_sec)


def format_config():
    return "GraphQL 间隔 %.1fs, 每用户后暂停 %.1fs" % (_min_interval_sec, _pause_after_user_sec)
