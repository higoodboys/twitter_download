import os
import json
from datetime import datetime


class SyncState:
    """按用户记录 media_count / statuses_count，用于判断是否需拉取时间线。"""

    def __init__(self, save_path: str) -> None:
        self.path = save_path + 'sync_state.json'
        self.data = {}
        if os.path.exists(self.path):
            with open(self.path, 'r', encoding='utf8') as f:
                self.data = json.load(f)

    def _save(self) -> None:
        with open(self.path, 'w', encoding='utf8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get(self, screen_name: str):
        return self.data.get(screen_name)

    def should_fetch(self, screen_name: str, media_count: int, statuses_count: int, ignore_check: bool):
        """
        返回 (是否拉取时间线, 原因)。
        原因: ignore_check | first_run | increased | unchanged | decreased
        """
        if ignore_check:
            return True, 'ignore_count_sync'

        prev = self.data.get(screen_name)
        if prev is None:
            return True, 'first_run'

        prev_media = prev.get('media_count', 0)
        prev_statuses = prev.get('statuses_count', 0)

        if media_count > prev_media or statuses_count > prev_statuses:
            return True, 'increased'
        if media_count < prev_media or statuses_count < prev_statuses:
            return False, 'decreased'
        return False, 'unchanged'

    def update(self, screen_name: str, media_count: int, statuses_count: int) -> None:
        self.data[screen_name] = {
            'media_count': media_count,
            'statuses_count': statuses_count,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        self._save()
