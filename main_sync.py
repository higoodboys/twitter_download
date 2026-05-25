# coding=utf-8
"""
一站式同步：媒体直接保存到 twitter/{用户}/{tid}/，下载后即生成 HTML 并入库（不写 CSV）。
不修改 main.py；复用其 GraphQL/下载 API 与 csv_to_twitter 入库函数。

目录结构:
  twitter/{screen_name}/cache_data.log  已下载媒体 URL（可选）
  twitter/{screen_name}/{tid}/媒体文件
  twitter/{screen_name}/{tid}/Z{tid}.html
  twitter/sync_state.json               计数增量（是否拉时间线）

增量更新（media_count 增加）时只拉第一页时间线。

用法: python3 main_sync.py
"""

import asyncio
import json
import os
import re
import signal
import sys
import time
from contextlib import contextmanager

import httpx

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import main as m
import csv_to_twitter as ctt
from user_info import User_info
from cache_gen import cache_gen
from sync_state import SyncState
from graphql_throttle import configure as configure_graphql_throttle
from graphql_throttle import format_config as graphql_throttle_config_str
from graphql_throttle import pause_after_user as graphql_pause_after_user
from graphql_throttle import wait_before_graphql
from sync_priority import clear_rate_limited, get_pending, mark_rate_limited, sort_users
from twitter_info_db import save_user_info_to_db
from url_utils import quote_url

try:
    from md_gen import md_gen
except ImportError:
    md_gen = None

TWITTER_ROOT = ctt.TWITTER_DIR
SYNC_STATE_PATH = TWITTER_ROOT + os.sep

_api_rate_limited = False


class SyncRunSession:
    """记录当次运行更新明细，并支持 Ctrl+C 优雅停止。"""

    def __init__(self):
        self.shutdown_requested = False
        self._sigint_count = 0
        self.updated_users = []
        self.processed_users = []
        self._pending_user = None

    def handle_sigint(self, signum, frame):
        self._sigint_count += 1
        if self._sigint_count >= 2:
            print("\n再次按下 Ctrl+C，立即退出。")
            raise SystemExit(130)
        if not self.shutdown_requested:
            self.shutdown_requested = True
            cur = self._pending_user or "（无）"
            print(
                "\n" + "=" * 50
                + "\n收到 Ctrl+C：将完成当前用户 @%s 的处理，随后停止后续用户。"
                % cur
                + "\n正在收尾，请稍候…（再按一次 Ctrl+C 可强制退出）"
                + "\n" + "=" * 50,
                flush=True,
            )

    def set_current_user(self, screen_name):
        self._pending_user = screen_name

    def clear_current_user(self):
        self._pending_user = None

    def _entry_has_update(self, entry):
        return bool(entry.get("media")) or entry.get("db") == "insert" or entry.get("html")

    def record_user(self, screen_name, display_name, status, pub_stats, fetch_reason=None):
        entries = pub_stats.get("entries") or []
        entries = [e for e in entries if self._entry_has_update(e)]
        rec = {
            "screen_name": screen_name,
            "display_name": display_name or screen_name,
            "status": status,
            "fetch_reason": fetch_reason,
            "stats": dict(pub_stats),
            "entries": entries,
        }
        self.processed_users.append(rec)
        if entries:
            self.updated_users.append(rec)

    def print_update_report(self, t0, totals, interrupted=False):
        elapsed = time.time() - t0
        print("\n" + "=" * 50)
        if interrupted:
            print("当次同步摘要（已中断）")
        else:
            print("当次同步摘要（正常结束）")
        print("耗时: %.1f 秒" % elapsed)
        print(
            "用户: 已处理 %d / 计划 %d，有更新 %d"
            % (
                totals.get("processed", 0),
                totals.get("planned", 0),
                len(self.updated_users),
            )
        )
        print(
            "状态: 下载完成 %d, 跳过 %d, 限流 %d, 失败 %d"
            % (
                totals.get("n_dl", 0),
                totals.get("n_skip", 0),
                totals.get("n_rate", 0),
                totals.get("n_fail", 0),
            )
        )
        print(
            "入库: HTML %d, 新增 %d, 已存在 %d, 失败 %d, 跳过媒体 %d"
            % (
                totals.get("total_html", 0),
                totals.get("total_ins", 0),
                totals.get("total_ext", 0),
                totals.get("total_fail", 0),
                totals.get("total_skip_media", 0),
            )
        )
        print("API %d 次, 新媒体 %d 份" % (totals.get("api", 0), totals.get("media", 0)))

        if not self.updated_users:
            print("\n当次无新媒体/入库更新。")
        else:
            print("\n--- 当次更新明细（用户 → 条目）---")
            for u in self.updated_users:
                reason = u.get("fetch_reason")
                extra = (" [%s]" % reason) if reason else ""
                print("\n@%s (%s)%s" % (u["screen_name"], u["display_name"], extra))
                for e in u["entries"]:
                    title = (e.get("title") or e["tid"])[:40]
                    parts = ["tid=%s" % e["tid"], title]
                    if e.get("media"):
                        parts.append("媒体: %s" % ", ".join(e["media"]))
                    tags = []
                    if e.get("html"):
                        tags.append("HTML")
                    if e.get("db") == "insert":
                        tags.append("入库")
                    elif e.get("db") == "fail":
                        tags.append("入库失败")
                    if tags:
                        parts.append("|".join(tags))
                    print("  · %s" % " | ".join(parts))

        if interrupted and totals.get("remaining_users"):
            rem = totals["remaining_users"]
            print("\n未处理用户 (%d):" % len(rem))
            show = rem[:20]
            print("  " + ", ".join("@%s" % x for x in show))
            if len(rem) > 20:
                print("  … 另有 %d 个" % (len(rem) - 20))
        print("=" * 50)
_orig_httpx_get = httpx.get


def _mark_rate_limit_text(text):
    global _api_rate_limited
    if text and "Rate limit exceeded" in text:
        _api_rate_limited = True
        return True
    return False


def _graphql_timeout():
    s = m.settings
    connect = float(s.get("graphql_connect_timeout_sec", 15))
    read = float(s.get("graphql_read_timeout_sec", 90))
    return (connect, read)


def _tracking_httpx_get(*args, **kwargs):
    url = args[0] if args else kwargs.get("url", "")
    wait_before_graphql(url)
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = _graphql_timeout()
    resp = _orig_httpx_get(*args, **kwargs)
    _mark_rate_limit_text(getattr(resp, "text", "") or "")
    return resp


@contextmanager
def _rate_limit_watch():
    global _api_rate_limited
    _api_rate_limited = False
    httpx.get = _tracking_httpx_get
    try:
        yield
    finally:
        httpx.get = _orig_httpx_get


def get_other_info_checked(user_info):
    """与 main.get_other_info 相同，并检测限流。"""
    url = (
        'https://twitter.com/i/api/graphql/xc8f1g7BYqr6VTzTbvNlGw/UserByScreenName?variables={"screen_name":"'
        + user_info.screen_name
        + '","withSafetyModeUserFields":false}&features={"hidden_profile_likes_enabled":false,"hidden_profile_subscriptions_enabled":false,"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"subscriptions_verification_info_verified_since_enabled":true,"highlights_tweets_tab_ui_enabled":true,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"responsive_web_graphql_timeline_navigation_enabled":true}&fieldToggles={"withAuxiliaryUserLabels":false}'
    )
    try:
        m.request_count += 1
        response = _tracking_httpx_get(
            quote_url(url), headers=m._headers, proxy=m.proxies, timeout=_graphql_timeout()
        ).text
        if _mark_rate_limit_text(response):
            print("获取信息失败: Rate limit exceeded")
            return False
        raw_data = json.loads(response)
        user_info.rest_id = raw_data["data"]["user"]["result"]["rest_id"]
        user_info.name = raw_data["data"]["user"]["result"]["legacy"]["name"]
        user_info.statuses_count = raw_data["data"]["user"]["result"]["legacy"]["statuses_count"]
        user_info.media_count = raw_data["data"]["user"]["result"]["legacy"]["media_count"]
    except json.JSONDecodeError:
        print("获取信息失败")
        if _api_rate_limited:
            print("Rate limit exceeded")
        return False
    except Exception as e:
        print("获取信息失败")
        print(e)
        return False
    return True


def _user_list():
    raw = m.settings.get("user_lst", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def _user_meta_dir(screen_name):
    return os.path.join(TWITTER_ROOT, screen_name)


def _tweet_dir(screen_name, tid):
    return os.path.join(TWITTER_ROOT, screen_name, tid)


def extract_tid(expanded_url):
    hit = re.search(r"status/(\d+)", expanded_url or "")
    return hit.group(1) if hit else None


def _msecs_to_tweet_date(msecs):
    return ctt.format_tweet_date(
        time.strftime("%Y-%m-%d %H:%M", time.localtime(int(msecs) / 1000))
    )


def _list_tweet_media(dest_dir):
    """列出 tid 目录下已下载的媒体文件名。"""
    if not os.path.isdir(dest_dir):
        return []
    out = []
    for name in sorted(os.listdir(dest_dir)):
        if "-img_" in name or "-vid_" in name:
            out.append(name)
    return out


def _tweet_fully_present(screen_name, tid, db_ok):
    """本地有媒体且（未开库同步或库中已有该 tid）。"""
    dest = _tweet_dir(screen_name, tid)
    if not _list_tweet_media(dest):
        return False
    if db_ok and not ctt.tid_exists_in_db(tid):
        return False
    return True


def _publish_tid(screen_name, tid, tweet_content, tweet_msecs, db_ok):
    """
    根据 tid 目录内已有媒体生成 HTML 并入库。
    返回 (html_new, db_result) 其中 db_result 为 insert|exist|fail|skip。
    """
    dest_dir = _tweet_dir(screen_name, tid)
    names = _list_tweet_media(dest_dir)
    if not names:
        return 0, "skip"

    media_list = []
    for name in names:
        media_list.append({
            "type": "Video" if name.lower().endswith(".mp4") else "Image",
            "filename": name,
        })

    html_new = 0
    html_path = os.path.join(dest_dir, "Z%s.html" % tid)
    title = ctt.extract_title(tweet_content) or tid
    if not os.path.isfile(html_path):
        print("保存: %s" % html_path)
        html = ctt.build_html(title, tweet_content, media_list)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        html_new = 1

    if not db_ok:
        return html_new, "skip"

    pic_num = len(media_list)
    res_type = 1 if any(
        "Video" in x["type"] or x["filename"].lower().endswith(".mp4")
        for x in media_list
    ) else 0
    resource = "<!>".join(x["filename"] for x in media_list)
    result = ctt.save_to_db(
        tid=tid,
        thread_title=title,
        user_name=screen_name,
        pic_num=pic_num,
        res_type=res_type,
        resource=resource,
        tweet_date=_msecs_to_tweet_date(tweet_msecs),
    )
    return html_new, result, title


def _ensure_entry(entries_map, tid, tweet_content=""):
    if tid not in entries_map:
        title = ctt.extract_title(tweet_content) or tid
        entries_map[tid] = {
            "tid": tid,
            "title": title,
            "media": [],
            "html": False,
            "db": None,
        }
    elif tweet_content and not entries_map[tid].get("title"):
        entries_map[tid]["title"] = ctt.extract_title(tweet_content) or tid
    return entries_map[tid]


def _reset_fetch_globals():
    m.start_label = True
    m.First_Page = True


def _auto_sync_from_twitter_dirs(user_info, meta_dir):
    """根据 twitter/{user}/{tid}/ 下已有文件名调整时间起点。"""
    if not os.path.isdir(meta_dir):
        return
    re_rule = r"\d{4}-\d{2}-\d{2}"
    latest = None
    for name in os.listdir(meta_dir):
        path = os.path.join(meta_dir, name)
        if not os.path.isdir(path) or not name.isdigit():
            continue
        for fname in os.listdir(path):
            if "-img_" in fname or "-vid_" in fname:
                mtime = os.path.getmtime(os.path.join(path, fname))
                if latest is None or mtime > latest:
                    latest = mtime
    if latest is not None:
        m.start_time_stamp = int(latest * 1000)


def _media_download_timeout():
    """与 main.py 默认一致；可在 settings 覆盖。"""
    s = m.settings
    connect = float(s.get("media_connect_timeout_sec", 3.05))
    read = float(s.get("media_read_timeout_sec", 16))
    return (connect, read)


def _media_max_retries():
    return int(m.settings.get("media_max_retries", 50))


def _retry_sleep_seconds(retries):
    """main.py 无间隔立即重试；此处加短延迟避免日志刷屏。"""
    base = float(m.settings.get("media_retry_delay_sec", 2))
    if base <= 0:
        return 0
    return min(base * retries, 30)


def _rate_limit_cooldown_sec():
    return float(m.settings.get("rate_limit_cooldown_sec", 120) or 0)


def _sleep_interruptible(session, seconds, reason=""):
    """可响应 Ctrl+C 的等待；返回 False 表示被用户中断。"""
    if seconds <= 0:
        return True
    if reason:
        print(reason, flush=True)
    deadline = time.time() + seconds
    last_left = -1
    while time.time() < deadline:
        if session.shutdown_requested:
            print("\n等待期间收到停止请求，结束等待。", flush=True)
            return False
        left = int(deadline - time.time())
        if left != last_left and left > 0 and left % 30 == 0:
            print("  … 剩余约 %d 秒" % left, flush=True)
            last_left = left
        time.sleep(min(1.0, max(0, deadline - time.time())))
    return True


def sync_download_control(user_info, meta_dir, db_ok, single_page_only=False):
    """下载到 twitter/{user}/{tid}/，每完成一条媒体即尝试 HTML+入库。"""
    stats = {
        "html": 0, "insert": 0, "exist": 0, "fail": 0, "skip_media": 0,
        "entries": [],
    }
    entries_map = {}

    async def _run():
        pending_publish = {}

        def _queue_publish(tid, tweet_content, tweet_msecs):
            pending_publish[tid] = (tweet_content, tweet_msecs)
            _ensure_entry(entries_map, tid, tweet_content)

        def _flush_publish():
            for tid, (content, msecs) in pending_publish.items():
                h, r, title = _publish_tid(user_info.screen_name, tid, content, msecs, db_ok)
                stats["html"] += h
                _tally_db(stats, r)
                ent = _ensure_entry(entries_map, tid, content)
                if title:
                    ent["title"] = title[:40]
                if h:
                    ent["html"] = True
                if r in ("insert", "exist", "fail"):
                    ent["db"] = r
            pending_publish.clear()

        async def down_save(url, prefix, csv_info, order):
            tid = extract_tid(csv_info[3])
            if not tid:
                print("  跳过无 tid: %s" % csv_info[3])
                return

            tweet_path = _tweet_dir(user_info.screen_name, tid)
            os.makedirs(tweet_path, exist_ok=True)

            if ".mp4" in url:
                fname = "%s_%d.mp4" % (prefix, user_info.count + order)
            else:
                try:
                    if m.orig_format:
                        url += "?name=orig"
                        ext = csv_info[5][-3:] if len(csv_info[5]) >= 3 else m.img_format
                        fname = "%s_%d.%s" % (prefix, user_info.count + order, ext)
                    else:
                        fname = "%s_%d.%s" % (prefix, user_info.count + order, m.img_format)
                        if m.img_format != "png":
                            url += "?format=jpg&name=4096x4096"
                        else:
                            url += "?format=png&name=4096x4096"
                except Exception:
                    print(url)
                    return

            file_path = os.path.join(tweet_path, fname)
            tweet_msecs = csv_info[0]
            tweet_content = csv_info[7] if len(csv_info) > 7 else ""

            if _tweet_fully_present(user_info.screen_name, tid, db_ok):
                stats["skip_media"] += 1
                return

            if os.path.isfile(file_path):
                _queue_publish(tid, tweet_content, tweet_msecs)
                return

            if m.md_output and md_writer:
                md_writer.media_tweet_input(csv_info, prefix)

            retries = 0
            is_video = ".mp4" in url
            max_retries = _media_max_retries()
            timeouts = _media_download_timeout()
            while True:
                try:
                    async with semaphore:
                        async with httpx.AsyncClient(proxy=m.proxies) as client:
                            m.down_count += 1
                            resp = await client.get(quote_url(url), timeout=timeouts)
                            if resp.status_code == 404:
                                raise Exception("404")
                    with open(file_path, "wb") as f:
                        f.write(resp.content)
                    if m.log_output:
                        print("%s =====> 下载完成" % file_path)
                    ent = _ensure_entry(entries_map, tid, tweet_content)
                    if fname not in ent["media"]:
                        ent["media"].append(fname)
                    _queue_publish(tid, tweet_content, tweet_msecs)
                    return
                except Exception as e:
                    if is_video or m.orig_format or str(e) != "404":
                        retries += 1
                        if retries >= max_retries:
                            print("%s =====> 下载失败已跳过" % file_path)
                            print(url)
                            break
                        delay = _retry_sleep_seconds(retries)
                        if delay > 0:
                            print("%s =====> 第%d次下载失败, %ds 后重试" % (file_path, retries, delay))
                            await asyncio.sleep(delay)
                        else:
                            print("%s =====> 第%d次下载失败,正在重试" % (file_path, retries))
                            print(url)
                    else:
                        url = url.replace("name=orig", "name=4096x4096")

        while True:
            try:
                photo_lst = m.get_download_url(user_info)
            except Exception as e:
                print("获取时间线异常: %s" % e)
                break
            if photo_lst is False or not photo_lst:
                break
            if photo_lst[0] is True:
                continue
            coros = []
            for order, item in enumerate(photo_lst):
                if cache_writer and not cache_writer.is_present(item[0]):
                    continue
                coros.append(down_save(item[0], item[1], item[2], order))
            if coros:
                semaphore = asyncio.Semaphore(m.max_concurrent_requests)
                await asyncio.gather(*coros)
            _flush_publish()
            user_info.count += len(photo_lst)
            if single_page_only:
                print("  增量模式：仅拉取第 1 页时间线")
                break

    asyncio.run(_run())
    stats["entries"] = list(entries_map.values())
    return stats


def _tally_db(stats, result):
    if result == "insert":
        stats["insert"] += 1
    elif result == "exist":
        stats["exist"] += 1
    elif result == "fail":
        stats["fail"] += 1


def download_user(user_info, db_ok=True):
    """
    拉取时间线并直接写入 twitter 目录。
    返回: (status, publish_stats, fetch_reason)
    publish_stats 含 entries 列表（当次有变化的 tid 明细）。
    """
    empty_stats = {
        "html": 0, "insert": 0, "exist": 0, "fail": 0, "skip_media": 0, "entries": [],
    }
    fetch_reason = None
    token = re.findall(r"ct0=(.*?);", m._headers["cookie"])
    if not token:
        print("cookie 缺少 ct0")
        return "failed", empty_stats, fetch_reason
    m._headers["x-csrf-token"] = token[0]
    m._headers["referer"] = "https://twitter.com/" + user_info.screen_name

    with _rate_limit_watch():
        if not get_other_info_checked(user_info):
            if _api_rate_limited:
                mark_rate_limited(user_info.screen_name)
                return "rate_limited", empty_stats, fetch_reason
            return "failed", empty_stats, fetch_reason

        if m.db_sync:
            save_user_info_to_db(user_info.screen_name, user_info.name, m.db_config)

        m.print_info(user_info)
        sync_state = SyncState(SYNC_STATE_PATH)
        sn = user_info.screen_name
        meta_dir = _user_meta_dir(sn)
        os.makedirs(meta_dir, exist_ok=True)
        user_info.save_path = meta_dir + os.sep

        if not m.has_likes and not m.has_highlights and (user_info.media_count or 0) == 0:
            sync_state.update(sn, user_info.media_count, user_info.statuses_count)
            clear_rate_limited(sn)
            print("%s: 含媒体推数为 0，跳过下载\n" % user_info.name)
            return "skipped", empty_stats, fetch_reason

        should_fetch, skip_reason = sync_state.should_fetch(
            sn, user_info.media_count, user_info.statuses_count, m.ignore_count_sync
        )
        fetch_reason = skip_reason
        single_page_only = skip_reason == "increased"
        prev = sync_state.get(sn)
        if prev:
            print(
                "计数同步: 上次 media=%s statuses=%s → 当前 media=%s statuses=%s"
                % (
                    prev.get("media_count"), prev.get("statuses_count"),
                    user_info.media_count, user_info.statuses_count,
                )
            )
        if not should_fetch:
            sync_state.update(sn, user_info.media_count, user_info.statuses_count)
            clear_rate_limited(sn)
            print("%s: %s，跳过时间线下载\n" % (user_info.name, skip_reason))
            return "skipped", empty_stats, fetch_reason

        if single_page_only:
            print("%s: 计数有增量，仅同步第 1 页新内容\n" % user_info.name)

        global cache_writer, md_writer
        md_writer = None
        if m.md_output and md_gen:
            md_writer = md_gen(
                meta_dir, user_info.name, sn, m.settings["time_range"],
                m.has_likes, m.media_count_limit,
            )

        cache_writer = cache_gen(meta_dir) if m.down_log else None
        try:
            if m.autoSync:
                _auto_sync_from_twitter_dirs(user_info, meta_dir)

            pub_stats = sync_download_control(
                user_info, meta_dir, db_ok, single_page_only=single_page_only
            )
        finally:
            if md_writer:
                md_writer.md_close()
            if cache_writer:
                try:
                    cache_writer.save()
                except Exception:
                    pass
                cache_writer = None

        if _api_rate_limited:
            mark_rate_limited(sn)
            print("%s: 下载中触发限流，已标记下次优先\n" % user_info.name)
            return "rate_limited", pub_stats, fetch_reason

    sync_state.update(sn, user_info.media_count, user_info.statuses_count)
    clear_rate_limited(sn)
    print("%s: 下载完成\n" % user_info.name)
    return "downloaded", pub_stats, fetch_reason


cache_writer = None
md_writer = None


def run():
    os.makedirs(TWITTER_ROOT, exist_ok=True)
    ctt.apply_db_config_from_settings(m.settings)
    configure_graphql_throttle(m.settings)
    users = sort_users(_user_list())

    if not users:
        print("user_lst 为空")
        return

    session = SyncRunSession()
    old_sigint = signal.signal(signal.SIGINT, session.handle_sigint)

    total_users = len(users)
    pending = get_pending()
    pending_in_run = [u for u in users if u in pending]

    print("=" * 50)
    print("main_sync: 直存 twitter/ + 下载即入库（无 CSV）")
    print("用户: %d（Ctrl+C 将完成当前用户后停止）" % total_users)
    if pending_in_run:
        print("限流优先: %d 个 (%s)" % (
            len(pending_in_run),
            ", ".join(pending_in_run[:8]) + ("..." if len(pending_in_run) > 8 else ""),
        ))
    print("根目录: %s" % TWITTER_ROOT)
    print("数据库: %s" % ctt.DB_HOST)
    print("节流: %s" % graphql_throttle_config_str())
    cooldown = _rate_limit_cooldown_sec()
    if cooldown > 0:
        print("限流冷却: 触发后等待 %.0f 秒再试下一用户" % cooldown)
    print("=" * 50)

    t0 = time.time()
    n_dl = n_skip = n_fail = n_rate = 0
    processed_count = 0
    total_html = total_ins = total_ext = total_fail = total_skip_media = 0
    db_ok = True
    interrupted = False

    try:
        for idx, screen_name in enumerate(users, 1):
            if session.shutdown_requested:
                interrupted = True
                print("\n已请求停止，不再处理后续用户。")
                break

            print("\n" + "=" * 50)
            print("[%d/%d] @%s" % (idx, total_users, screen_name))
            if screen_name in pending:
                print("(上次限流，本次优先)")
            print("=" * 50)

            session.set_current_user(screen_name)
            try:
                _reset_fetch_globals()
                ui = User_info(screen_name)
                status, pub, fetch_reason = download_user(ui, db_ok=db_ok)
                if pub.get("fail"):
                    db_ok = False
                total_html += pub.get("html", 0)
                total_ins += pub.get("insert", 0)
                total_ext += pub.get("exist", 0)
                total_fail += pub.get("fail", 0)
                total_skip_media += pub.get("skip_media", 0)

                if status == "downloaded":
                    n_dl += 1
                elif status == "skipped":
                    n_skip += 1
                elif status == "rate_limited":
                    n_rate += 1
                else:
                    n_fail += 1

                session.record_user(
                    screen_name, ui.name, status, pub, fetch_reason=fetch_reason
                )
                processed_count += 1

                print(
                    "@%s: HTML+%d 入库+%d 已存在%d 失败%d 跳过媒体%d | 总进度 %d/%d"
                    % (
                        screen_name, pub.get("html", 0), pub.get("insert", 0),
                        pub.get("exist", 0), pub.get("fail", 0), pub.get("skip_media", 0),
                        idx, total_users,
                    )
                )
                changed = [
                    e for e in pub.get("entries", [])
                    if session._entry_has_update(e)
                ]
                if changed:
                    print("  当次更新 %d 条:" % len(changed))
                    for e in changed[:5]:
                        print("    · tid=%s %s" % (e["tid"], (e.get("title") or "")[:30]))
                    if len(changed) > 5:
                        print("    … 另有 %d 条" % (len(changed) - 5))
            finally:
                session.clear_current_user()

            if session.shutdown_requested:
                interrupted = True
                print("\n当前用户 @%s 已处理完毕，按 Ctrl+C 要求停止后续任务。" % screen_name)
                break

            if status == "rate_limited":
                cooldown = _rate_limit_cooldown_sec()
                if cooldown > 0 and idx < total_users:
                    ok = _sleep_interruptible(
                        session,
                        cooldown,
                        "\nAPI 限流：暂停 %.0f 秒后再处理下一用户（剩余 %d/%d）…"
                        % (cooldown, total_users - idx, total_users),
                    )
                    if not ok:
                        interrupted = True
                        break
                continue

            graphql_pause_after_user()

        still_pending = get_pending()
        if still_pending and not interrupted:
            print("\n限流队列剩余 %d: %s" % (
                len(still_pending),
                ", ".join(still_pending[:12]) + ("..." if len(still_pending) > 12 else ""),
            ))

        remaining = users[processed_count:] if interrupted else []
        totals = {
            "processed": processed_count,
            "planned": total_users,
            "n_dl": n_dl,
            "n_skip": n_skip,
            "n_rate": n_rate,
            "n_fail": n_fail,
            "total_html": total_html,
            "total_ins": total_ins,
            "total_ext": total_ext,
            "total_fail": total_fail,
            "total_skip_media": total_skip_media,
            "api": m.request_count,
            "media": m.down_count,
            "remaining_users": remaining,
        }
        session.print_update_report(t0, totals, interrupted=interrupted)
    finally:
        signal.signal(signal.SIGINT, old_sigint)


if __name__ == "__main__":
    run()
