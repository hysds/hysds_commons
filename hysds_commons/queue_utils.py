from future import standard_library
standard_library.install_aliases()

import os
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.ssl_ import create_urllib3_context
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

class CustomCipherAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ssl_context = create_urllib3_context(ciphers=app.conf.get("broker_use_ssl", {}).get("ciphers"))
        kwargs["ssl_context"] = ssl_context
        return super(CustomCipherAdapter, self).init_poolmanager(*args, **kwargs)

def get_all_queues(rabbitmq_admin_url):
    """
    List the queues available for job-running
    Note: does not return celery internal queues
    @param rabbitmq_admin_url: RabbitMQ admin URL
    @return: list of queues
    """

    try:
        session = requests.Session()
        session.mount("https://", CustomCipherAdapter())

        endpoint = os.path.join(rabbitmq_admin_url, "api/queues")

        req = session.get(endpoint, verify=False)
        req.raise_for_status()

        data = req.json()

    except requests.HTTPError as e:
        if e.response.status_code == 401:
            logger.error(f"Failed to authenticate {rabbitmq_admin_url}. Ensure credentials are set in .netrc")
        raise Exception(e)
    return [obj["name"] for obj in data if not obj["name"].startswith("celery") and obj["name"] not in HYSDS_QUEUES]
