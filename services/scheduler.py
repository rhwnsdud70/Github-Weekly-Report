"""Fixed weekly KST boundary, independent of machine locale and DST."""
from datetime import datetime, timedelta, timezone
import logging
import time
from models.domain import ReportPeriod, instant

KST = timezone(timedelta(hours=9),'KST')


def report_period(now=None):
    local = instant(now or datetime.now(KST)).astimezone(KST)
    end = (local-timedelta(days=(local.weekday()+1)%7)).replace(hour=19,minute=0,second=0,microsecond=0)
    if end > local:
        end -= timedelta(days=7)
    return ReportPeriod(end-timedelta(days=7),end)


def next_run(now=None):
    return report_period(now).end+timedelta(days=7)


def scheduler_loop(job, clock=lambda:datetime.now(KST), sleep=time.sleep, stop=lambda:False):
    target = next_run(clock())
    logger = logging.getLogger(__name__)
    while not stop():
        now = clock()
        if now >= target:
            try:
                job(report_period(now))
            except Exception as error:
                logger.error('Scheduled report failed (%s); see automation_runs',type(error).__name__)
            target = next_run(now)
        sleep(min(30,max(0.1,(target-clock()).total_seconds())))
