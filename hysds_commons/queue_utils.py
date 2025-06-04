from future import standard_library
standard_library.install_aliases()

import os
import requests

from hysds_commons.log_utils import logger
from hysds.celery import app


HYSDS_QUEUES = (
    app.conf['JOBS_PROCESSED_QUEUE'],
    app.conf['USER_RULES_JOB_QUEUE'],
    app.conf['DATASET_PROCESSED_QUEUE'],
    app.conf['USER_RULES_DATASET_QUEUE'],
    app.conf['USER_RULES_TRIGGER_QUEUE'],
    app.conf['ON_DEMAND_DATASET_QUEUE'],
    app.conf['ON_DEMAND_JOB_QUEUE'],
    app.conf['PROCESS_EVENTS_TASKS_QUEUE'],
)


def get_all_queues(rabbitmq_admin_url):
    """
    List the queues available for job-running
    Note: does not return celery internal queues
    @param rabbitmq_admin_url: RabbitMQ admin URL
    @return: list of queues
    """

    try:
        endpoint = os.path.join(rabbitmq_admin_url, "api/queues")

        req = requests.get(endpoint)
        req.raise_for_status()

        data = req.json()

    except requests.HTTPError as e:
        if e.response.status_code == 401:
            logger.error(f"Failed to authenticate {rabbitmq_admin_url}. Ensure credentials are set in .netrc")
        raise Exception(e)
    return [obj["name"] for obj in data if not obj["name"].startswith("celery") and obj["name"] not in HYSDS_QUEUES]
