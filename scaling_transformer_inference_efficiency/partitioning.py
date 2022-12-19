# coding=utf-8
# Copyright 2022 The Google Research Authors.
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

"""Parallel partitioning functions."""

import contextlib
from enum import Enum  # pylint: disable = g-importing-member
import functools
import threading
from typing import Any, Optional, Sequence, Union

import jax
from jax import pxla
from jax.experimental import mesh_utils
from jax.experimental import pjit
from jax.experimental.gda_serialization import serialization as jax_gda_serialization
from jax.experimental.global_device_array import GlobalDeviceArray
from jax.experimental.maps import Mesh
from jax.experimental.pjit import PartitionSpec as P
import jax.numpy as jnp
from jax.sharding import NamedSharding
import numpy as np
import tensorstore



class AttnAllToAll(Enum):
  """How much of an alltoall to use for attention."""
  NONE = 0  # [batch.B, heads.YZX]
  AXIS_Z = 1  # [batch.ZB, heads.YX]
  AXES_YZ = 2  # [batch.YZB, heads.X]
  AXES_YZX = 3  # [batch.YZXB, heads]


def attn_sharding_to_axes(attn_batch_sharding):
  if attn_batch_sharding == AttnAllToAll.NONE:
    return None
  elif attn_batch_sharding == AttnAllToAll.AXIS_Z:
    return 'z'
  elif attn_batch_sharding == AttnAllToAll.AXES_YZ:
    return ('y', 'z')
  elif attn_batch_sharding == AttnAllToAll.AXES_YZX:
    return ('y', 'z', 'x')


def make_rules_two_d(attn_batch_sharding=AttnAllToAll.NONE):

  return [
      ('prefix_time', None),
      ('prefix_layers', None),
      ('prefix_qkv', None),
      ('batch', None),
      ('residual_batch', ('z',)),
      ('logit_batch', 'x'),  # for vocab
      ('residual_embed', ('x', 'y')),
      ('post_norm_batch', None),
      ('post_norm_embed', 'x'),
      ('heads', ('y', 'z', 'x')),
      ('qkv', None),
      ('params_heads', ('y', 'z')),
      ('params_embed', 'x'),
      ('vocab', ('y', 'z')),
      ('attn_batch', attn_sharding_to_axes(attn_batch_sharding)),
  ]


class _ThreadResourcesLocalState(threading.local):

  def __init__(self):
    self.stack = [[]]  # empty rules

  @property
  def rules(self):
    return self.stack[-1]


thread_resources = _ThreadResourcesLocalState()


class PartitioningRules(contextlib.ContextDecorator):
  """Creates a new set of rules in a context manager.

  Usage:
  rules = partitioning.PartitioningRules(
        partitioning.make_rules_two_d(attn_sharding))
  with rules:
    x = logical_to_physical(y)  # no need thread rules everywhere
  """

  def __init__(self, rules):
    self.rules = rules

  def __enter__(self):
    thread_resources.stack.append(self.rules)
    return self

  def __exit__(self, exc_type, exc_value, traceback):
    thread_resources.stack.pop()
    return False


def logical_to_physical(logical_axes):
  """Converts logical to physical axes for a layer using rule priority."""
  # Priority order of logical to physical axes mapping

  result = [None] * len(logical_axes)
  for logical_axis, physical_axis in thread_resources.rules:
    if logical_axis in logical_axes:
      pos = logical_axes.index(logical_axis)
      # Only map that logical axis against the physical if it hasn't already
      # been mapped - therefore earlier rules have priority over later ones.
      if physical_axis not in result:
        result[pos] = result[pos] or physical_axis
  return P(*result)


@functools.cache
def make_mesh():
  """Creates a device mesh for use with xmap over x/y/z axes."""
  devices = jax.devices()
  if len(devices) == 1:
    x, y, z = 1, 1, 1  # TODO(sholto): test
  elif len(devices) == 4:
    x, y, z = 2, 1, 2  # TODO(sholto): test
  elif len(devices) == 8:
    x, y, z = 2, 2, 2  # TODO(sholto): test - always appropriate for B=1?
  elif len(devices) == 16:
    # 2,4,2 or 4,2,2 is good
    x, y, z = 2, 4, 2
  elif len(devices) == 32:
    x, y, z = 4, 2, 4
  elif len(devices) == 64:
    x, y, z = 4, 4, 4
  elif len(devices) == 128:
    x, y, z = 8, 4, 4
    # x, y, z = 4, 4, 8
  elif len(devices) == 256:
    # x, y, z = 8, 4, 8
    x, y, z = 4, 8, 8
  elif len(devices) == 512:
    x, y, z = 8, 8, 8
  else:
    raise NotImplementedError

  return Mesh(mesh_utils.create_device_mesh((x, y, z)), ('x', 'y', 'z'))


def copy_to_device(x, sharding,
                   expected):
  """Copies the input to the device, however is appropriate for the input.

  If it's an np.ndarray, copies from host memory to device memory. If it's a
  jax.ShapedArray, creates a jnp.zeros() of the appropriate shape in device
  memory. If it's a tensorstore.Spec, fetches the data from tensorstore to
  device memory using JAX or Pathways, as appropriate for the current JAX
  backend.

  Args:
    x: The input array.
    sharding: The sharding to use for the array.
    expected: Expected shape and type of the output array.

  Returns:
    The array in sharded device memory.
  """
  # If it's a tensorstore spec with an array() driver, it's already in host
  # memory. Convert it to np.ndarray and use that.
  if isinstance(x, tensorstore.Spec):
    json = x.to_json()
    if json.get('driver') == 'array':
      x = tensorstore.open(x).result().read().result()

  assert x.shape == expected.shape, f'{x.shape} != {expected.shape}'

  if isinstance(x, np.ndarray) or isinstance(x, jnp.ndarray):

    def cb(i):
      return jax.lax.convert_element_type(x[i], expected.dtype)

    if jax.config.jax_array:
      return jax.make_array_from_callback(x.shape, sharding, cb)
    else:
      result = GlobalDeviceArray.from_callback(x.shape, sharding.mesh,
                                               sharding.spec, cb)
      return result  # pytype: disable=bad-return-type
  elif isinstance(x, jax.ShapedArray):

    def sharded_zeros():
      return jnp.zeros(x.shape, expected.dtype)

    with sharding.mesh:
      return pjit.pjit(
          sharded_zeros, in_axis_resources=(),
          out_axis_resources=sharding.spec)()
  elif isinstance(x, tensorstore.Spec):
    if jax.config.read('jax_xla_backend') == 'pathways':

      # Read from tensorstore using pathways.
      ts = x.to_json()
      # Further code is internal
    else:
      # Read from tensorstore using jax gda_serialization
      tensor, = jax_gda_serialization.run_deserialization([sharding], [x],
                                                          [expected.shape],
                                                          [expected.dtype])
      return tensor
  else:
    raise ValueError(f'Unsupported type: {type(x)}')


_ALLOW_UNEVEN_SHARDING = True


def _with_sharding_constraint(t,
                              spec):
  """Applies a logical sharding constraint to a tensor."""
  axes = logical_to_physical(spec)
  # First check that the sharding is equally sized on all chips. While the SPMD
  # partitioner is _designed_ to support unequal sharding on chips, in practice
  # this seems to be a fertile ground for XLA bugs such as b/245966065 and
  # possibly the underlying bug for cr/455700040. So we just ban it, and push
  # the padding complexity on the caller.
  mesh = pxla.thread_resources.env.physical_mesh
  name_to_size = dict(zip(mesh.axis_names, mesh.devices.shape))
  for size, axis in zip(t.shape, axes):
    if axis is None or axis not in name_to_size:
      continue
    axis_size = name_to_size[axis]
    assert size % axis_size == 0 or _ALLOW_UNEVEN_SHARDING, f'Uneven sharding. Shape: {t.shape}, spec: {spec}, axis: {axis}, axis size: {axis_size}'
  return pjit.with_sharding_constraint(t, axes)


def get_sharding_divisor(logical):
  """Returns how many shards will be along a given logical axis."""
  sharding_axis = logical_to_physical(logical)
  if sharding_axis == P(None,):
    sharding_axis_size = 1
  else:
    sharding_axis_size = np.prod([
        jax.lax.psum(1, a) for a in sharding_axis])
  return sharding_axis_size
