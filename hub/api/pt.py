from collections import defaultdict
import os
import pickle
from hub.util.keys import get_index_map_key
from hub.core.chunk_engine.chunker import join_chunks
from hub.core.storage import S3Provider
from pathos.pools import ProcessPool, ThreadPool
import numpy as np

from hub.core.chunk_engine.read import read_tensor_meta
from itertools import repeat
import time
from concurrent.futures import ThreadPoolExecutor, as_completed




def _transform_data(args):
    transform, data = args
    return transform(data) if transform else data


def _read_chunks(args):
    chunk_key, storage = args
    start = time.time()
    out = storage[chunk_key]
    end = time.time()
    # print("took thread", end - start)
    return chunk_name, out


def _to_pytorch(dataset, transform=None, workers=1):
    try:
        import torch
    except ModuleNotFoundError:
        raise Exception
        # raise ModuleNotInstalledException("torch")

    global torch
    return TorchDataset(dataset, transform, workers)


class TorchDataset:
    def __init__(self, ds, transform=None, workers=1):
        self.ds = ds
        # TODO disable the memory cache
        self.transform = transform
        self.workers = workers
        self.storage = self.ds.provider
        self._load_index_maps()
        self._load_meta()
        self.key_chunks = {}
        self.thread_pool = ThreadPool(nodes=workers)
        self.process_pool = ProcessPool(nodes=workers) if self.transform else None
        self.all_index_value_maps = defaultdict(dict)
        self.last_index_map = {}
        self.first_sample_processed = -1
        self.last_sample_processed = -1


    def _load_index_maps(self):
        self.all_index_maps = {}
        for key in self.ds.tensors:
            # meta = read_tensor_meta(key, self.storage)
            index_map = pickle.loads(self.storage[get_index_map_key(key)])
            self.all_index_maps[key] = index_map

    def _load_meta(self):
        self.all_meta = {}
        for key in self.ds.tensors:
            meta = read_tensor_meta(key, self.storage)
            if meta["dtype"] == "uint16":
                meta["dtype"] = "int32"
            elif meta["dtype"] in ["uint32", "uint64"]:
                meta["dtype"] = "int64"
            self.all_meta[key] = meta

    def __len__(self):
        return len(self.ds)

    def _get_value_from_chunks(self, start_ind, key, chunk_map):
        dtype = self.all_meta[key]["dtype"]
        index_value_map = {}
        index = start_ind
        while index < len(self.ds):
            # cur_chunks = self.all_index_maps[key][index]["chunk_names"]
            chunks = []
            index_entry = self.all_index_maps[key][index]
            for chunk_name in index_entry["chunk_names"]:
                if chunk_name not in chunk_map:
                    return index_value_map, index - 1
                chunk = chunk_map[chunk_name]
                chunks.append(chunk)

            combined_bytes = join_chunks(
                chunks,
                index_entry["start_byte"],
                index_entry["end_byte"],
            )
            index_value_map[index] = np.frombuffer(combined_bytes, dtype=dtype).reshape(
                index_entry["shape"]
            )
            index += 1
        return index_value_map, index - 1

    # def _get_transform_chunk(self, key, chunk_names, start_ind):
    #     chunk_map = {}
    #     for chunk_name in chunk_names:
    #         chunk_key = os.path.join(key, "chunks", chunk_name)
    #         chunk_map[key] = self.storage[chunk_key]
    #     index = start_ind
    #     index_map = self.all_index_maps[key]
    #     while index_map[index]["chunk_names"][0] not in chunk_names and index<len(self.ds):
    #         index += 1

    #     index_value_map = {}
    #     meta = self.all_meta[key]
    #     while index<len(self.ds):
    #         index_entry = index_map[index]
    #         chunks = []
    #         for chunk_name in index_entry["chunk_names"]:
    #             if chunk_name not in chunk_map:
    #                 return index_value_map
    #             chunk = chunk_map[chunk_name]
    #             chunks.append(chunk)

    #         combined_bytes = join_chunks(
    #             chunks,
    #             index_entry["start_byte"],
    #             index_entry["end_byte"],
    #         )
    #         index_value_map[index] = np.frombuffer(combined_bytes, dtype=meta["dtype"]).reshape(index_entry["shape"])
    #         index += 1
    #     index_value_map

    def __getitem__(self, index):
        for key in self.ds.tensors:
            if index in self.all_index_value_maps[key]:
                # print("cache hit!", key, index)
                continue

            chunk_set = set()
            ind = index
            # while len(chunk_set) < self.workers and ind < len(self):
            #     chunk_names = self.all_index_maps[key][ind]["chunk_names"]
            #     chunk_set.add(tuple(chunk_names))
            #     ind += 1
            while len(chunk_set) < self.workers and ind < len(self):
                chunk_names = self.all_index_maps[key][ind]["chunk_names"]
                chunk_set.update(chunk_names)
                ind += 1

                if len(chunk_set) > self.workers:
                    chunk_set -= set(chunk_names)
            # print("fetching", len(chunk_set))
            start = time.time()
            # chunks = []
            # with ThreadPoolExecutor(max_workers=self.workers) as executor:
            #     # Using a dict for preserving the downloaded file for each future, to store it as a failure if we need that
            #     futures = [
            #         executor.submit(_read_chunks, key, chunk, self.storage) for chunk in chunk_set
            #     ]
            # for future in as_completed(futures):
            #     chunks.append(future.result())
            chunk_keys = [os.path.join(key, "chunks", chunk_name) for chunk_name in chunk_set]
            chunks = self.thread_pool.map(
                _read_chunks, zip(chunk_keys, repeat(self.storage))
            )
            end = time.time()
            print("time was", end-start)
            chunk_map = dict(chunks)
            # del chunks
            (
                self.all_index_value_maps[key],
                self.last_index_map[key],
            ) = self._get_value_from_chunks(index, key, chunk_map)

        if index > self.last_sample_processed:
            start_index = self.last_sample_processed + 1
            last_index = min(self.last_index_map[key] for key in self.ds.tensors)
            raw_samples = []
            for i in range(start_index, last_index + 1):
                d = {key: self.all_index_value_maps[key][i] for key in self.ds.tensors}
                raw_samples.append(d)
            if self.transform:
                self.processed_samples = self.process_pool.map(
                    _transform_data, zip(repeat(self.transform), raw_samples)
                )
            else:
                self.processed_samples = raw_samples
            self.first_sample_processed = start_index
            self.last_sample_processed = last_index
        return self.processed_samples[index - self.first_sample_processed]

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]