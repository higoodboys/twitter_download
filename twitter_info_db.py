"""同步 twitterInfo 表中的用户昵称，逻辑参考 twitter/twitter.py saveUserInfoToDB。"""

import pymysql


def mysql_utf8_safe(s):
    """MySQL utf8 不支持 4 字节字符（emoji 等），写入前过滤。"""
    if not s:
        return ""
    return "".join(c for c in str(s) if ord(c) <= 0xFFFF)

DEFAULT_DB_CONFIG = {
    'host': '192.168.2.181',
    'user': 'root',
    'password': 'liuwei22',
    'database': 'p91',
    'charset': 'utf8',
}


def save_user_info_to_db(name: str, nickname: str, db_config=None) -> bool:
    """
    name: @ 后面的 screen_name
    nickname: 显示名称（legacy.name）
    不存在则插入；存在且昵称变化则更新。
    """
    if not name or not nickname:
        return False

    nickname = mysql_utf8_safe(nickname)
    cfg = {**DEFAULT_DB_CONFIG, **(db_config or {})}
    db = None
    cursor = None
    try:
        db = pymysql.connect(
            host=cfg['host'],
            user=cfg['user'],
            password=cfg['password'],
            database=cfg['database'],
            charset=cfg.get('charset', 'utf8'),
        )
        cursor = db.cursor()
        cursor.execute('SET NAMES utf8')

        cursor.execute(
            'SELECT nickname FROM twitterInfo WHERE name = %s',
            (name,),
        )
        row = cursor.fetchone()
        if row is None:
            print(f'添加用户数据... {name} {nickname}')
            cursor.execute(
                'INSERT INTO twitterInfo(name, nickname) VALUES (%s, %s)',
                (name, nickname),
            )
        elif row[0] != nickname:
            print(f'更新昵称... {name}: {row[0]} -> {nickname}')
            cursor.execute(
                'UPDATE twitterInfo SET nickname = %s WHERE name = %s',
                (nickname, name),
            )
        db.commit()
        return True
    except Exception as e:
        if db:
            db.rollback()
        print(f'同步 twitterInfo 失败 ({name}): {e}')
        return False
    finally:
        if cursor:
            cursor.close()
        if db:
            db.close()
