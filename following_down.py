# coding=utf-8
"""
通过 GraphQL Following 接口获取指定账号的关注列表，保存到本地并下载头像。

API: GET https://x.com/i/api/graphql/2vUj-_Ek-UmBVDNtd8OnQA/Following
需登录 cookie（与 main.py 相同），先 UserByScreenName 取 userId 再分页拉取。

输出（默认在 twitter/ 目录，与 twitter.py 一致）:
  - follow.pickle   {"/screen_name": "nickname", ...}
  - following.json  完整列表
  - face/SDNMQ5.jpg 头像（400x400）
"""

import json
import os
import pickle
import re
import sys
import time
from urllib.parse import quote

import httpx

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from twitter_info_db import save_user_info_to_db, DEFAULT_DB_CONFIG
except ImportError:
    save_user_info_to_db = None
    DEFAULT_DB_CONFIG = {}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SCRIPT_DIR, 'settings.json')

FOLLOWING_QUERY_ID = '2vUj-_Ek-UmBVDNtd8OnQA'
USER_BY_SCREEN_QUERY_ID = 'xc8f1g7BYqr6VTzTbvNlGw'

FOLLOWING_FEATURES = {
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'tweetypie_unmention_optimization_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'rweb_video_timestamps_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'responsive_web_media_download_video_enabled': False,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_enhance_cards_enabled': False,
}

USER_BY_SCREEN_FEATURES = {
    'hidden_profile_likes_enabled': False,
    'hidden_profile_subscriptions_enabled': False,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'subscriptions_verification_info_verified_since_enabled': True,
    'highlights_tweets_tab_ui_enabled': True,
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True,
}


def load_settings():
    with open(SETTINGS_PATH, 'r', encoding='utf8') as f:
        s = json.load(f)
    proxy = s.get('proxy') or None
    db_config = None
    if s.get('db_sync', True) and save_user_info_to_db:
        db_config = {
            'host': s.get('db_host', DEFAULT_DB_CONFIG.get('host', '192.168.2.181')),
            'user': s.get('db_user', DEFAULT_DB_CONFIG.get('user', 'root')),
            'password': s.get('db_password', DEFAULT_DB_CONFIG.get('password', '')),
            'database': s.get('db_name', DEFAULT_DB_CONFIG.get('database', 'p91')),
            'charset': 'utf8',
        }
    return {
        'cookie': s['cookie'],
        'proxy': proxy,
        'following_screen_name': s.get('following_screen_name', 'higoodboy'),
        'following_count': int(s.get('following_count', 50)),
        'twitter_dir': s.get('twitter_dir') or os.path.join(SCRIPT_DIR, 'twitter'),
        'db_sync': bool(s.get('db_sync', True)),
        'db_config': db_config,
        'skip_existing_avatar': bool(s.get('following_skip_existing_avatar', True)),
        'update_user_lst': bool(s.get('following_update_user_lst', True)),
        'exclude_owner_from_user_lst': bool(s.get('following_exclude_owner_from_user_lst', True)),
    }


def update_settings_user_lst(screen_names: list, owner_screen_name: str, exclude_owner: bool) -> str:
    """将关注用户名写回 settings.json 的 user_lst（逗号分隔、无空格）。"""
    names = []
    seen = set()
    for sn in screen_names:
        if exclude_owner and sn.lower() == owner_screen_name.lower():
            continue
        if sn not in seen:
            seen.add(sn)
            names.append(sn)
    user_lst_str = ','.join(names)
    with open(SETTINGS_PATH, 'r', encoding='utf8') as f:
        settings = json.load(f)
    old_lst = settings.get('user_lst', '')
    settings['user_lst'] = user_lst_str
    with open(SETTINGS_PATH, 'w', encoding='utf8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)
        f.write('\n')
    print(f'已更新 settings.json user_lst: 共 {len(names)} 个用户')
    if old_lst != user_lst_str:
        old_set = {x.strip() for x in old_lst.split(',') if x.strip()}
        new_set = set(names)
        print(f'  新增 {len(new_set - old_set)}，移除 {len(old_set - new_set)}')
    return user_lst_str


def build_headers(cookie: str, referer_screen_name: str) -> dict:
    m = re.findall(r'ct0=(.*?);', cookie)
    if not m:
        raise ValueError('cookie 中缺少 ct0')
    return {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
        'cookie': cookie,
        'x-csrf-token': m[0],
        'referer': f'https://x.com/{referer_screen_name}/following',
    }


def _gql_url(operation: str, query_id: str, variables: dict, features: dict) -> str:
    var = quote(json.dumps(variables, separators=(',', ':')), safe='')
    feat = quote(json.dumps(features, separators=(',', ':')), safe='')
    return (
        f'https://x.com/i/api/graphql/{query_id}/{operation}'
        f'?variables={var}&features={feat}'
    )


def get_user_id(screen_name: str, headers: dict, proxy) -> tuple:
    variables = {
        'screen_name': screen_name,
        'withSafetyModeUserFields': False,
    }
    url = _gql_url('UserByScreenName', USER_BY_SCREEN_QUERY_ID, variables, USER_BY_SCREEN_FEATURES)
    toggles = quote(json.dumps({'withAuxiliaryUserLabels': False}, separators=(',', ':')), safe='')
    url += f'&fieldToggles={toggles}'
    resp = httpx.get(url, headers=headers, proxy=proxy, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f'UserByScreenName HTTP {resp.status_code}: {resp.text[:500]}')
    data = resp.json()
    user = data['data']['user']['result']
    legacy = user['legacy']
    return user['rest_id'], legacy['name'], legacy.get('friends_count')


def _collect_entries(obj, out: list):
    if isinstance(obj, dict):
        if 'entries' in obj and isinstance(obj['entries'], list):
            out.append(obj['entries'])
        for v in obj.values():
            _collect_entries(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_entries(item, out)


def parse_following_response(data: dict):
    """从 Following 响应解析用户列表与下一页 cursor。"""
    users = []
    next_cursor = None
    entries_lists = []
    _collect_entries(data, entries_lists)
    if not entries_lists:
        return users, next_cursor

    for entries in entries_lists:
        for item in entries:
            eid = item.get('entryId', '')
            if eid.startswith('user-'):
                results = item.get('content', {}).get('itemContent', {}).get('user_results', {})
                result = results.get('result') or {}
                if result.get('__typename') == 'UserUnavailable':
                    continue
                legacy = result.get('legacy') or {}
                screen_name = legacy.get('screen_name')
                if not screen_name:
                    continue
                avatar = legacy.get('profile_image_url_https') or ''
                users.append({
                    'screen_name': screen_name,
                    'nickname': legacy.get('name', ''),
                    'rest_id': result.get('rest_id', ''),
                    'avatar_url': avatar,
                })
            elif 'cursor-bottom' in eid:
                next_cursor = item.get('content', {}).get('value')

    return users, next_cursor


def fetch_following_page(user_id: str, count: int, cursor, headers: dict, proxy, retries=3):
    variables = {
        'userId': user_id,
        'count': count,
        'includePromotedContent': False,
    }
    if cursor:
        variables['cursor'] = cursor
    url = _gql_url('Following', FOLLOWING_QUERY_ID, variables, FOLLOWING_FEATURES)
    last_err = None
    for attempt in range(1, retries + 1):
        resp = httpx.get(url, headers=headers, proxy=proxy, timeout=30)
        text = resp.text
        if resp.status_code == 200:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                if 'Rate limit exceeded' in text:
                    last_err = 'API 次数已超限 (Rate limit exceeded)'
                else:
                    last_err = f'Following 响应非 JSON: {text[:500]}'
        else:
            last_err = f'Following HTTP {resp.status_code}: {text[:500]}'
        if attempt < retries:
            wait = 15 * attempt
            print(f'  请求失败，{wait}s 后重试 ({attempt}/{retries})...')
            time.sleep(wait)
    raise RuntimeError(last_err)


def avatar_to_hd(url: str) -> str:
    if not url:
        return url
    return re.sub(r'_normal(\.\w+)$', r'_400x400\1', url)


def save_avatar(url: str, dest_path: str, proxy, skip_existing: bool) -> bool:
    if skip_existing and os.path.exists(dest_path):
        return False
    if not url:
        return False
    hd = avatar_to_hd(url)
    try:
        r = httpx.get(hd, timeout=30, proxy=proxy)
        if r.status_code == 404 and hd != url:
            r = httpx.get(url, timeout=30, proxy=proxy)
        r.raise_for_status()
        with open(dest_path, 'wb') as f:
            f.write(r.content)
        return True
    except Exception as e:
        print(f'  头像下载失败 {dest_path}: {e}')
        return False


def fetch_all_following(screen_name: str, settings: dict) -> dict:
    headers = build_headers(settings['cookie'], screen_name)
    proxy = settings['proxy']
    count = settings['following_count']
    out_dir = settings['twitter_dir']
    face_dir = os.path.join(out_dir, 'face')
    os.makedirs(face_dir, exist_ok=True)

    print(f'获取用户 @{screen_name} 的 rest_id...')
    user_id, owner_name, friends_count = get_user_id(screen_name, headers, proxy)
    print(f'  {owner_name} (id={user_id}, following≈{friends_count})')

    following_pickle = {}
    following_list = []
    seen = set()
    cursor = None
    page = 0
    api_calls = 1

    while True:
        page += 1
        print(f'拉取关注列表第 {page} 页...')
        raw = fetch_following_page(user_id, count, cursor, headers, proxy)
        api_calls += 1
        users, next_cursor = parse_following_response(raw)
        if not users and not next_cursor:
            err = raw.get('errors') or raw
            raise RuntimeError(f'无法解析 Following 数据: {str(err)[:300]}')

        new_on_page = 0
        for u in users:
            sn = u['screen_name']
            if sn in seen:
                continue
            seen.add(sn)
            new_on_page += 1
            following_list.append(u)
            following_pickle['/' + sn] = u['nickname']

            face_path = os.path.join(face_dir, f'{sn}.jpg')
            if save_avatar(u['avatar_url'], face_path, proxy, settings['skip_existing_avatar']):
                print(f'  头像 {sn}.jpg')
            elif settings['skip_existing_avatar'] and os.path.exists(face_path):
                pass
            else:
                print(f'  跳过/失败头像 {sn}')

            if settings['db_sync'] and save_user_info_to_db and settings['db_config']:
                save_user_info_to_db(sn, u['nickname'], settings['db_config'])

        print(f'  本页 {len(users)} 人，新增 {new_on_page} 人，累计 {len(following_list)} 人')

        if not next_cursor:
            print('  无下一页 cursor，结束')
            break
        if len(users) == 0:
            print('  本页无用户数据，结束')
            break
        if new_on_page == 0:
            print('  本页无新增用户（均已存在），结束')
            break
        if friends_count and len(following_list) >= friends_count:
            print(f'  已达资料页关注数 {friends_count}，结束')
            break

        cursor = next_cursor
        time.sleep(1)

    pickle_path = os.path.join(out_dir, 'follow.pickle')
    json_path = os.path.join(out_dir, 'following.json')
    with open(pickle_path, 'wb') as f:
        pickle.dump(following_pickle, f)
    with open(json_path, 'w', encoding='utf8') as f:
        json.dump({
            'owner': screen_name,
            'owner_id': user_id,
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'count': len(following_list),
            'users': following_list,
        }, f, ensure_ascii=False, indent=2)

    print(f'\n完成: 共 {len(following_list)} 个关注')
    print(f'  {pickle_path}')
    print(f'  {json_path}')
    print(f'  头像目录 {face_dir}/')

    if settings.get('update_user_lst'):
        names = [u['screen_name'] for u in following_list]
        update_settings_user_lst(
            names,
            screen_name,
            settings.get('exclude_owner_from_user_lst', True),
        )

    print(f'  GraphQL 调用约 {api_calls} 次')
    return following_pickle


def main():
    settings = load_settings()
    screen_name = settings['following_screen_name']
    if len(sys.argv) > 1:
        screen_name = sys.argv[1].lstrip('@')
    print(f'同步 @{screen_name} 的关注列表 → {settings["twitter_dir"]}\n')
    fetch_all_following(screen_name, settings)


if __name__ == '__main__':
    main()
