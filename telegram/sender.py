from dataclasses import dataclass
import time
import httpx
from config import rules


@dataclass
class TransmissionResult:
    kind: str
    rank: int | None
    part: int
    attempts: int
    status: str
    error: str | None = None


class TelegramSender:
    def __init__(self, token, chat_id, client=None, sleep=time.sleep, log=None):
        self.token, self.chat_id = token, chat_id
        self.client = client or httpx.Client(timeout=30)
        self.sleep, self.log = sleep, log

    def send(self, messages):
        results = []
        for index, message in enumerate(messages):
            if index:
                self.sleep(rules.TELEGRAM_DELAY)
            for attempt in range(1, rules.TELEGRAM_ATTEMPTS + 1):
                delay = rules.TELEGRAM_DELAY
                error = None
                try:
                    response = self.client.post(f'https://api.telegram.org/bot{self.token}/sendMessage',
                        json={'chat_id':self.chat_id,'text':message.text,
                              'link_preview_options':{'is_disabled':True}})
                    data = response.json()
                    success = response.status_code == 200 and data.get('ok') is True
                    if not success:
                        # Telegram error descriptions may echo input; log only stable error codes.
                        error = f'Telegram HTTP {response.status_code}, API code {data.get("error_code", "unknown")}'
                        retry_after = data.get('parameters', {}).get('retry_after')
                        if isinstance(retry_after, (int,float)) and retry_after > 0:
                            delay = retry_after
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    success, error = False, 'Telegram transport or response failure'
                status = ('SUCCESS' if attempt == 1 else 'RETRY_SUCCESS') if success else (
                    'FAILED' if attempt == rules.TELEGRAM_ATTEMPTS else 'RETRY')
                if self.log:
                    self.log(message, attempt, status, error)
                if success or attempt == rules.TELEGRAM_ATTEMPTS:
                    results.append(TransmissionResult(message.kind,message.rank,message.part,attempt,status,error))
                    break
                self.sleep(delay)
        return results
