from huey import crontab
from huey.contrib.djhuey import periodic_task, task
from numbas_lti.report_outcome import ReportOutcomeException
from numbas_lti.models import Attempt, ScormElement, diff_scormelements
from django.db.models import Count
from datetime import datetime
import time

@task()
def editorlink_update_cache(el):
    el.update_cache()
    el.save()

@task()
def send_attempt_completion_receipt(attempt):
    attempt.send_completion_receipt()

@task()
def resource_report_scores(resource):
    resource.report_scores()

@task()
def attempt_report_outcome(attempt):
    time.sleep(0.1)
    try:
        attempt.report_outcome()
    except ReportOutcomeException:
        pass

@periodic_task(crontab(minute='*'))
def diff_suspend_data():
    attempts = Attempt.objects.filter(diffed=False)
    MAX_TIME = 10
    start = datetime.now()
    if attempts.exists():
        for a in attempts:
            diff_scormelements(a)
            t2 = datetime.now()
            if (t2-start).total_seconds()>MAX_TIME:
                break
