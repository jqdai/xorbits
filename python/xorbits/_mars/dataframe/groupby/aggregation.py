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

import functools
import itertools
import logging
import uuid
from typing import Callable, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from ... import opcodes as OperandDef
from ...config import options
from ...core import ENTITY_TYPE, OutputType
from ...core.context import get_context
from ...core.custom_log import redirect_custom_log
from ...core.operand import OperandStage
from ...serialization.serializables import (
    AnyField,
    DictField,
    Int32Field,
    Int64Field,
    ListField,
    StringField,
)
from ...typing import ChunkType, TileableType
from ...utils import (
    enter_current_session,
    estimate_pandas_size,
    lazy_import,
    pd_release_version,
)
from ..core import GROUPBY_TYPE
from ..merge import DataFrameConcat
from ..operands import DataFrameOperand, DataFrameOperandMixin, DataFrameShuffleProxy
from ..reduction.aggregation import is_funcs_aggregate, normalize_reduction_funcs
from ..reduction.core import ReductionAggStep, ReductionCompiler, ReductionSteps
from ..utils import (
    PD_VERSION_GREATER_THAN_2_10,
    build_concatenated_rows_frame,
    concat_on_columns,
    is_cudf,
    parse_index,
)
from .core import DataFrameGroupByOperand
from .custom_aggregation import custom_agg_functions
from .sort import (
    DataFrameGroupbyConcatPivot,
    DataFrameGroupbySortShuffle,
    DataFramePSRSGroupbySample,
)

cp = lazy_import("cupy", rename="cp")
cudf = lazy_import("cudf")

logger = logging.getLogger(__name__)
CV_THRESHOLD = 0.2
MEAN_RATIO_THRESHOLD = 2 / 3
_support_get_group_without_as_index = pd_release_version[:2] > (1, 0)


class SizeRecorder:
    def __init__(self):
        self._raw_records = []
        self._agg_records = []

    def record(self, raw_record: int, agg_record: int):
        self._raw_records.append(raw_record)
        self._agg_records.append(agg_record)

    def get(self):
        return self._raw_records, self._agg_records


_agg_functions = {
    "sum": lambda x: x.sum(),
    "prod": lambda x: x.prod(),
    "product": lambda x: x.product(),
    "min": lambda x: x.min(),
    "max": lambda x: x.max(),
    "all": lambda x: x.all(),
    "any": lambda x: x.any(),
    "count": lambda x: x.count(),
    "size": lambda x: x._reduction_size(),
    "mean": lambda x: x.mean(),
    "var": lambda x, ddof=1: x.var(ddof=ddof),
    "std": lambda x, ddof=1: x.std(ddof=ddof),
    "sem": lambda x, ddof=1: x.sem(ddof=ddof),
    "skew": lambda x, bias=False: x.skew(bias=bias),
    "kurt": lambda x, bias=False: x.kurt(bias=bias),
    "kurtosis": lambda x, bias=False: x.kurtosis(bias=bias),
    "nunique": lambda x: x.reduction_nunique(),
}
_series_col_name = "col_name"


def _patch_groupby_kurt():
    try:
        from pandas.core.groupby import DataFrameGroupBy, SeriesGroupBy

        if not hasattr(DataFrameGroupBy, "kurt"):  # pragma: no branch

            def _kurt_by_frame(a, *args, **kwargs):
                data = a.to_frame().kurt(*args, **kwargs).iloc[0]
                if is_cudf(data):  # pragma: no cover
                    data = data.copy()
                return data

            def _group_kurt(x, *args, **kwargs):
                if kwargs.get("numeric_only") is not None:
                    return x.agg(functools.partial(_kurt_by_frame, *args, **kwargs))
                else:
                    return x.agg(functools.partial(pd.Series.kurt, *args, **kwargs))

            DataFrameGroupBy.kurt = DataFrameGroupBy.kurtosis = _group_kurt
            SeriesGroupBy.kurt = SeriesGroupBy.kurtosis = _group_kurt
    except (AttributeError, ImportError):  # pragma: no cover
        pass


_patch_groupby_kurt()
del _patch_groupby_kurt


def build_mock_agg_result(
    groupby: GROUPBY_TYPE,
    groupby_params: Dict,
    raw_func: Callable,
    **raw_func_kw,
):
    try:
        agg_result = groupby.op.build_mock_groupby().aggregate(raw_func, **raw_func_kw)
    except ValueError:
        if (
            groupby_params.get("as_index") or _support_get_group_without_as_index
        ):  # pragma: no cover
            raise
        agg_result = (
            groupby.op.build_mock_groupby(as_index=True)
            .aggregate(raw_func, **raw_func_kw)
            .to_frame()
        )
        agg_result.index.names = [None] * agg_result.index.nlevels
    return agg_result


class DataFrameGroupByAgg(DataFrameOperand, DataFrameOperandMixin):
    _op_type_ = OperandDef.GROUPBY_AGG

    raw_func = AnyField("raw_func")
    raw_func_kw = DictField("raw_func_kw")
    func = AnyField("func")
    func_rename = ListField("func_rename")

    raw_groupby_params = DictField("raw_groupby_params")
    groupby_params = DictField("groupby_params")

    method = StringField("method")

    # for chunk
    combine_size = Int32Field("combine_size")
    chunk_store_limit = Int64Field("chunk_store_limit")
    pre_funcs = ListField("pre_funcs")
    agg_funcs = ListField("agg_funcs")
    post_funcs = ListField("post_funcs")
    index_levels = Int32Field("index_levels")
    size_recorder_name = StringField("size_recorder_name")

    def _set_inputs(self, inputs):
        super()._set_inputs(inputs)
        inputs_iter = iter(self._inputs[1:])
        if len(self._inputs) > 1:
            by = []
            for v in self.groupby_params["by"]:
                if isinstance(v, ENTITY_TYPE):
                    by.append(next(inputs_iter))
                else:
                    by.append(v)
            self.groupby_params["by"] = by

    def _get_inputs(self, inputs):
        if isinstance(self.groupby_params["by"], list):
            for v in self.groupby_params["by"]:
                if isinstance(v, ENTITY_TYPE):
                    inputs.append(v)
        return inputs

    def _get_index_levels(self, groupby, mock_index):
        if not self.groupby_params["as_index"]:
            try:
                as_index_agg_df = groupby.op.build_mock_groupby(
                    as_index=True
                ).aggregate(self.raw_func, **self.raw_func_kw)
            except:  # noqa: E722  # nosec  # pylint: disable=bare-except
                # handling cases like mdf.groupby("b", as_index=False).b.agg({"c": "count"})
                if isinstance(self.groupby_params["by"], list):
                    return len(self.groupby_params["by"])
                raise  # pragma: no cover
            pd_index = as_index_agg_df.index
        else:
            pd_index = mock_index
        return 1 if not isinstance(pd_index, pd.MultiIndex) else len(pd_index.levels)

    def _fix_as_index(self, result_index: pd.Index):
        # make sure if as_index=False takes effect
        if isinstance(result_index, pd.MultiIndex):
            # if MultiIndex, as_index=False definitely takes no effect
            self.groupby_params["as_index"] = True
        elif result_index.name is not None:
            # if not MultiIndex and agg_df.index has a name
            # means as_index=False takes no effect
            self.groupby_params["as_index"] = True

    def _call_dataframe(self, groupby, input_df):
        agg_df = build_mock_agg_result(
            groupby, self.groupby_params, self.raw_func, **self.raw_func_kw
        )

        shape = (np.nan, agg_df.shape[1])
        if isinstance(agg_df.index, pd.RangeIndex):
            index_value = parse_index(
                pd.RangeIndex(-1), groupby.key, groupby.index_value.key
            )
        else:
            index_value = parse_index(
                agg_df.index, groupby.key, groupby.index_value.key
            )

        # make sure if as_index=False takes effect
        self._fix_as_index(agg_df.index)

        # determine num of indices to group in intermediate steps
        self.index_levels = self._get_index_levels(groupby, agg_df.index)

        inputs = self._get_inputs([input_df])
        return self.new_dataframe(
            inputs,
            shape=shape,
            dtypes=agg_df.dtypes,
            index_value=index_value,
            columns_value=parse_index(agg_df.columns, store_data=True),
        )

    def _call_series(self, groupby, in_series):
        agg_result = build_mock_agg_result(
            groupby, self.groupby_params, self.raw_func, **self.raw_func_kw
        )

        # make sure if as_index=False takes effect
        self._fix_as_index(agg_result.index)

        index_value = parse_index(
            agg_result.index, groupby.key, groupby.index_value.key
        )

        inputs = self._get_inputs([in_series])

        # determine num of indices to group in intermediate steps
        self.index_levels = self._get_index_levels(groupby, agg_result.index)

        # update value type
        if isinstance(agg_result, pd.DataFrame):
            return self.new_dataframe(
                inputs,
                shape=(np.nan, len(agg_result.columns)),
                dtypes=agg_result.dtypes,
                index_value=index_value,
                columns_value=parse_index(agg_result.columns, store_data=True),
            )
        else:
            return self.new_series(
                inputs,
                shape=(np.nan,),
                dtype=agg_result.dtype,
                name=agg_result.name,
                index_value=index_value,
            )

    def __call__(self, groupby):
        normalize_reduction_funcs(self, ndim=groupby.ndim)
        df = groupby
        while df.op.output_types[0] not in (OutputType.dataframe, OutputType.series):
            df = df.inputs[0]

        if self.raw_func == "size":
            self.output_types = [OutputType.series]
        else:
            self.output_types = (
                [OutputType.dataframe]
                if groupby.op.output_types[0] == OutputType.dataframe_groupby
                else [OutputType.series]
            )

        if self.output_types[0] == OutputType.dataframe:
            return self._call_dataframe(groupby, df)
        else:
            return self._call_series(groupby, df)

    @classmethod
    def partition_merge_data(
        cls,
        op: "DataFrameGroupByAgg",
        partition_chunks: List[ChunkType],
        proxy_chunk: ChunkType,
    ):
        # stage 4: all *ith* classes are gathered and merged
        partition_sort_chunks = []
        properties = dict(by=op.groupby_params["by"], gpu=op.is_gpu())
        out_df = op.outputs[0]

        for i, partition_chunk in enumerate(partition_chunks):
            output_types = (
                [OutputType.dataframe_groupby]
                if out_df.ndim == 2
                else [OutputType.series_groupby]
            )
            partition_shuffle_reduce = DataFrameGroupbySortShuffle(
                stage=OperandStage.reduce,
                reducer_index=(i, 0),
                n_reducers=len(partition_chunks),
                output_types=output_types,
                **properties,
            )
            chunk_shape = list(partition_chunk.shape)
            chunk_shape[0] = np.nan

            kw = dict(
                shape=tuple(chunk_shape),
                index=partition_chunk.index,
                index_value=partition_chunk.index_value,
            )
            if op.outputs[0].ndim == 2:
                kw.update(
                    dict(
                        columns_value=partition_chunk.columns_value,
                        dtypes=partition_chunk.dtypes,
                    )
                )
            else:
                kw.update(dict(dtype=partition_chunk.dtype, name=partition_chunk.name))
            cs = partition_shuffle_reduce.new_chunks([proxy_chunk], **kw)
            partition_sort_chunks.append(cs[0])
        return partition_sort_chunks

    @classmethod
    def partition_local_data(
        cls,
        op: "DataFrameGroupByAgg",
        sorted_chunks: List[ChunkType],
        concat_pivot_chunk: ChunkType,
        in_df: TileableType,
    ):
        out_df = op.outputs[0]
        map_chunks = []
        chunk_shape = (in_df.chunk_shape[0], 1)
        for chunk in sorted_chunks:
            chunk_inputs = [chunk, concat_pivot_chunk]
            output_types = (
                [OutputType.dataframe_groupby]
                if out_df.ndim == 2
                else [OutputType.series_groupby]
            )
            map_chunk_op = DataFrameGroupbySortShuffle(
                shuffle_size=chunk_shape[0],
                stage=OperandStage.map,
                n_partition=len(sorted_chunks),
                output_types=output_types,
            )
            kw = dict()
            if out_df.ndim == 2:
                kw.update(
                    dict(
                        columns_value=chunk_inputs[0].columns_value,
                        dtypes=chunk_inputs[0].dtypes,
                    )
                )
            else:
                kw.update(dict(dtype=chunk_inputs[0].dtype, name=chunk_inputs[0].name))

            map_chunks.append(
                map_chunk_op.new_chunk(
                    chunk_inputs,
                    shape=chunk_shape,
                    index=chunk.index,
                    index_value=chunk_inputs[0].index_value,
                    # **kw
                )
            )

        return map_chunks

    @classmethod
    def _gen_shuffle_chunks_with_pivot(
        cls,
        op: "DataFrameGroupByAgg",
        in_df: TileableType,
        chunks: List[ChunkType],
        pivot: ChunkType,
    ):
        map_chunks = cls.partition_local_data(op, chunks, pivot, in_df)

        proxy_chunk = DataFrameShuffleProxy(
            output_types=[OutputType.dataframe]
        ).new_chunk(map_chunks, shape=())

        partition_sort_chunks = cls.partition_merge_data(op, map_chunks, proxy_chunk)

        return partition_sort_chunks

    @classmethod
    def _gen_shuffle_chunks(cls, op, chunks):
        # generate map chunks
        map_chunks = []
        chunk_shape = (len(chunks), 1)
        for chunk in chunks:
            # no longer consider as_index=False for the intermediate phases,
            # will do reset_index at last if so
            map_op = DataFrameGroupByOperand(
                stage=OperandStage.map,
                shuffle_size=chunk_shape[0],
                output_types=[OutputType.dataframe_groupby],
            )
            map_chunks.append(
                map_op.new_chunk(
                    [chunk],
                    shape=(np.nan, np.nan),
                    index=chunk.index,
                    index_value=op.outputs[0].index_value,
                )
            )

        proxy_chunk = DataFrameShuffleProxy(
            output_types=[OutputType.dataframe]
        ).new_chunk(map_chunks, shape=())

        # generate reduce chunks
        reduce_chunks = []
        out_indices = list(itertools.product(*(range(s) for s in chunk_shape)))
        for out_idx in out_indices:
            reduce_op = DataFrameGroupByOperand(
                stage=OperandStage.reduce,
                output_types=[OutputType.dataframe_groupby],
                n_reducers=len(out_indices),
            )
            reduce_chunks.append(
                reduce_op.new_chunk(
                    [proxy_chunk],
                    shape=(np.nan, np.nan),
                    index=out_idx,
                    index_value=None,
                )
            )
        return reduce_chunks

    @classmethod
    def _gen_map_chunks(
        cls,
        op: "DataFrameGroupByAgg",
        in_chunks: List[ChunkType],
        out_df: TileableType,
        func_infos: ReductionSteps,
    ):
        map_chunks = []
        for chunk in in_chunks:
            chunk_inputs = [chunk]
            map_op = op.copy().reset_key()
            # force as_index=True for map phase
            map_op.output_types = op.output_types
            map_op.groupby_params = map_op.groupby_params.copy()
            map_op.groupby_params["as_index"] = True
            if isinstance(map_op.groupby_params["by"], list):
                by = []
                for v in map_op.groupby_params["by"]:
                    if isinstance(v, ENTITY_TYPE):
                        by_chunk = v.cix[chunk.index[0],]
                        chunk_inputs.append(by_chunk)
                        by.append(by_chunk)
                    else:
                        by.append(v)
                map_op.groupby_params["by"] = by
            map_op.stage = OperandStage.map
            map_op.pre_funcs = func_infos.pre_funcs
            map_op.agg_funcs = func_infos.agg_funcs
            new_index = chunk.index if len(chunk.index) == 2 else (chunk.index[0],)
            if out_df.ndim == 2:
                new_index = (new_index[0], 0) if len(new_index) == 1 else new_index
                map_chunk = map_op.new_chunk(
                    chunk_inputs,
                    shape=out_df.shape,
                    index=new_index,
                    index_value=out_df.index_value,
                    columns_value=out_df.columns_value,
                    dtypes=out_df.dtypes,
                )
            else:
                new_index = new_index[:1] if len(new_index) == 2 else new_index
                map_chunk = map_op.new_chunk(
                    chunk_inputs,
                    shape=(out_df.shape[0],),
                    index=new_index,
                    index_value=out_df.index_value,
                    dtype=out_df.dtype,
                )
            map_chunks.append(map_chunk)
        return map_chunks

    @classmethod
    def _compile_funcs(cls, op: "DataFrameGroupByAgg", in_df) -> ReductionSteps:
        compiler = ReductionCompiler(store_source=True)
        if isinstance(op.func, list):
            func_iter = ((None, f) for f in op.func)
        else:
            func_iter = ((col, f) for col, funcs in op.func.items() for f in funcs)

        func_renames = (
            op.func_rename
            if getattr(op, "func_rename", None) is not None
            else itertools.repeat(None)
        )
        for func_rename, (col, f) in zip(func_renames, func_iter):
            func_name = None
            if isinstance(f, str):
                f, func_name = _agg_functions[f], f
            if func_rename is not None:
                func_name = func_rename

            func_cols = None
            if col is not None:
                func_cols = [col]
            compiler.add_function(f, in_df.ndim, cols=func_cols, func_name=func_name)
        return compiler.compile()

    @classmethod
    def _tile_with_shuffle(
        cls,
        op: "DataFrameGroupByAgg",
        in_df: TileableType,
        out_df: TileableType,
        func_infos: ReductionSteps,
    ):
        # First, perform groupby and aggregation on each chunk.
        agg_chunks = cls._gen_map_chunks(op, in_df.chunks, out_df, func_infos)
        return cls._perform_shuffle(op, agg_chunks, in_df, out_df, func_infos)

    @classmethod
    def _gen_pivot_chunk(
        cls,
        op: "DataFrameGroupByAgg",
        sample_chunks: List[ChunkType],
        agg_chunk_len: int,
    ):
        properties = dict(
            by=op.groupby_params["by"],
            gpu=op.is_gpu(),
        )

        # stage 2: gather and merge samples, choose and broadcast p-1 pivots
        kind = "quicksort"
        output_types = [OutputType.tensor]

        concat_pivot_op = DataFrameGroupbyConcatPivot(
            kind=kind,
            n_partition=agg_chunk_len,
            output_types=output_types,
            **properties,
        )

        concat_pivot_chunk = concat_pivot_op.new_chunk(
            sample_chunks,
            shape=(agg_chunk_len,),
            dtype=np.dtype(object),
        )
        return concat_pivot_chunk

    @classmethod
    def _sample_chunks(
        cls,
        op: "DataFrameGroupByAgg",
        agg_chunks: List[ChunkType],
    ):
        chunk_shape = len(agg_chunks)
        sampled_chunks = []

        properties = dict(
            by=op.groupby_params["by"],
            gpu=op.is_gpu(),
        )

        for i, chunk in enumerate(agg_chunks):
            kws = []
            sampled_shape = (
                (chunk_shape, chunk.shape[1]) if chunk.ndim == 2 else (chunk_shape,)
            )
            chunk_index = (i, 0) if chunk.ndim == 2 else (i,)
            chunk_op = DataFramePSRSGroupbySample(
                kind="quicksort",
                n_partition=chunk_shape,
                output_types=op.output_types,
                **properties,
            )
            if op.output_types[0] == OutputType.dataframe:
                kws.append(
                    {
                        "shape": sampled_shape,
                        "index_value": chunk.index_value,
                        "index": chunk_index,
                        "type": "regular_sampled",
                    }
                )
            else:
                kws.append(
                    {
                        "shape": sampled_shape,
                        "index_value": chunk.index_value,
                        "index": chunk_index,
                        "type": "regular_sampled",
                        "dtype": chunk.dtype,
                    }
                )
            chunk = chunk_op.new_chunk([chunk], kws=kws)
            sampled_chunks.append(chunk)

        return sampled_chunks

    @classmethod
    def _perform_shuffle(
        cls,
        op: "DataFrameGroupByAgg",
        agg_chunks: List[ChunkType],
        in_df: TileableType,
        out_df: TileableType,
        func_infos: ReductionSteps,
    ):
        if op.groupby_params["sort"] and len(in_df.chunks) > 1:
            agg_chunk_len = len(agg_chunks)
            sample_chunks = cls._sample_chunks(op, agg_chunks)
            pivot_chunk = cls._gen_pivot_chunk(op, sample_chunks, agg_chunk_len)
            reduce_chunks = cls._gen_shuffle_chunks_with_pivot(
                op, in_df, agg_chunks, pivot_chunk
            )
        else:
            reduce_chunks = cls._gen_shuffle_chunks(op, agg_chunks)

        # Combine groups
        agg_chunks = []
        for chunk in reduce_chunks:
            agg_op = op.copy().reset_key()
            agg_op.tileable_op_key = op.key
            agg_op.groupby_params = agg_op.groupby_params.copy()
            agg_op.groupby_params.pop("selection", None)
            # use levels instead of by for reducer
            agg_op.groupby_params.pop("by", None)
            agg_op.groupby_params["level"] = list(range(op.index_levels))
            agg_op.stage = OperandStage.agg
            agg_op.agg_funcs = func_infos.agg_funcs
            agg_op.post_funcs = func_infos.post_funcs
            if op.output_types[0] == OutputType.dataframe:
                agg_chunk = agg_op.new_chunk(
                    [chunk],
                    shape=out_df.shape,
                    index=chunk.index,
                    index_value=out_df.index_value,
                    dtypes=out_df.dtypes,
                    columns_value=out_df.columns_value,
                )
            else:
                agg_chunk = agg_op.new_chunk(
                    [chunk],
                    shape=out_df.shape,
                    index=(chunk.index[0],),
                    dtype=out_df.dtype,
                    index_value=out_df.index_value,
                    name=out_df.name,
                )
            agg_chunks.append(agg_chunk)

        new_op = op.copy()
        if op.output_types[0] == OutputType.dataframe:
            nsplits = ((np.nan,) * len(agg_chunks), (out_df.shape[1],))
        else:
            nsplits = ((np.nan,) * len(agg_chunks),)
        kw = out_df.params.copy()
        kw.update(dict(chunks=agg_chunks, nsplits=nsplits))
        return new_op.new_tileables([in_df], **kw)

    @classmethod
    def _tile_with_tree(
        cls,
        op: "DataFrameGroupByAgg",
        in_df: TileableType,
        out_df: TileableType,
        func_infos: ReductionSteps,
    ):
        chunks = cls._gen_map_chunks(op, in_df.chunks, out_df, func_infos)
        return cls._combine_tree(op, chunks, out_df, func_infos)

    @classmethod
    def _build_tree_chunks(
        cls,
        op: "DataFrameGroupByAgg",
        chunks: List[ChunkType],
        func_infos: ReductionSteps,
        combine_size: int,
        input_chunk_size: float = None,
        chunk_store_limit: int = None,
    ):
        out_df = op.outputs[0]
        # if concat chunk's size is greater than chunk_store_limit,
        # stop combining them.
        check_size = False
        if chunk_store_limit is not None:
            assert input_chunk_size is not None
            check_size = True
        concat_chunk_size = input_chunk_size
        while (not check_size or concat_chunk_size < chunk_store_limit) and (
            len(chunks) > combine_size
        ):
            new_chunks = []
            for idx, i in enumerate(range(0, len(chunks), combine_size)):
                chks = chunks[i : i + combine_size]
                if len(chks) == 1:
                    chk = chks[0]
                else:
                    concat_op = DataFrameConcat(output_types=out_df.op.output_types)
                    # Change index for concatenate
                    for j, c in enumerate(chks):
                        c._index = (j, 0)
                    if out_df.ndim == 2:
                        chk = concat_op.new_chunk(chks, dtypes=chks[0].dtypes)
                    else:
                        chk = concat_op.new_chunk(chks, dtype=chunks[0].dtype)
                chunk_op = op.copy().reset_key()
                chunk_op.tileable_op_key = None
                chunk_op.output_types = out_df.op.output_types
                chunk_op.stage = OperandStage.combine
                chunk_op.groupby_params = chunk_op.groupby_params.copy()
                chunk_op.groupby_params.pop("selection", None)
                # use levels instead of by for agg
                chunk_op.groupby_params.pop("by", None)
                chunk_op.groupby_params["level"] = list(range(op.index_levels))
                chunk_op.agg_funcs = func_infos.agg_funcs

                new_shape = (
                    (np.nan, out_df.shape[1]) if len(out_df.shape) == 2 else (np.nan,)
                )

                new_chunks.append(
                    chunk_op.new_chunk(
                        [chk],
                        index=(idx, 0),
                        shape=new_shape,
                        index_value=chks[0].index_value,
                        columns_value=getattr(out_df, "columns_value", None),
                    )
                )
            chunks = new_chunks
            if concat_chunk_size is not None:
                concat_chunk_size *= combine_size
        if concat_chunk_size:
            return chunks, concat_chunk_size
        else:
            return chunks

    @classmethod
    def _build_out_tileable(
        cls,
        op: "DataFrameGroupByAgg",
        out_df: TileableType,
        combined_chunks: List[ChunkType],
        func_infos: ReductionSteps,
    ):
        if len(combined_chunks) == 1:
            chk = combined_chunks[0]
        else:
            concat_op = DataFrameConcat(output_types=out_df.op.output_types)
            if out_df.ndim == 2:
                chk = concat_op.new_chunk(
                    combined_chunks, dtypes=combined_chunks[0].dtypes
                )
            else:
                chk = concat_op.new_chunk(
                    combined_chunks, dtype=combined_chunks[0].dtype
                )
        chunk_op = op.copy().reset_key()
        chunk_op.tileable_op_key = op.key
        chunk_op.stage = OperandStage.agg
        chunk_op.groupby_params = chunk_op.groupby_params.copy()
        chunk_op.groupby_params.pop("selection", None)
        # use levels instead of by for agg
        chunk_op.groupby_params.pop("by", None)
        chunk_op.groupby_params["level"] = list(range(op.index_levels))
        chunk_op.agg_funcs = func_infos.agg_funcs
        chunk_op.post_funcs = func_infos.post_funcs
        kw = out_df.params.copy()
        kw["index"] = (0, 0) if op.output_types[0] == OutputType.dataframe else (0,)
        chunk = chunk_op.new_chunk([chk], **kw)
        new_op = op.copy()
        if op.output_types[0] == OutputType.dataframe:
            nsplits = ((out_df.shape[0],), (out_df.shape[1],))
        else:
            nsplits = ((out_df.shape[0],),)

        kw = out_df.params.copy()
        kw.update(dict(chunks=[chunk], nsplits=nsplits))
        return new_op.new_tileables(op.inputs, **kw)

    @classmethod
    def _combine_tree(
        cls,
        op: "DataFrameGroupByAgg",
        chunks: List[ChunkType],
        out_df: TileableType,
        func_infos: ReductionSteps,
    ):
        combine_size = op.combine_size
        chunks = cls._build_tree_chunks(op, chunks, func_infos, combine_size)
        return cls._build_out_tileable(op, out_df, chunks, func_infos)

    @classmethod
    def _build_tree_and_shuffle_chunks(
        cls,
        op: "DataFrameGroupByAgg",
        in_df: TileableType,
        out_df: TileableType,
        func_infos: ReductionSteps,
        sample_map_chunks: List[ChunkType],
        sample_agg_sizes: List[int],
    ):
        combine_size = op.combine_size
        left_chunks = cls._gen_map_chunks(
            op, in_df.chunks[combine_size:], out_df, func_infos
        )
        input_size = sum(sample_agg_sizes) / len(sample_agg_sizes)
        combine_chunk_limit = op.chunk_store_limit / 4
        combined_chunks, concat_size = cls._build_tree_chunks(
            op,
            sample_map_chunks + left_chunks,
            func_infos,
            combine_size,
            input_size,
            combine_chunk_limit,
        )
        logger.debug(
            "Combine map chunks to %s chunks for groupby operand %s",
            len(combined_chunks),
            op,
        )
        if concat_size <= combine_chunk_limit:
            logger.debug(
                "Choose tree method after combining chunks for groupby operand %s", op
            )
            return cls._build_out_tileable(op, out_df, combined_chunks, func_infos)
        else:
            logger.debug(
                "Choose shuffle method after combining chunks for "
                "groupby operand %s, chunk count is %s",
                op,
                len(combined_chunks),
            )
            return cls._perform_shuffle(
                op,
                combined_chunks,
                in_df,
                out_df,
                func_infos,
            )

    @classmethod
    def _tile_auto(
        cls,
        op: "DataFrameGroupByAgg",
        in_df: TileableType,
        out_df: TileableType,
        func_infos: ReductionSteps,
    ):
        ctx = get_context()
        combine_size = op.combine_size
        size_recorder_name = str(uuid.uuid4())
        size_recorder = ctx.create_remote_object(size_recorder_name, SizeRecorder)

        # collect the first combine_size chunks, run it
        # to get the size before and after agg
        chunks = cls._gen_map_chunks(
            op, in_df.chunks[:combine_size], out_df, func_infos
        )
        for chunk in chunks:
            chunk.op.size_recorder_name = size_recorder_name
        # yield to trigger execution
        yield chunks

        raw_sizes, agg_sizes = size_recorder.get()
        # destroy size recorder
        ctx.destroy_remote_object(size_recorder_name)

        logger.debug(
            "Start to choose method for Groupby, agg_sizes: %s, raw_sizes: %s, "
            "sample_count: %s, total_count: %s, chunk_store_limit: %s",
            agg_sizes,
            raw_sizes,
            len(agg_sizes),
            len(in_df.chunks),
            op.chunk_store_limit,
        )

        return cls._build_tree_and_shuffle_chunks(
            op, in_df, out_df, func_infos, chunks, agg_sizes
        )

    @classmethod
    def tile(cls, op: "DataFrameGroupByAgg"):
        in_df = op.inputs[0]
        if len(in_df.shape) > 1:
            in_df = build_concatenated_rows_frame(in_df)
        out_df = op.outputs[0]

        func_infos = cls._compile_funcs(op, in_df)

        if op.method == "auto":
            logger.debug("Choose auto method for groupby operand %s", op)
            if len(in_df.chunks) <= op.combine_size:
                return cls._tile_with_tree(op, in_df, out_df, func_infos)
            else:
                return (yield from cls._tile_auto(op, in_df, out_df, func_infos))
        if op.method == "shuffle":
            logger.debug("Choose shuffle method for groupby operand %s", op)
            return cls._tile_with_shuffle(op, in_df, out_df, func_infos)
        elif op.method == "tree":
            logger.debug("Choose tree method for groupby operand %s", op)
            return cls._tile_with_tree(op, in_df, out_df, func_infos)
        else:  # pragma: no cover
            raise NotImplementedError

    @classmethod
    def _get_grouped(cls, op: "DataFrameGroupByAgg", df, ctx, copy=False, grouper=None):
        if copy:
            df = df.copy()

        params = op.groupby_params.copy()
        params.pop("as_index", None)
        selection = params.pop("selection", None)

        if grouper is not None:
            params["by"] = grouper
            params.pop("level", None)
        elif isinstance(params.get("by"), list):
            new_by = []
            for v in params["by"]:
                if isinstance(v, ENTITY_TYPE):
                    new_by.append(ctx[v.key])
                else:
                    new_by.append(v)
            params["by"] = new_by

        grouped = df.groupby(**params)

        if selection is not None:
            grouped = grouped[selection]
        return grouped

    @staticmethod
    def _pack_inputs(agg_funcs: List[ReductionAggStep], in_data):
        pos = 0
        out_dict = dict()
        for step in agg_funcs:
            if step.custom_reduction is None:
                out_dict[step.output_key] = in_data[pos]
            else:
                out_dict[step.output_key] = tuple(
                    in_data[pos : pos + step.output_limit]
                )
            pos += step.output_limit
        return out_dict

    @staticmethod
    def _do_custom_agg(
        func_name: str, op: "DataFrameGroupByAgg", in_data: pd.DataFrame
    ) -> Union[pd.Series, pd.DataFrame]:
        # Must be tuple way, like x=('col', 'agg_func_name')
        # See `is_funcs_aggregate` func,
        # if not this way, the code doesn't go here or switch to transform execution.
        if op.raw_func is None:
            func_name = list(op.raw_func_kw.values())[0][1]

        if op.stage == OperandStage.map:
            return custom_agg_functions[func_name].execute_map(op, in_data)
        elif op.stage == OperandStage.combine:
            return custom_agg_functions[func_name].execute_combine(op, in_data)
        else:  # must be OperandStage.agg, since OperandStage.reduce has been excluded in the execute function.
            return custom_agg_functions[func_name].execute_agg(op, in_data)

    @staticmethod
    def _do_predefined_agg(
        input_obj,
        agg_func,
        single_func: bool = False,
        gpu: Optional[bool] = False,
        **kwds,
    ):
        ndim = getattr(input_obj, "ndim", None) or input_obj.obj.ndim
        if agg_func == "str_concat":
            agg_func = lambda x: x.str.cat(**kwds)
        elif isinstance(agg_func, str) and not kwds.get("skipna", True):
            func_name = agg_func
            agg_func = lambda x: getattr(x, func_name)(skipna=False)
            agg_func.__name__ = func_name

        if ndim == 2:
            if single_func:
                # DataFrameGroupby.agg('size') returns empty df in cudf, which is not correct
                # The index order of .size() is wrong in cudf,
                # however, for performance considerations, sort_index() will not be called here
                result = (
                    input_obj.size()
                    if gpu and agg_func == "size"
                    else input_obj.agg(agg_func)
                )
            else:
                result = input_obj.agg([agg_func])
                result.columns = result.columns.droplevel(-1)
            return result
        else:
            return input_obj.agg(agg_func)

    @staticmethod
    def _series_to_df(in_series, gpu):
        xdf = cudf if gpu else pd

        in_df = in_series.to_frame()
        if in_series.name is not None:
            in_df.columns = xdf.Index([in_series.name])
        return in_df

    @classmethod
    def _execute_map(cls, ctx, op: "DataFrameGroupByAgg"):
        xdf = cudf if op.gpu else pd

        in_data = ctx[op.inputs[0].key]
        if (
            isinstance(in_data, xdf.Series)
            and op.output_types[0] == OutputType.dataframe
        ):
            in_data = cls._series_to_df(in_data, op.gpu)

        # map according to map groups
        ret_map_groupbys = dict()
        grouped = cls._get_grouped(op, in_data, ctx)
        grouper = None
        drop_names = False

        for input_key, output_key, cols, func in op.pre_funcs:
            if input_key == output_key:
                if cols is None or getattr(grouped, "_selection", None) is not None:
                    ret_map_groupbys[output_key] = grouped
                else:
                    ret_map_groupbys[output_key] = grouped[cols]
            else:

                def _wrapped_func(col):
                    try:
                        return func(col, gpu=op.is_gpu())
                    except TypeError:
                        return col

                pre_df = in_data if cols is None else in_data[cols]
                try:
                    pre_df = func(pre_df, gpu=op.is_gpu())
                except TypeError:
                    pre_df = pre_df.transform(_wrapped_func)

                if grouper is None:
                    try:
                        grouper = grouped.grouper
                    except AttributeError:  # cudf does not have GroupBy.grouper
                        grouper = xdf.Series(
                            grouped.grouping.keys, index=grouped.obj.index
                        )
                        if in_data.ndim == 2:
                            drop_names = True

                if drop_names:
                    pre_df = pre_df.drop(
                        columns=grouped.grouping.names, errors="ignore"
                    )
                ret_map_groupbys[output_key] = cls._get_grouped(
                    op, pre_df, ctx, grouper=grouper
                )

        agg_dfs = []
        for (
            input_key,
            raw_func_name,
            map_func_name,
            _agg_func_name,
            custom_reduction,
            _output_key,
            _output_limit,
            kwds,
        ) in op.agg_funcs:
            input_obj = ret_map_groupbys[input_key]
            if map_func_name == "custom_reduction":
                agg_dfs.append(cls._do_custom_agg(raw_func_name, op, in_data))
            else:
                single_func = map_func_name == op.raw_func
                agg_dfs.append(
                    cls._do_predefined_agg(
                        input_obj, map_func_name, single_func, op.gpu, **kwds
                    )
                )

        if getattr(op, "size_recorder_name", None) is not None:
            # record_size
            raw_size = estimate_pandas_size(in_data)
            # when agg by a list of methods, agg_size should be sum
            agg_size = sum([estimate_pandas_size(item) for item in agg_dfs])
            size_recorder = ctx.get_remote_object(op.size_recorder_name)
            size_recorder.record(raw_size, agg_size)

        ctx[op.outputs[0].key] = tuple(agg_dfs)

    @classmethod
    def _execute_combine(cls, ctx, op: "DataFrameGroupByAgg"):
        xdf = cudf if op.gpu else pd

        in_data_tuple = ctx[op.inputs[0].key]
        in_data_list = []
        for in_data in in_data_tuple:
            if (
                isinstance(in_data, xdf.Series)
                and op.output_types[0] == OutputType.dataframe
            ):
                in_data = cls._series_to_df(in_data, op.gpu)
            in_data_list.append(cls._get_grouped(op, in_data, ctx))
        in_data_tuple = tuple(in_data_list)
        in_data_dict = cls._pack_inputs(op.agg_funcs, in_data_tuple)

        combines = []
        for raw_input, (
            _input_key,
            raw_func_name,
            _map_func_name,
            agg_func_name,
            custom_reduction,
            output_key,
            _output_limit,
            kwds,
        ) in zip(ctx[op.inputs[0].key], op.agg_funcs):
            input_obj = in_data_dict[output_key]
            if agg_func_name == "custom_reduction":
                combines.append(cls._do_custom_agg(raw_func_name, op, raw_input))
            else:
                combines.append(
                    cls._do_predefined_agg(input_obj, agg_func_name, gpu=op.gpu, **kwds)
                )
        ctx[op.outputs[0].key] = tuple(combines)

    @classmethod
    def _execute_agg(cls, ctx, op: "DataFrameGroupByAgg"):
        xdf = cudf if op.gpu else pd
        out_chunk = op.outputs[0]
        col_value = (
            out_chunk.columns_value.to_pandas()
            if hasattr(out_chunk, "columns_value")
            else None
        )

        in_data_tuple = ctx[op.inputs[0].key]
        in_data_list = []
        for in_data in in_data_tuple:
            if (
                isinstance(in_data, xdf.Series)
                and op.output_types[0] == OutputType.dataframe
            ):
                in_data = cls._series_to_df(in_data, op.gpu)
            in_data_list.append(in_data)
        in_data_tuple = tuple(in_data_list)
        in_data_dict = cls._pack_inputs(op.agg_funcs, in_data_tuple)

        for (
            _input_key,
            raw_func_name,
            _map_func_name,
            agg_func_name,
            custom_reduction,
            output_key,
            _output_limit,
            kwds,
        ) in op.agg_funcs:
            if agg_func_name == "custom_reduction":
                in_data_dict[output_key] = cls._do_custom_agg(
                    raw_func_name, op, in_data_dict[output_key]
                )
            else:
                input_obj = cls._get_grouped(op, in_data_dict[output_key], ctx)
                in_data_dict[output_key] = cls._do_predefined_agg(
                    input_obj, agg_func_name, gpu=op.gpu, **kwds
                )

        aggs = []
        for input_keys, _output_key, func_name, cols, func in op.post_funcs:
            if func_name in custom_agg_functions:
                agg_df = in_data_dict[_output_key]
            else:
                if cols is None:
                    func_inputs = [in_data_dict[k] for k in input_keys]
                else:
                    func_inputs = [in_data_dict[k][cols] for k in input_keys]

                if (
                    func_inputs[0].ndim == 2
                    and len(set(inp.shape[1] for inp in func_inputs)) > 1
                ):
                    common_cols = func_inputs[0].columns
                    for inp in func_inputs[1:]:
                        common_cols = common_cols.join(inp.columns, how="inner")
                    func_inputs = [inp[common_cols] for inp in func_inputs]

                agg_df = func(*func_inputs, gpu=op.is_gpu())
            if isinstance(agg_df, np.ndarray):
                agg_df = xdf.DataFrame(agg_df, index=func_inputs[0].index)

            new_cols = None
            if out_chunk.ndim == 2 and col_value is not None:
                if col_value.nlevels > agg_df.columns.nlevels:
                    new_cols = xdf.MultiIndex.from_product(
                        [agg_df.columns, [func_name]]
                    )
                elif agg_df.shape[-1] == 1 and func_name in col_value:
                    new_cols = xdf.Index([func_name])
            aggs.append((agg_df, new_cols))

        for agg_df, new_cols in aggs:
            if new_cols is not None:
                agg_df.columns = new_cols
        aggs = [item[0] for item in aggs]

        if out_chunk.ndim == 2:
            result = concat_on_columns(aggs)
            if (
                not op.groupby_params.get("as_index", True)
                and col_value.nlevels == result.columns.nlevels
            ):
                result.reset_index(
                    inplace=True,
                    drop=False
                    if xdf is pd
                    and PD_VERSION_GREATER_THAN_2_10
                    and isinstance(result.columns, pd.MultiIndex)
                    else result.index.name in result.columns,
                )
            if isinstance(col_value, xdf.MultiIndex) and not col_value.is_unique:
                # reindex doesn't work when the agg function list contains duplicated
                # functions, e.g. df.groupby(...)agg((func, func))
                if xdf is pd and PD_VERSION_GREATER_THAN_2_10:
                    result = xdf.concat(
                        [result[c] for c in col_value.drop_duplicates()], axis=1
                    )
                else:
                    result = result.iloc[:, result.columns.duplicated()]
                    result = xdf.concat([result[c] for c in col_value], axis=1)
                result.columns = col_value
            else:
                result = result.reindex(col_value, axis=1)

            if result.ndim == 2 and len(result) == 0:
                result = result.astype(out_chunk.dtypes)
        else:
            result = xdf.concat(aggs)
            if result.ndim == 2:
                result = result.iloc[:, 0]
                if is_cudf(result):  # pragma: no cover
                    result = result.copy()
            result.name = out_chunk.name

        ctx[out_chunk.key] = result

    @classmethod
    @redirect_custom_log
    @enter_current_session
    def execute(cls, ctx, op: "DataFrameGroupByAgg"):
        if op.stage == OperandStage.map:
            cls._execute_map(ctx, op)
        elif op.stage == OperandStage.combine:
            cls._execute_combine(ctx, op)
        elif op.stage == OperandStage.agg:
            cls._execute_agg(ctx, op)
        else:  # pragma: no cover
            raise ValueError("Aggregation operand not executable")


def agg(groupby, func=None, method="auto", combine_size=None, *args, **kwargs):
    """
    Aggregate using one or more operations on grouped data.

    Parameters
    ----------
    groupby : Mars Groupby
        Groupby data.
    func : str or list-like
        Aggregation functions.
    method : {'auto', 'shuffle', 'tree'}, default 'auto'
        'tree' method provide a better performance, 'shuffle' is recommended
        if aggregated result is very large, 'auto' will use 'shuffle' method
        in distributed mode and use 'tree' in local mode.
    combine_size : int
        The number of chunks to combine when method is 'tree'


    Returns
    -------
    Series or DataFrame
        Aggregated result.
    """

    # When perform a computation on the grouped data, we won't shuffle
    # the data in the stage of groupby and do shuffle after aggregation.

    if not isinstance(groupby, GROUPBY_TYPE):
        raise TypeError(f"Input should be type of groupby, not {type(groupby)}")

    if method is None:
        method = "auto"
    if method not in ["shuffle", "tree", "auto"]:
        raise ValueError(
            f"Method {method} is not available, please specify 'tree' or 'shuffle"
        )

    if not is_funcs_aggregate(func, ndim=groupby.ndim):
        # pass index to transform, otherwise it will lose name info for index
        agg_result = build_mock_agg_result(
            groupby, groupby.op.groupby_params, func, **kwargs
        )
        if isinstance(agg_result.index, pd.RangeIndex):
            # set -1 to represent unknown size for RangeIndex
            index_value = parse_index(
                pd.RangeIndex(-1), groupby.key, groupby.index_value.key
            )
        else:
            index_value = parse_index(
                agg_result.index, groupby.key, groupby.index_value.key
            )
        return groupby.transform(
            func, *args, _call_agg=True, index=index_value, **kwargs
        )

    agg_op = DataFrameGroupByAgg(
        raw_func=func,
        raw_func_kw=kwargs,
        method=method,
        raw_groupby_params=groupby.op.groupby_params,
        groupby_params=groupby.op.groupby_params,
        combine_size=combine_size or options.combine_size,
        chunk_store_limit=options.chunk_store_limit,
    )
    return agg_op(groupby)
