from dataclasses import dataclass, field
from datetime import timedelta, timezone
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    github_token: str = field(default='', repr=False)
    openai_api_key: str = field(default='', repr=False)
    telegram_bot_token: str = field(default='', repr=False)
    telegram_chat_id: str = field(default='', repr=False)
    database_path: str = 'data/github_weekly_report.db'
    timezone: str = 'Asia/Seoul'
    openai_model: str = 'gpt-4.1-mini'

    @property
    def tz(self):
        # KST has no DST; works on Windows without an OS timezone database.
        return timezone(timedelta(hours=9), 'KST') if self.timezone == 'Asia/Seoul' else ZoneInfo(self.timezone)

    def validate_live(self, send=False):
        required = ['github_token', 'openai_api_key']
        if send:
            required += ['telegram_bot_token', 'telegram_chat_id']
        missing = [key.upper() for key in required if not getattr(self, key)]
        if missing:
            raise ValueError('Missing settings: ' + ', '.join(missing))

    @classmethod
    def load(cls, env_file: str | Path = '.env', environ=None):
        values = {**dotenv_values(env_file), **(os.environ if environ is None else environ)}
        settings = cls(**{key: values[key.upper()] for key in cls.__dataclass_fields__
                          if values.get(key.upper()) is not None})
        settings.tz
        if not settings.database_path:
            raise ValueError('DATABASE_PATH must not be empty')
        return settings
