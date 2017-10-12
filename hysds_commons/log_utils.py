import logging


log_format = "[%(asctime)s: %(levelname)s %(filename)s:%(funcName)s] %(message)s"
logging.basicConfig(format=log_format, level=logging.INFO)
logger = logging.getLogger('hysds_commons')


def get_logger(): return logger
