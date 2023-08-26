# Copyright 2022-2023 XProbe Inc.
# derived from copyright 1999-2021 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import os
import sys
import time

import numpy as np
import pandas as pd
import pytest
import xoscar as mo

from ..... import dataframe as md
from ..... import remote as mr
from ..... import tensor as mt
from .....core import ChunkGraph, ExecutionError
from .....core.context import get_context
from .....core.graph import ChunkGraphBuilder, TileableGraph, TileableGraphBuilder
from .....core.operand import OperandStage
from .....oscar import create_actor_pool
from .....resource import Resource
from .....utils import Timer
from ....cluster import MockClusterAPI
from ....lifecycle import MockLifecycleAPI
from ....meta import MockMetaAPI, MockWorkerMetaAPI
from ....scheduling import MockSchedulingAPI
from ....session import MockSessionAPI
from ....storage import MockStorageAPI
from ....task import MapReduceInfo, new_task_id
from ....task.supervisor.manager import TaskConfigurationActor, TaskManagerActor
from ....task.task_info_collector import TaskInfoCollectorActor
from ... import Subtask, SubtaskResult, SubtaskStatus
from ...worker.manager import SubtaskRunnerManagerActor
from ...worker.runner import SubtaskRunnerActor, SubtaskRunnerRef
from ...worker.storage import RunnerStorageActor, RunnerStorageRef


class FakeTaskManager(TaskManagerActor):
    def set_subtask_result(self, subtask_result: SubtaskResult):
        return

    def get_map_reduce_info(self, task_id: str, map_reduce_id: int) -> MapReduceInfo:
        return MapReduceInfo(
            map_reduce_id=0,
            reducer_indexes=[(0, 0)],
            reducer_bands=[(self.address, "numa-0")],
        )


class MockTaskInfoCollectorActor(mo.Actor):
    def collect_task_info_enabled(self):
        return False


@pytest.fixture
async def actor_pool():
    start_method = (
        os.environ.get("POOL_START_METHOD", "forkserver")
        if sys.platform != "win32"
        else None
    )
    pool = await create_actor_pool(
        "127.0.0.1",
        n_process=3,
        labels=["main"] + ["numa-0"] * 2 + ["io"],
        subprocess_start_method=start_method,
    )

    async with pool:
        session_id = "test_session"
        # create mock APIs
        await MockClusterAPI.create(
            pool.external_address, band_to_resource={"numa-0": Resource(num_cpus=2)}
        )
        await MockSessionAPI.create(pool.external_address, session_id=session_id)
        meta_api = await MockMetaAPI.create(session_id, pool.external_address)
        await MockWorkerMetaAPI.create(session_id, pool.external_address)
        await MockLifecycleAPI.create(session_id, pool.external_address)
        storage_api = await MockStorageAPI.create(session_id, pool.external_address)
        await MockSchedulingAPI.create(session_id, pool.external_address)

        # create configuration
        await mo.create_actor(
            TaskConfigurationActor,
            dict(),
            dict(),
            uid=TaskConfigurationActor.default_uid(),
            address=pool.external_address,
        )
        await mo.create_actor(
            FakeTaskManager,
            session_id,
            uid=FakeTaskManager.gen_uid(session_id),
            address=pool.external_address,
        )
        await mo.create_actor(
            MockTaskInfoCollectorActor,
            uid=TaskInfoCollectorActor.default_uid(),
            address=pool.external_address,
        )
        manager = await mo.create_actor(
            SubtaskRunnerManagerActor,
            pool.external_address,
            None,
            uid=SubtaskRunnerManagerActor.default_uid(),
            address=pool.external_address,
        )
        try:
            yield pool, session_id, meta_api, storage_api, manager
        finally:
            await MockStorageAPI.cleanup(pool.external_address)
            await MockClusterAPI.cleanup(pool.external_address)


def _gen_subtask(t, session_id):
    graph = TileableGraph([t.data])
    next(TileableGraphBuilder(graph).build())

    chunk_graph = next(ChunkGraphBuilder(graph, fuse_enabled=False).build())
    subtask = Subtask(new_task_id(), session_id, new_task_id(), chunk_graph)

    return subtask


@pytest.mark.skip
@pytest.mark.asyncio
async def test_subtask_success(actor_pool):
    pool, session_id, meta_api, storage_api, manager = actor_pool

    a = mt.ones((10, 10), chunk_size=10)
    b = a + 1

    subtask = _gen_subtask(b, session_id)
    subtask_runner: SubtaskRunnerRef = await mo.actor_ref(
        SubtaskRunnerActor.gen_uid("numa-0", 0), address=pool.external_address
    )
    # test runner storage.
    runner_storage: RunnerStorageRef = await mo.actor_ref(
        RunnerStorageActor.gen_uid("numa-0", 0), address=pool.external_address
    )
    await runner_storage.put_data(
        key="abcd",
        data=1234,
    )
    data = await runner_storage.get_data(
        key="abcd",
    )
    assert data == 1234

    await subtask_runner.run_subtask(subtask)
    result = await subtask_runner.get_subtask_result()
    assert result.status == SubtaskStatus.succeeded

    # check storage
    expected = np.ones((10, 10)) + 1
    result_key = subtask.chunk_graph.results[0].key
    # result = await storage_api.get(result_key)
    # check runner storage
    result = await runner_storage.get_data(key=result_key)
    np.testing.assert_array_equal(expected, result)

    # check meta
    chunk_meta = await meta_api.get_chunk_meta(result_key)
    assert chunk_meta is not None
    assert chunk_meta["bands"][0] == (pool.external_address, "numa-0")
    assert chunk_meta["slot_ids"][0] == 0
    assert await subtask_runner.is_runner_free() is True


@pytest.mark.asyncio
async def test_p2p_load_input(actor_pool):
    pool, session_id, meta_api, storage_api, manager = actor_pool
    
    a = mt.ones((10, 10), chunk_size=10)
    b = a + 1

    subtask = _gen_subtask(b, session_id)
    subtask_runner: SubtaskRunnerRef = await mo.actor_ref(
        SubtaskRunnerActor.gen_uid("numa-0", 0), address=pool.external_address
    )
    runner_storage: RunnerStorageRef = await mo.actor_ref(
        RunnerStorageActor.gen_uid("numa-0", 0), address=pool.external_address
    )

    await subtask_runner.run_subtask(subtask)
    result = await subtask_runner.get_subtask_result()
    assert result.status == SubtaskStatus.succeeded

    # check storage
    expected = np.ones((10, 10)) + 1
    result_key = subtask.chunk_graph.results[0].key
    result = await storage_api.get(result_key)
    # check runner storage
    result = await runner_storage.get_data(key=result_key)
    np.testing.assert_array_equal(expected, result)

    # check meta
    chunk_meta = await meta_api.get_chunk_meta(result_key)
    assert chunk_meta is not None
    assert chunk_meta["bands"][0] == (pool.external_address, "numa-0")
    assert chunk_meta["slot_ids"][0] == 0
    assert await subtask_runner.is_runner_free() is True
    pass


@pytest.mark.skip
@pytest.mark.asyncio
async def test_shuffle_subtask(actor_pool):
    pool, session_id, meta_api, storage_api, manager = actor_pool

    pdf = pd.DataFrame({"f1": ["a", "b", "a"], "f2": [1, 2, 3]})
    df = md.DataFrame(pdf)
    result = df.groupby("f1").sum(method="shuffle")

    graph = TileableGraph([result.data])
    next(TileableGraphBuilder(graph).build())
    chunk_graph = next(ChunkGraphBuilder(graph, fuse_enabled=False).build())
    result_chunks = []
    new_chunk_graph = ChunkGraph(result_chunks)
    chunk_graph_iter = chunk_graph.topological_iter()
    curr = None
    for _ in range(3):
        prev = curr
        curr = next(chunk_graph_iter)
        new_chunk_graph.add_node(curr)
        if prev is not None:
            new_chunk_graph.add_edge(prev, curr)
    assert curr.op.stage == OperandStage.map
    curr.op.extra_params = {"analyzer_map_reduce_id": 0}
    result_chunks.append(curr)
    subtask = Subtask(new_task_id(), session_id, new_task_id(), new_chunk_graph)
    subtask_runner: SubtaskRunnerRef = await mo.actor_ref(
        SubtaskRunnerActor.gen_uid("numa-0", 0), address=pool.external_address
    )
    runner_storage: RunnerStorageRef = await mo.actor_ref(
        RunnerStorageActor.gen_uid("numa-0", 0), address=pool.external_address
    )
    await subtask_runner.run_subtask(subtask)
    result = await subtask_runner.get_subtask_result()
    assert result.status == SubtaskStatus.succeeded


@pytest.mark.skip
@pytest.mark.asyncio
async def test_subtask_failure(actor_pool):
    pool, session_id, meta_api, storage_api, manager = actor_pool

    # test execution error
    with mt.errstate(divide="raise"):
        a = mt.ones((10, 10), chunk_size=10)
        c = a / 0

    subtask = _gen_subtask(c, session_id)
    subtask_runner: SubtaskRunnerRef = await mo.actor_ref(
        SubtaskRunnerActor.gen_uid("numa-0", 0), address=pool.external_address
    )
    runner_storage: RunnerStorageRef = await mo.actor_ref(
        RunnerStorageActor.gen_uid("numa-0", 0), address=pool.external_address
    )
    with pytest.raises(ExecutionError) as ex_info:
        await subtask_runner.run_subtask(subtask)
    assert isinstance(ex_info.value.nested_error, FloatingPointError)
    result = await subtask_runner.get_subtask_result()
    assert result.status == SubtaskStatus.errored
    assert isinstance(result.error, FloatingPointError)
    assert await subtask_runner.is_runner_free() is True


@pytest.mark.skip
@pytest.mark.asyncio
async def test_cancel_subtask(actor_pool):
    pool, session_id, meta_api, storage_api, manager = actor_pool
    subtask_runner: SubtaskRunnerRef = await mo.actor_ref(
        SubtaskRunnerActor.gen_uid("numa-0", 0), address=pool.external_address
    )
    runner_storage: RunnerStorageRef = await mo.actor_ref(
        RunnerStorageActor.gen_uid("numa-0", 0), address=pool.external_address
    )

    def sleep(timeout: int):
        time.sleep(timeout)
        return timeout

    b = mr.spawn(sleep, 100)

    subtask = _gen_subtask(b, session_id)
    asyncio.create_task(subtask_runner.run_subtask(subtask))
    await asyncio.sleep(0.2)
    with Timer() as timer:
        # normal cancel by cancel asyncio Task
        aio_task = asyncio.create_task(
            asyncio.wait_for(asyncio.shield(subtask_runner.cancel_subtask()), timeout=1)
        )
        assert await subtask_runner.is_runner_free() is False
        with pytest.raises(asyncio.TimeoutError):
            await aio_task
    # need 1 sec to reach timeout, then killing actor and wait for auto recovering
    # the time would not be over 5 sec
    assert timer.duration < 5

    async def wait_slot_restore():
        while True:
            try:
                assert await subtask_runner.is_runner_free() is True
            except (mo.ServerClosed, ConnectionRefusedError, mo.ActorNotExist):
                await asyncio.sleep(0.5)
            else:
                break

    await mo.kill_actor(subtask_runner)
    await wait_slot_restore()

    a = mr.spawn(sleep, 2)

    subtask2 = _gen_subtask(a, session_id)
    asyncio.create_task(subtask_runner.run_subtask(subtask2))
    await asyncio.sleep(0.2)
    with Timer() as timer:
        # normal cancel by cancel asyncio Task
        await asyncio.wait_for(subtask_runner.cancel_subtask(), timeout=6)
    # do not need to wait 10 sec
    assert timer.duration < 10
    assert await subtask_runner.is_runner_free() is True


@pytest.mark.skip
@pytest.mark.asyncio
async def test_subtask_op_progress(actor_pool):
    pool, session_id, meta_api, storage_api, manager = actor_pool
    subtask_runner: SubtaskRunnerRef = await mo.actor_ref(
        SubtaskRunnerActor.gen_uid("numa-0", 0), address=pool.external_address
    )
    runner_storage: RunnerStorageRef = await mo.actor_ref(
        RunnerStorageActor.gen_uid("numa-0", 0), address=pool.external_address
    )

    def progress_sleep(interval: float, count: int):
        for idx in range(count):
            time.sleep(interval)
            get_context().set_progress((1 + idx) * 1.0 / count)

    b = mr.spawn(progress_sleep, args=(0.75, 2))

    subtask = _gen_subtask(b, session_id)
    aio_task = asyncio.create_task(subtask_runner.run_subtask(subtask))
    try:
        await asyncio.sleep(0.5)
        result = await subtask_runner.get_subtask_result()
        assert result.progress == 0.0

        await asyncio.sleep(0.75)
        result = await subtask_runner.get_subtask_result()
        assert result.progress == 0.5
    finally:
        await aio_task

    result = await subtask_runner.get_subtask_result()
    assert result.progress == 1.0


@pytest.mark.skip
def test_update_subtask_result():
    subtask_result = SubtaskResult(
        subtask_id="test_subtask_abc",
        status=SubtaskStatus.pending,
        progress=0.0,
        bands=[("127.0.0.1", "numa-0")],
    )
    new_result = SubtaskResult(
        subtask_id="test_subtask_abc",
        status=SubtaskStatus.succeeded,
        progress=1.0,
        bands=[("127.0.0.1", "numa-0")],
        execution_start_time=1646125099.622051,
        execution_end_time=1646125104.448726,
    )
    subtask_result.update(new_result)
    assert subtask_result.execution_start_time == new_result.execution_start_time
    assert subtask_result.execution_end_time == new_result.execution_end_time
