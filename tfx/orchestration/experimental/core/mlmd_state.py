# Copyright 2021 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for working with MLMD state."""

import collections
import contextlib
import copy
import threading
import typing
from typing import Callable, Iterator, MutableMapping, Optional

import cachetools
from tfx.orchestration import metadata

from google.protobuf.internal import containers
from ml_metadata.proto import metadata_store_pb2


class _LocksManager:
  """Class for managing value based locking."""

  def __init__(self):
    self._main_lock = threading.Lock()
    self._locks: MutableMapping[typing.Hashable, threading.Lock] = {}
    self._refcounts = collections.defaultdict(int)

  @contextlib.contextmanager
  def lock(self, value: typing.Hashable) -> Iterator[None]:
    """Context manager for input value based locking.

    Only one thread can enter the context for a given value.

    Args:
      value: Value of any hashable type.

    Yields:
      Nothing.
    """
    with self._main_lock:
      lock = self._locks.setdefault(value, threading.Lock())
      self._refcounts[value] += 1
    try:
      with lock:
        yield
    finally:
      with self._main_lock:
        self._refcounts[value] -= 1
        if self._refcounts[value] <= 0:
          del self._refcounts[value]
          del self._locks[value]


class _ExecutionCache:
  """Read-through / write-through cache for MLMD executions."""

  def __init__(self):
    self._cache: MutableMapping[
        int, metadata_store_pb2.Execution] = cachetools.LRUCache(maxsize=1024)
    self._lock = threading.Lock()

  def get_execution(self, mlmd_handle: metadata.Metadata,
                    execution_id: int) -> metadata_store_pb2.Execution:
    """Gets execution either from cache or, upon cache miss, from MLMD."""
    with self._lock:
      execution = self._cache.get(execution_id)
    if not execution:
      executions = mlmd_handle.store.get_executions_by_id([execution_id])
      if executions:
        execution = executions[0]
        with self._lock:
          self._cache[execution_id] = execution
    if not execution:
      raise ValueError(f'Execution not found for execution id: {execution_id}')
    return execution

  def put_execution(self, mlmd_handle: metadata.Metadata,
                    execution: metadata_store_pb2.Execution,
                    field_mask_paths: Optional[list[str]] = None) -> None:
    """Writes execution to MLMD and updates cache."""
    mlmd_handle.store.put_executions([execution], field_mask_paths)
    # The execution is fetched from MLMD again to ensure that the in-memory
    # value of `last_update_time_since_epoch` of the execution is same as the
    # one stored in MLMD.
    [execution] = mlmd_handle.store.get_executions_by_id([execution.id])
    with self._lock:
      self._cache[execution.id] = execution

  def evict(self, execution_id: int) -> None:
    """Evicts execution with the given execution_id from the cache if one exists."""
    self._cache.pop(execution_id, None)

  def clear_cache(self):
    """Clears underlying cache; MLMD is untouched."""
    with self._lock:
      self._cache.clear()


_execution_cache = _ExecutionCache()
_execution_id_locks = _LocksManager()


@contextlib.contextmanager
def mlmd_execution_atomic_op(
    mlmd_handle: metadata.Metadata,
    execution_id: int,
    on_commit: Optional[
        Callable[
            [metadata_store_pb2.Execution, metadata_store_pb2.Execution], None
        ]
    ] = None,
    pre_commit: Optional[
        Callable[
            [metadata_store_pb2.Execution, metadata_store_pb2.Execution], None
        ]
    ] = None,
) -> Iterator[metadata_store_pb2.Execution]:
  """Context manager for accessing or mutating an execution atomically.

  The idea of using this context manager is to ensure that the in-memory state
  of an MLMD execution is centrally managed so that it stays in sync with the
  execution in MLMD even when multiple threads in the process may be mutating.

  If execution for given execution id exists in MLMD, it is locked before being
  yielded so that no other thread in the process can make conflicting updates if
  the yielded execution is mutated within the context. Mutated executions are
  also automatically committed to MLMD when exiting the context.

  Args:
    mlmd_handle: A handle to MLMD db.
    execution_id: Id of the execution to yield.
    on_commit: An optional callback function which is invoked post successful
      MLMD execution commit operation. This won't be invoked if execution is not
      mutated within the context and hence MLMD commit is not needed. The
      callback is passed copies of the pre-commit and post-commit executions.
    pre_commit: An optional hook function which is invoked before the execution
      gets committed to MLMD. Note that if the execution is not mutated within
      the context manager, this function would not be invoked either. The hook
      function should neither apply any modification to `execution` nor
      `execution_copy`.

  Yields:
    If execution with given id exists in MLMD, the execution is yielded under
    an exclusive lock context.

  Raises:
    RuntimeError: If execution id is changed within the context.
    ValueError: If execution having given execution id is not found in MLMD.
  """
  with _execution_id_locks.lock(execution_id):
    execution = _execution_cache.get_execution(mlmd_handle, execution_id)
    execution_copy = copy.deepcopy(execution)
    yield execution_copy
    if execution != execution_copy:
      if execution.id != execution_copy.id:
        raise RuntimeError(
            'Execution id should not be changed within mlmd_execution_atomic_op'
            ' context.')

      if pre_commit is not None:
        pre_commit(execution, execution_copy)

      # Orchestrator code will only update top-level fields and properties/
      # custom properties with diffs.

      # Motivation: to allow non-orchestrator code (specifically, pipeline tags
      # and labels) to modify execution custom properties while the orchestrator
      # is running. Delta changes are only applied for masked properties /
      # custom properties. execution.last_known_state will always be updated.

      # It enables orchestrator and non-orchestrator codes to run concurrently
      # as long as there are no overlaps in the modified fields.

      # Make a copy before writing to cache as the yielded `execution_copy`
      # object may be modified even after exiting the contextmanager.
      _execution_cache.put_execution(
          mlmd_handle,
          copy.deepcopy(execution_copy),
          get_field_mask_paths(execution, execution_copy),
      )
      if on_commit is not None:
        pre_commit_execution = copy.deepcopy(execution)
        post_commit_execution = copy.deepcopy(
            _execution_cache.get_execution(mlmd_handle, execution_copy.id))
        on_commit(pre_commit_execution, post_commit_execution)


@contextlib.contextmanager
def evict_from_cache(execution_id: int) -> Iterator[None]:
  """Context manager for mutating an MLMD execution using cache unaware functions.

  It is preferable to use `mlmd_execution_atomic_op` for mutating MLMD
  executions but sometimes it may be necessary to use third party functions
  which are not cache aware. Such functions should be invoked within this
  context for proper locking and cache eviction to prevent stale entries.

  Args:
    execution_id: Id of the execution to be evicted from cache.

  Yields:
    Nothing
  """
  with _execution_id_locks.lock(execution_id):
    _execution_cache.evict(execution_id)
    yield


def clear_in_memory_state():
  """Clears cached state. Useful in tests."""
  _execution_cache.clear_cache()


def get_field_mask_paths(
    execution: metadata_store_pb2.Execution,
    execution_copy: metadata_store_pb2.Execution,
) -> list[str]:
  """Get Execution field mask paths for mutations.

  Args:
    execution: original in-memory state of an MLMD execution.
    execution_copy: in-memory state of an MLMD execution after mutations.

  Returns:
    All top-level field paths, and property / custom property fields with diffs.
    Only field paths in the mask will be updated during MLMD commits.
  """
  field_mask_paths = []

  # Get all non-property field paths.
  for field in metadata_store_pb2.Execution.DESCRIPTOR.fields:
    # Skip property fields.
    if field.name not in ['properties', 'custom_properties']:
      field_mask_paths.append(field.name)

  # Get property names with diffs.  Note that Python supports == operator for
  # proto messages.
  def _get_property_names_with_diff(
      properties: containers.MessageMap[str, metadata_store_pb2.Value],
      copy_properties: containers.MessageMap[str, metadata_store_pb2.Value],
  ) -> list[str]:
    property_names_with_diff = []
    for name in set(properties.keys()).union(set(copy_properties.keys())):
      if (
          name in properties
          and name in copy_properties
          and properties[name] == copy_properties[name]
      ):
        continue
      property_names_with_diff.append(name)
    return property_names_with_diff

  property_names_with_diff = _get_property_names_with_diff(
      execution.properties, execution_copy.properties
  )
  custom_property_names_with_diff = _get_property_names_with_diff(
      execution.custom_properties, execution_copy.custom_properties
  )

  field_mask_paths.extend(
      [f'properties.{name}' for name in property_names_with_diff]
  )
  field_mask_paths.extend(
      [f'custom_properties.{name}' for name in custom_property_names_with_diff]
  )
  return field_mask_paths
