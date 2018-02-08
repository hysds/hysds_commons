import os
from requests import HTTPError

from hysds_commons.request_utils import get_requests_json_response
from hysds_commons.log_utils import logger

from hysds.celery import app


HYSDS_QUEUES = (
    app.conf['JOBS_PROCESSED_QUEUE'],
    app.conf['USER_RULES_JOB_QUEUE'],
    app.conf['DATASET_PROCESSED_QUEUE'],
    app.conf['USER_RULES_DATASET_QUEUE'],
    app.conf['USER_RULES_TRIGGER_QUEUE'],
)


def get_all_queues(rabbitmq_admin_url):
    '''
    List the queues available for job-running
    Note: does not return celery internal queues
    @param rabbitmq_admin_url: RabbitMQ admin URL
    @return: list of queues
    '''

    try: data = get_requests_json_response(os.path.join(rabbitmq_admin_url, "api/queues"))
    except HTTPError, e:
        if e.response.status_code == 401:
            logger.error("Failed to authenticate to {}. Ensure credentials are set in .netrc.".format(rabbitmq_admin_url)) 
        raise
    return [ obj["name"] for obj in data if not obj["name"].startswith("celery") and obj["name"] not in HYSDS_QUEUES ]