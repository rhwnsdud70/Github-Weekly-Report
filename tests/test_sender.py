import json
import httpx
from models.domain import TelegramMessage
from telegram.sender import TelegramSender


def test_three_attempts_then_continue_and_preview_off():
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body['link_preview_options'] == {'is_disabled':True}
        if body['text'].startswith('bad'):
            return httpx.Response(500,json={'ok':False})
        return httpx.Response(200,json={'ok':True})
    sender = TelegramSender('fake','chat',httpx.Client(transport=httpx.MockTransport(respond)),sleep=lambda _:None)
    results = sender.send([TelegramMessage('top','bad',[],1),TelegramMessage('top','good',[],2)])
    assert [r.attempts for r in results] == [3,1]
    assert [r.status for r in results] == ['FAILED','SUCCESS']
    assert len(calls) == 4


def test_retry_after_and_retry_success():
    sleeps, calls = [], []
    def respond(request):
        calls.append(request)
        return httpx.Response(429,json={'ok':False,'parameters':{'retry_after':4}}) if len(calls)==1 else httpx.Response(200,json={'ok':True})
    result = TelegramSender('fake','chat',httpx.Client(transport=httpx.MockTransport(respond)),sleep=sleeps.append).send([
        TelegramMessage('summary','hello',[])])
    assert result[0].status == 'RETRY_SUCCESS'
    assert sleeps == [4]
