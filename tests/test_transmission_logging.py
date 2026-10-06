import httpx
from database.db import Database
from database.transmission_log_store import transmission_logger, transmission_status
from models.domain import TelegramMessage
from telegram.sender import TelegramSender


def test_persist_every_attempt_partial_success():
    db = Database(':memory:')
    run = db.start_run('start','end','test')
    count = []
    def respond(request):
        count.append(request)
        return httpx.Response(500,json={'ok':False}) if len(count)<=3 else httpx.Response(200,json={'ok':True})
    sender = TelegramSender('fake','chat',httpx.Client(transport=httpx.MockTransport(respond)),
        sleep=lambda _:None,log=transmission_logger(db,run))
    result = sender.send([TelegramMessage('top','a',[],1),TelegramMessage('top','b',[],2)])
    assert transmission_status(result) == 'PARTIAL_SUCCESS'
    rows = db.rows('SELECT * FROM telegram_logs ORDER BY id')
    assert [r['status'] for r in rows] == ['RETRY','RETRY','FAILED','SUCCESS']
    assert rows[2]['attempt_count'] == 3
    assert all(row['sent_time'] and row['run_id']==run for row in rows)
