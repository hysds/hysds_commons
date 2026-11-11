"""Live OCC test against the local OpenSearch. Set HYSDS_LIVE_OS=1 (and source local-stack/env.sh)."""
import os
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HYSDS_LIVE_OS") != "1", reason="set HYSDS_LIVE_OS=1")

from hysds_commons.workflow_status import (WorkflowStatusExists, create_status, get_status,
                                           update_status)


@pytest.fixture
def es_index():
    from hysds_commons.opensearch_utils import OpenSearchUtility
    util = OpenSearchUtility(os.environ.get("HYSDS_LIVE_OS_URL", "https://127.0.0.1:9200"))
    index = f"tmp_wf_status_occ_{uuid.uuid4().hex[:8]}"
    util.es.indices.create(index=index, body={"mappings": {"properties": {"counter": {"type": "long"}}}})
    yield util.es, index
    util.es.indices.delete(index=index, ignore_unavailable=True)


def test_two_concurrent_writers_no_lost_updates(es_index):
    es, index = es_index
    create_status(es, index, "run1", {"counter": 0, "status": "running"})
    with pytest.raises(WorkflowStatusExists):
        create_status(es, index, "run1", {"counter": 0})

    n, retries = 50, []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        for _ in range(n):
            calls = [0]

            def mutate(d):
                calls[0] += 1
                time.sleep(0.002)  # widen the read-write window so writers truly race
                d["counter"] += 1
                return d
            update_status(es, index, "run1", mutate, max_attempts=100, base_delay=0.001)
            with lock:
                retries.append(calls[0] - 1)

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    doc, _, _ = get_status(es, index, "run1")
    print(f"total retries (conflicts): {sum(retries)}")
    assert doc["counter"] == 2 * n
    assert sum(retries) > 0, "no conflicts occurred; test did not exercise OCC"
