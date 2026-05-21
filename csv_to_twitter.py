# coding=utf-8
"""
将 twitter_download 目录下各用户 CSV 及媒体文件，
按 twitter/twitter.py 的目录结构、HTML 格式写入 twitter/ 目录，并同步到数据库。
"""

import csv
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime

import pymysql

# 与 twitter.twitter 类保持一致
BBS = 5
DB_HOST = "192.168.2.181"
DB_USER = "root"
DB_PASSWORD = "liuwei22"
DB_NAME = "p91"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TWITTER_DIR = os.path.join(SCRIPT_DIR, "twitter")
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")
IMPORT_CONFIG_PATH = os.path.join(SCRIPT_DIR, "csv_import.json")

CSV_HEADER = [
    "Tweet Date", "Display Name", "User Name", "Tweet URL", "Media Type",
    "Media URL", "Saved Filename", "Tweet Content", "Favorite Count",
    "Retweet Count", "Reply Count",
]


def load_user_list():
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        settings = json.load(f)
    raw = settings.get("user_lst", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def load_import_config():
    if os.path.isfile(IMPORT_CONFIG_PATH):
        with open(IMPORT_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {"reparse_all": False, "parsed_csv": {}}
    if "parsed_csv" not in cfg:
        cfg["parsed_csv"] = {}
    return cfg


def save_import_config(cfg):
    with open(IMPORT_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)


def _csv_cache_key(screen_name, fname):
    return "%s/%s" % (screen_name, fname)


def should_parse_csv(cfg, key, mtime):
    if cfg.get("reparse_all", False):
        return True
    rec = cfg.get("parsed_csv", {}).get(key)
    if not rec:
        return True
    return rec.get("mtime") != mtime


def extract_tid(tweet_url):
    m = re.search(r"status/(\d+)", tweet_url)
    return m.group(1) if m else None


def _has_chinese(s):
    return bool(re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", s))


def extract_title(content, max_len=50):
    """去除首尾英文、空白、换行、链接，保留中间中文描述。"""
    if not content:
        return ""
    text = re.sub(r"https?://\S+", "", content)
    text = re.sub(r"[\r\n\t]+", " ", text)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    parts = []
    for ln in lines:
        ln = re.sub(r"https?://\S+", "", ln).strip()
        if _has_chinese(ln):
            parts.append(ln)
    text = " ".join(parts) if parts else text.strip()
    # 去掉首尾 ASCII（\w 会匹配中文，故仅用 a-zA-Z0-9_）
    _edge = r"[\sA-Za-z0-9_.,!?;:\-@#_/\\()+'\"`~\[\]{}|]+"
    text = re.sub("^" + _edge, "", text)
    text = re.sub(_edge + "$", "", text)
    text = text.strip()
    return text[:max_len] if text else ""


def format_tweet_date(date_str):
    """CSV 为 YYYY-MM-DD HH:MM，补全为 YYYY-MM-DD HH:MM:SS。"""
    if not date_str or not str(date_str).strip():
        return ""
    s = str(date_str).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", s):
        return s
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", s):
        return s + ":00"
    return s


def mysql_utf8_safe(s):
    """MySQL utf8 不支持 4 字节字符（emoji 等），写入前过滤。"""
    if not s:
        return ""
    return "".join(c for c in str(s) if ord(c) <= 0xFFFF)


def sql_escape(s):
    if s is None:
        return ""
    return str(s).replace("\\", "\\\\").replace("'", "''")


def _parse_one_csv(fpath):
    """解析单个 csv 文件，返回行数据列表。"""
    with open(fpath, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    data_start = 0
    for i, row in enumerate(rows):
        if len(row) >= 4 and row[0] == "Tweet Date":
            data_start = i + 1
            break
        if i >= 3:
            data_start = i
            break
    return rows[data_start:]


def parse_csv_files(user_dir, screen_name, import_cfg):
    """解析用户目录下 csv，按 tid 聚合；跳过配置中已解析且未改动的文件。"""
    tweets = defaultdict(lambda: {
        "tweet_date": "",
        "display_name": "",
        "user_name": "",
        "tweet_url": "",
        "tweet_content": "",
        "media": [],
    })
    parsed_keys = []
    skipped = 0

    for fname in sorted(os.listdir(user_dir)):
        if not fname.endswith(".csv"):
            continue
        fpath = os.path.join(user_dir, fname)
        key = _csv_cache_key(screen_name, fname)
        mtime = os.path.getmtime(fpath)

        if not should_parse_csv(import_cfg, key, mtime):
            skipped += 1
            print("  跳过已解析 csv: %s" % key)
            continue

        print("  解析 csv: %s" % key)
        for row in _parse_one_csv(fpath):
            if len(row) < 8:
                continue
            while len(row) < 11:
                row.append("")

            tid = extract_tid(row[3])
            if not tid:
                continue

            t = tweets[tid]
            t["tweet_date"] = format_tweet_date(row[0])
            t["display_name"] = row[1]
            t["user_name"] = row[2].lstrip("@")
            t["tweet_url"] = row[3]
            t["tweet_content"] = row[7]
            saved = row[6].strip()
            if saved:
                existing = {m["filename"] for m in t["media"]}
                if saved not in existing:
                    t["media"].append({
                        "type": row[4],
                        "filename": saved,
                    })

        parsed_keys.append((key, mtime))

    return tweets, parsed_keys, skipped


def build_html(title, content, media_list):
    """参考 twitter.py 909-916 行生成 HTML。"""
    img_str = "</br>"
    img_str2 = "</br>"

    for m in media_list:
        name = m["filename"]
        if "Video" in m["type"] or name.lower().endswith(".mp4"):
            img_str += f'<a href="{name}">{name}</a></br>'
            img_str += (
                f'<video src="{name}" type="video/mp4" '
                f'style="width:100%" controls="controls"></video>'
            )
        else:
            img_str += f'<img src="{name}" style="width:24%"/>'
            img_str2 += f'<img src="{name}" style="width:100%"/>'

    body_content = content.replace("\n", "<br/>")
    html = (
        '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"/>\n'
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />'
        f"<title>{title}</title></head><body>"
    )
    html += body_content
    html += img_str
    html += img_str2
    html += "</body></html>"
    return html


def save_to_db(tid, thread_title, user_name, pic_num, res_type, resource, tweet_date):
    """参考 twitter.py saveToDB，数据库地址改为 192.168.2.181。"""
    try:
        db = pymysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
            database=DB_NAME, charset="utf8",
        )
    except Exception as e:
        print("数据库连接失败 (%s): %s" % (DB_HOST, e))
        return False
    cursor = db.cursor()
    cursor.execute("SET NAMES utf8")

    sql = "SELECT * FROM list WHERE tid = '%s' and bbs=%d" % (sql_escape(tid), BBS)
    try:
        cursor.execute(sql)
        results = cursor.fetchall()
        exists = len(results)
    except Exception as e:
        print("查询失败: %s" % e)
        cursor.close()
        db.close()
        return False

    if exists == 0:
        db_title = mysql_utf8_safe(thread_title)
        print("添加数据... %s %s" % (tid, user_name))
        sql = (
            "INSERT INTO list(tid, title, user, pic, date, high, bbs, datePath, res) "
            "VALUES ('%s', '%s', '%s', %d, '%s', %d, %d, '%s', '%s')"
        ) % (
            sql_escape(mysql_utf8_safe(tid)),
            sql_escape(db_title),
            sql_escape(mysql_utf8_safe(user_name)),
            pic_num,
            sql_escape(mysql_utf8_safe(tweet_date)),
            res_type,
            BBS,
            sql_escape(mysql_utf8_safe(user_name)),
            sql_escape(mysql_utf8_safe(resource)),
        )
        try:
            cursor.execute(sql)
            db.commit()
            cursor.close()
            db.close()
            return "insert"
        except Exception as e:
            print("插入失败: %s--[title:%s]" % (e, db_title))
            db.rollback()
            cursor.close()
            db.close()
            return "fail"
    else:
        print("%s 已存在于数据库" % tid)
        cursor.close()
        db.close()
        return "exist"


def copy_media(src_dir, dest_dir, media_list):
    """将媒体文件从用户下载目录复制到 tid 子目录。"""
    os.makedirs(dest_dir, exist_ok=True)
    copied = 0
    for m in media_list:
        src = os.path.join(src_dir, m["filename"])
        dst = os.path.join(dest_dir, m["filename"])
        if os.path.isfile(dst):
            copied += 1
            continue
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            copied += 1
        else:
            print("  警告: 源文件不存在 %s" % src)
    return copied


def mark_csv_parsed(import_cfg, parsed_keys):
    parsed_csv = import_cfg.setdefault("parsed_csv", {})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for key, mtime in parsed_keys:
        parsed_csv[key] = {"parsed_at": now, "mtime": mtime}


def process_user(screen_name, import_cfg, db_enabled=True):
    user_dir = os.path.join(SCRIPT_DIR, screen_name)
    if not os.path.isdir(user_dir):
        print("跳过 %s: 目录不存在" % screen_name)
        return 0, 0, 0, 0, 0, db_enabled

    tweets, parsed_keys, csv_skipped = parse_csv_files(user_dir, screen_name, import_cfg)
    if parsed_keys:
        mark_csv_parsed(import_cfg, parsed_keys)

    if not tweets:
        print("%s: 无有效推文数据 (跳过 csv %d 个)" % (screen_name, csv_skipped))
        return 0, 0, 0, 0, csv_skipped, db_enabled

    html_count = 0
    db_insert = 0
    db_exist = 0
    db_fail = 0

    for tid, data in tweets.items():
        media_list = data["media"]
        if not media_list:
            continue

        # 目录结构: twitter/{userId}/{tid}/Z{tid}.html
        dest_dir = os.path.join(TWITTER_DIR, screen_name, tid)
        save_path = os.path.join(dest_dir, "Z%s.html" % tid)

        copy_media(user_dir, dest_dir, media_list)

        title = extract_title(data["tweet_content"]) or tid
        if not os.path.exists(save_path):
            print("保存: %s" % save_path)
            html = build_html(title, data["tweet_content"], media_list)
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(html)
            html_count += 1
        else:
            print("已存在: %s" % save_path)

        pic_num = len(media_list)
        res_type = 1 if any(
            "Video" in m["type"] or m["filename"].lower().endswith(".mp4")
            for m in media_list
        ) else 0
        resource = "<!>".join(m["filename"] for m in media_list)

        # 对应 twitter.py 923 行: saveToDB(tid, title, userId, picNum, resType, userId, resource, addDate)
        if db_enabled:
            result = save_to_db(
                tid=tid,
                thread_title=title,
                user_name=screen_name,
                pic_num=pic_num,
                res_type=res_type,
                resource=resource,
                tweet_date=data["tweet_date"],
            )
            if result is False:
                db_enabled = False
            elif result == "insert":
                db_insert += 1
            elif result == "exist":
                db_exist += 1
            elif result == "fail":
                db_fail += 1

    return html_count, db_insert, db_exist, db_fail, csv_skipped, db_enabled


def main():
    os.chdir(SCRIPT_DIR)
    import_cfg = load_import_config()
    users = load_user_list()
    print("用户列表: %s" % ", ".join(users))
    print("目标目录: %s" % TWITTER_DIR)
    print("数据库: %s" % DB_HOST)
    print("导入配置: %s" % IMPORT_CONFIG_PATH)
    print("reparse_all: %s" % import_cfg.get("reparse_all", False))

    total_html = 0
    total_db_insert = 0
    total_db_exist = 0
    total_db_fail = 0
    total_csv_skipped = 0
    db_ok = True
    for user in users:
        print("\n========== 处理用户: %s ==========" % user)
        h, ins, ext, fail, skip, db_ok = process_user(user, import_cfg, db_ok)
        total_html += h
        total_db_insert += ins
        total_db_exist += ext
        total_db_fail += fail
        total_csv_skipped += skip
        print(
            "%s 完成: 新建 HTML %d, 数据库写入 %d, 数据库已存在 %d, 写入失败 %d, 跳过 csv %d"
            % (user, h, ins, ext, fail, skip)
        )

    save_import_config(import_cfg)
    print(
        "\n全部完成: 新建 HTML %d, 数据库写入 %d, 数据库已存在 %d, 写入失败 %d, 跳过 csv %d"
        % (total_html, total_db_insert, total_db_exist, total_db_fail, total_csv_skipped)
    )


if __name__ == "__main__":
    main()
