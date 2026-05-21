# coding=utf-8
"""下载数据目录：默认项目下的 twitterData/"""

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR_NAME = 'twitterData'
SETTINGS_PATH = os.path.join(SCRIPT_DIR, 'settings.json')


def default_data_dir():
    return os.path.join(SCRIPT_DIR, DATA_DIR_NAME)


def resolve_save_path(save_path=None):
    """
    解析媒体下载根目录，末尾带路径分隔符。
    save_path 为空时使用 SCRIPT_DIR/twitterData/；相对路径相对于 SCRIPT_DIR。
    """
    raw = (save_path or '').strip()
    if raw:
        p = os.path.normpath(raw)
        if not os.path.isabs(p):
            p = os.path.join(SCRIPT_DIR, p)
    else:
        p = default_data_dir()
    os.makedirs(p, exist_ok=True)
    return p + os.sep


def load_save_path_from_settings():
    """从 settings.json 读取 save_path 并解析为下载根目录。"""
    if os.path.isfile(SETTINGS_PATH):
        with open(SETTINGS_PATH, 'r', encoding='utf8') as f:
            return resolve_save_path(json.load(f).get('save_path', ''))
    return resolve_save_path(None)
