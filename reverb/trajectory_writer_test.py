# Copyright 2019 DeepMind Technologies Limited.
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

"""Tests for reverb.trajectory_writer."""

import copy
from typing import Optional
from unittest import mock

from absl.testing import absltest
from absl.testing import parameterized
import numpy as np
from reverb import client as client_lib
from reverb import errors
from reverb import pybind
from reverb import server as server_lib
from reverb import trajectory_writer
import tree


class FakeWeakCellRef:

  def __init__(self, data):
    self.data = data

  @property
  def shape(self):
    return np.asarray(self.data).shape

  @property
  def dtype(self):
    return np.asarray(self.data).dtype

  @property
  def expired(self):
    return False

  def numpy(self):
    return self.data


def extract_data(column: trajectory_writer._ColumnHistory):
  return [ref.data if ref else None for ref in column]


def _mock_append(x):
  return [FakeWeakCellRef(y) if y is not None else None for y in x]


class TrajectoryWriterTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()

    self.items = []

    def _mock_create_item(unused_table, unused_priority, refs_per_col,
                          squeeze_per_col):
      item = []
      for refs, squeeze in zip(refs_per_col, squeeze_per_col):
        if squeeze:
          item.append(refs[0].data)
        else:
          item.append(tuple([ref.data for ref in refs]))
      self.items.append(tuple(item))

    self.cpp_writer_mock = mock.Mock()
    self.cpp_writer_mock.Append.side_effect = _mock_append
    self.cpp_writer_mock.AppendPartial.side_effect = _mock_append
    self.cpp_writer_mock.CreateItem.side_effect = _mock_create_item
    self.cpp_writer_mock.max_num_keep_alive_refs = 10

    self.writer = trajectory_writer.TrajectoryWriter(self.cpp_writer_mock)

  def test_history_require_append_to_be_called_before(self):
    with self.assertRaises(RuntimeError):
      _ = self.writer.history

  def test_history_contains_references_when_data_flat(self):
    self.writer.append(0)
    self.writer.append(1)
    self.writer.append(2)

    history = tree.map_structure(extract_data, self.writer.history)
    self.assertListEqual(history, [0, 1, 2])

  def test_history_contains_structured_references(self):
    self.writer.append({'observation': 1, 'first_step': True})
    self.writer.append({'action': 2, 'reward': 101})
    self.writer.append({'action': 3, 'reward': 103})

    history = self.writer.history
    mapped_history = tree.map_structure(extract_data, history)
    self.assertDictEqual(
        {
            'action': [None, 2, 3],
            'first_step': [True, None, None],
            'observation': [1, None, None],
            'reward': [None, 101, 103]
        }, mapped_history)
    for path in history:
      self.assertEqual(path, history[path]._path[0])

  def test_history_structure_evolves_with_data(self):
    self.writer.append({'x': 1, 'z': 2})
    first = tree.map_structure(extract_data, self.writer.history)
    self.assertDictEqual(first, {'x': [1], 'z': [2]})

    self.writer.append({'z': 3, 'y': 4})
    second = tree.map_structure(extract_data, self.writer.history)
    self.assertDictEqual(second, {
        'x': [1, None],
        'z': [2, 3],
        'y': [None, 4],
    })

    self.writer.append({'w': 5})
    third = tree.map_structure(extract_data, self.writer.history)
    self.assertDictEqual(
        third, {
            'x': [1, None, None],
            'z': [2, 3, None],
            'y': [None, 4, None],
            'w': [None, None, 5],
        })

    self.writer.append({'x': 6, 'w': 7})
    forth = tree.map_structure(extract_data, self.writer.history)
    self.assertDictEqual(
        forth, {
            'x': [1, None, None, 6],
            'z': [2, 3, None, None],
            'y': [None, 4, None, None],
            'w': [None, None, 5, 7],
        })

  @parameterized.named_parameters(
      ('tuple', (0,), (0, 1)),
      ('dict', {
          'x': 0
      }, {
          'x': 0,
          'y': 1
      }),
      ('list', [0], [0, 1]),
  )
  def test_append_with_more_fields(self, first_step_data, second_step_data):
    self.writer.append(first_step_data)
    self.writer.append(second_step_data)

  def test_append_forwards_flat_data_to_cpp_writer(self):
    data = {'x': 1, 'y': 2}
    self.writer.append(data)
    self.cpp_writer_mock.Append.assert_called_with(tree.flatten(data))

  def test_partial_append_appends_to_the_same_step(self):
    # Create a first step and keep it open.
    self.writer.append({'x': 1, 'z': 2}, partial_step=True)
    first = tree.map_structure(extract_data, self.writer.history)
    self.assertDictEqual(first, {'x': [1], 'z': [2]})

    # Append to the same step and keep it open.
    self.writer.append({'y': 4}, partial_step=True)
    second = tree.map_structure(extract_data, self.writer.history)
    self.assertDictEqual(second, {
        'x': [1],
        'z': [2],
        'y': [4],
    })

    # Append to the same step and close it.
    self.writer.append({'w': 5})
    third = tree.map_structure(extract_data, self.writer.history)
    self.assertDictEqual(third, {
        'x': [1],
        'z': [2],
        'y': [4],
        'w': [5],
    })

    # Append to a new step.
    self.writer.append({'w': 6})
    forth = tree.map_structure(extract_data, self.writer.history)
    self.assertDictEqual(forth, {
        'x': [1, None],
        'z': [2, None],
        'y': [4, None],
        'w': [5, 6],
    })

  def test_columns_must_not_appear_more_than_once_in_the_same_step(self):
    # Create a first step and keep it open.
    self.writer.append({'x': 1, 'z': 2}, partial_step=True)

    # Add another unseen column alongside an existing column with a None value.
    self.writer.append({'x': None, 'y': 3}, partial_step=True)

    # Provide a value for a field that has already been set in this step.
    with self.assertRaisesRegex(
        ValueError,
        r'Field \(\'x\',\) has already been set in the active step by previous '
        r'\(partial\) append call and thus must be omitted or set to None but '
        r'got: 4'):
      self.writer.append({'x': 4})

  def test_create_item_checks_type_of_leaves(self):
    self.writer.append({'x': 3, 'y': 2})
    self.writer.append({'x': 3, 'y': 2})

    # History automatically transforms data and thus should be valid.
    self.writer.create_item(
        'table',
        1.0,
        {
            'x': self.writer.history['x'][0],  # Just one step.
            'y': self.writer.history['y'][:],  # Two steps.
        })

    # But all leaves must be TrajectoryColumn.
    with self.assertRaises(TypeError):
      self.writer.create_item(
          'table', 1.0, {
              'x': self.writer.history['x'][0],
              'y': self.writer.history['y'][:].numpy(),
          })

  def test_flush_checks_block_until_num_itmes(self):
    self.writer.flush(0)
    self.writer.flush(1)
    with self.assertRaises(ValueError):
      self.writer.flush(-1)

  def test_history_can_be_indexed_by_ints(self):
    self.writer.append({'x': 1})
    self.writer.append({'x': 2})
    self.writer.append({'x': 3})

    self.writer.create_item('table', 1.0, {
        'first': self.writer.history['x'][0],
        'last': self.writer.history['x'][-1],
    })

    # Note that the columns are not tuples since the columns are squeezed.
    self.assertEqual(self.items[0], (1, 3))

  def test_history_can_be_indexed_by_slices(self):
    self.writer.append({'x': 1})
    self.writer.append({'x': 2})
    self.writer.append({'x': 3})

    self.writer.create_item(
        'table', 1.0, {
            'first_two': self.writer.history['x'][:2],
            'last_two': self.writer.history['x'][-2:],
        })

    self.assertEqual(self.items[0], ((1, 2), (2, 3)))

  def test_history_can_be_indexed_by_lists(self):
    self.writer.append({'x': 1})
    self.writer.append({'x': 2})
    self.writer.append({'x': 3})

    self.writer.create_item(
        'table', 1.0, {
            'first_and_last': self.writer.history['x'][[0, -1]],
            'permuted': self.writer.history['x'][[1, 0, 2]],
        })

    self.assertEqual(self.items[0], ((1, 3), (2, 1, 3)))

  def test_configure_uses_auto_tune_when_max_chunk_length_not_set(self):
    self.writer.append({'x': 3, 'y': 2})
    self.writer.configure(('x',), num_keep_alive_refs=2, max_chunk_length=None)
    self.cpp_writer_mock.ConfigureChunker.assert_called_with(
        0,
        pybind.AutoTunedChunkerOptions(
            num_keep_alive_refs=2, throughput_weight=1.0))

  def test_configure_seen_column(self):
    self.writer.append({'x': 3, 'y': 2})
    self.writer.configure(('x',), num_keep_alive_refs=2, max_chunk_length=1)
    self.cpp_writer_mock.ConfigureChunker.assert_called_with(
        0,
        pybind.ConstantChunkerOptions(
            num_keep_alive_refs=2, max_chunk_length=1))

  def test_configure_unseen_column(self):
    self.writer.append({'x': 3, 'y': 2})
    self.writer.configure(('z',), num_keep_alive_refs=2, max_chunk_length=1)

    # The configure call should be delayed until the column has been observed.
    self.cpp_writer_mock.ConfigureChunker.assert_not_called()

    # Still not seen.
    self.writer.append({'a': 4})
    self.cpp_writer_mock.ConfigureChunker.assert_not_called()

    self.writer.append({'z': 5})
    self.cpp_writer_mock.ConfigureChunker.assert_called_with(
        3,
        pybind.ConstantChunkerOptions(
            num_keep_alive_refs=2, max_chunk_length=1))

  @parameterized.parameters(
      (1, None, True),
      (0, None, False),
      (-1, None, False),
      (1, 1, True),
      (1, 0, False),
      (1, -1, False),
      (5, 5, True),
      (4, 5, False),
  )
  def test_configure_validates_params(self, num_keep_alive_refs: int,
                                      max_chunk_length: Optional[int],
                                      valid: bool):
    if valid:
      self.writer.configure(('a',),
                            num_keep_alive_refs=num_keep_alive_refs,
                            max_chunk_length=max_chunk_length)
    else:
      with self.assertRaises(ValueError):
        self.writer.configure(('a',),
                              num_keep_alive_refs=num_keep_alive_refs,
                              max_chunk_length=max_chunk_length)

  def test_episode_steps(self):
    server = server_lib.Server([server_lib.Table.queue('queue', 1)])
    client = client_lib.Client(f'localhost:{server.port}')
    writer = client.trajectory_writer(num_keep_alive_refs=1)

    for _ in range(10):
      # Every episode, including the first, should start at zero.
      self.assertEqual(writer.episode_steps, 0)

      for i in range(1, 21):
        writer.append({'x': 3, 'y': 2})

        # Step count should increment with each append call.
        self.assertEqual(writer.episode_steps, i)

      # Ending the episode should reset the step count to zero.
      writer.end_episode()

  def test_episode_steps_partial_step(self):
    server = server_lib.Server([server_lib.Table.queue('queue', 1)])
    client = client_lib.Client(f'localhost:{server.port}')
    writer = client.trajectory_writer(num_keep_alive_refs=1)

    for _ in range(3):
      # Every episode, including the first, should start at zero.
      self.assertEqual(writer.episode_steps, 0)

      for i in range(1, 4):
        writer.append({'x': 3}, partial_step=True)

        # Step count should not increment on partial append calls.
        self.assertEqual(writer.episode_steps, i - 1)

        writer.append({'y': 2})

        # Step count should increment after the unqualified append call.
        self.assertEqual(writer.episode_steps, i)

      # Ending the episode should reset the step count to zero.
      writer.end_episode()

  @parameterized.parameters(True, False)
  def test_episode_steps_reset_on_end_episode(self, clear_buffers: bool):
    server = server_lib.Server([server_lib.Table.queue('queue', 1)])
    client = client_lib.Client(f'localhost:{server.port}')

    # Create a writer and check that the counter starts at 0.
    writer = client.trajectory_writer(num_keep_alive_refs=1)
    self.assertEqual(writer.episode_steps, 0)

    # Append a step and check that the counter is incremented.
    writer.append([1])
    self.assertEqual(writer.episode_steps, 1)

    # End the episode and check the counter is reset.
    writer.end_episode(clear_buffers=clear_buffers)
    self.assertEqual(writer.episode_steps, 0)

  def test_exit_does_not_flush_on_reverb_error(self):
    # If there are no errors then flush should be called.
    with mock.patch.object(self.writer, 'flush') as flush_mock:
      with self.writer:
        pass

      flush_mock.assert_called_once()

    # It flush if unrelated errors are encountered
    with mock.patch.object(self.writer, 'flush') as flush_mock:
      with self.assertRaises(ValueError):
        with self.writer:
          raise ValueError('Test')

      flush_mock.assert_called_once()

    # But it should not flush if Reverb raises the error.
    with mock.patch.object(self.writer, 'flush') as flush_mock:
      with self.assertRaises(errors.ReverbError):
        with self.writer:
          raise errors.DeadlineExceededError('Test')

      flush_mock.assert_not_called()

  def test_timeout_on_flush(self):
    server = server_lib.Server([server_lib.Table.queue('queue', 1)])
    client = client_lib.Client(f'localhost:{server.port}')

    writer = client.trajectory_writer(num_keep_alive_refs=1)
    writer.append([1])

    # Table has space for one item, up to 2 more items can be queued in
    # table worker queues.
    # Since there isn't space for all 4 items flush should time out.
    with self.assertRaises(errors.DeadlineExceededError):
      for _ in range(4):
        writer.create_item('queue', 1.0, writer.history[0][:])
        writer.flush(timeout_ms=1)

    writer.close()
    server.stop()

  def test_timeout_on_end_episode(self):
    server = server_lib.Server([server_lib.Table.queue('queue', 1)])
    client = client_lib.Client(f'localhost:{server.port}')

    writer = client.trajectory_writer(num_keep_alive_refs=1)
    writer.append([1])

    # Table has space for one item, up to 2 more items can be queued in
    # table worker queues.
    # Since there isn't space for all 4 items end_episode should time out.
    with self.assertRaises(errors.DeadlineExceededError):
      for _ in range(4):
        writer.create_item('queue', 1.0, writer.history[0][:])
        writer.end_episode(clear_buffers=False, timeout_ms=1)

    writer.close()
    server.stop()


class TrajectoryColumnTest(parameterized.TestCase):

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls._server = server_lib.Server([server_lib.Table.queue('queue', 100)])

  def setUp(self):
    super().setUp()
    self.client = client_lib.Client(f'localhost:{self._server.port}')

  @classmethod
  def tearDownClass(cls):
    super().tearDownClass()
    cls._server.stop()

  def test_dtype_of_columns_are_validated(self):
    writer = self.client.trajectory_writer(num_keep_alive_refs=10)

    # Define the structure with the first append.
    data = {
        'scalar': 1,
        'nest': {
            'sub': 1.0,
            'sub_list': [1, 2, 3],
        },
    }
    writer.append(data)

    # Modify the type of columns and check that the error message references the
    # correct path.
    with self.assertRaisesRegex(
        ValueError,
        r'Tensor of wrong dtype provided for column \(\'scalar\',\)\. Got '
        r'double but expected int64\.'):
      bad_scalar = copy.deepcopy(data)
      bad_scalar['scalar'] = 1.0
      writer.append(bad_scalar)

    with self.assertRaisesRegex(
        ValueError,
        r'Tensor of wrong dtype provided for column \(\'nest\', \'sub\'\)\. Got '
        r'int64 but expected double\.'):
      bad_nest_scalar = copy.deepcopy(data)
      bad_nest_scalar['nest']['sub'] = 1
      writer.append(bad_nest_scalar)

    with self.assertRaisesRegex(
        ValueError,
        r'Tensor of wrong dtype provided for column '
        r'\(\'nest\', \'sub_list\', 1\)\. Got double but expected int64\.'):
      bad_nest_list = copy.deepcopy(data)
      bad_nest_list['nest']['sub_list'][1] = 2.0
      writer.append(bad_nest_list)

  def test_shapes_of_columns_are_validated(self):
    writer = self.client.trajectory_writer(num_keep_alive_refs=10)

    # Define the structure with the first append.
    data = {
        'scalar': 1,
        'nest': {
            'sub': np.array([1.0, 2.0]),
            'sub_list': [
                np.arange(3),
                np.arange(3),
                np.arange(3),
            ],
        },
    }
    writer.append(data)

    # Modify the shapes of columns and check that the error message references
    # the correct path.
    with self.assertRaisesRegex(
        ValueError,
        r'Tensor of incompatible shape provided for column \(\'scalar\',\)\. '
        r'Got \[2\] which is incompatible with \[\]\.'):
      bad_scalar = copy.deepcopy(data)
      bad_scalar['scalar'] = np.arange(2)
      writer.append(bad_scalar)

    with self.assertRaisesRegex(
        ValueError,
        r'Tensor of incompatible shape provided for column '
        r'\(\'nest\', \'sub\'\)\. Got \[1,2\] which is incompatible with '
        r'\[2\]\.'):
      bad_nest = copy.deepcopy(data)
      bad_nest['nest']['sub'] = data['nest']['sub'].reshape([1, 2])
      writer.append(bad_nest)

    with self.assertRaisesRegex(
        ValueError,
        r'Tensor of incompatible shape provided for column '
        r'\(\'nest\', \'sub_list\', 0\)\. Got \[10\] which is incompatible '
        r'with \[3\]\.'):
      bad_nest_list = copy.deepcopy(data)
      bad_nest_list['nest']['sub_list'][0] = np.arange(10)
      writer.append(bad_nest_list)

  def test_numpy(self):
    writer = self.client.trajectory_writer(num_keep_alive_refs=10)

    for i in range(10):
      writer.append({'a': i, 'b': np.ones([3, 3], np.float) * i})

      np.testing.assert_array_equal(writer.history['a'][:].numpy(),
                                    np.arange(i + 1, dtype=np.int64))

      np.testing.assert_array_equal(
          writer.history['b'][:].numpy(),
          np.stack([np.ones([3, 3], np.float) * x for x in range(i + 1)]))

  def test_numpy_squeeze(self):
    writer = self.client.trajectory_writer(num_keep_alive_refs=10)

    for i in range(10):
      writer.append({'a': i})
      self.assertEqual(writer.history['a'][-1].numpy(), i)

  def test_validates_squeeze(self):
    # Exactly one is valid.
    trajectory_writer.TrajectoryColumn([FakeWeakCellRef(1)], squeeze=True)

    # Zero is not fine.
    with self.assertRaises(ValueError):
      trajectory_writer.TrajectoryColumn([], squeeze=True)

    # Neither is two (or more).
    with self.assertRaises(ValueError):
      trajectory_writer.TrajectoryColumn(
          [FakeWeakCellRef(1), FakeWeakCellRef(2)], squeeze=True)

  def test_len(self):
    for i in range(1, 10):
      column = trajectory_writer.TrajectoryColumn([FakeWeakCellRef(1)] * i)
      self.assertLen(column, i)

  def test_none_raises(self):
    with self.assertRaisesRegex(ValueError, r'cannot contain any None'):
      trajectory_writer.TrajectoryColumn([None])

    with self.assertRaisesRegex(ValueError, r'cannot contain any None'):
      trajectory_writer.TrajectoryColumn([FakeWeakCellRef(1), None])

  @parameterized.named_parameters(
      ('int', 0),
      ('float', 1.0),
      ('bool', True),
      ('np ()', np.zeros(())),
      ('np (1)', np.zeros((1))),
      ('np (1, 1)', np.zeros((1, 1))),
      ('np (3, 4, 2)', np.zeros((3, 4, 2))),
  )
  def test_shape(self, data):
    expected_shape = np.asarray(data).shape
    for i in range(1, 10):
      column = trajectory_writer.TrajectoryColumn([FakeWeakCellRef(data)] * i)
      self.assertEqual(column.shape, (i, *expected_shape))

  def test_shape_squeezed(self):
    expected_shape = (2, 5)
    data = np.arange(10).reshape(*expected_shape)
    column = trajectory_writer.TrajectoryColumn([FakeWeakCellRef(data)],
                                                squeeze=True)
    self.assertEqual(column.shape, expected_shape)

  @parameterized.named_parameters(
      ('int', 0),
      ('float', 1.0),
      ('bool', True),
      ('np_float16', np.zeros(shape=(), dtype=np.float16)),
      ('np_float32', np.zeros(shape=(), dtype=np.float32)),
      ('np_float64', np.zeros(shape=(), dtype=np.float64)),
      ('np_int8', np.zeros(shape=(), dtype=np.int8)),
      ('np_int16', np.zeros(shape=(), dtype=np.int16)),
      ('np_int32', np.zeros(shape=(), dtype=np.int32)),
      ('np_int64', np.zeros(shape=(), dtype=np.int64)),
      ('np_uint8', np.zeros(shape=(), dtype=np.uint8)),
      ('np_uint16', np.zeros(shape=(), dtype=np.uint16)),
      ('np_uint32', np.zeros(shape=(), dtype=np.uint32)),
      ('np_uint64', np.zeros(shape=(), dtype=np.uint64)),
      ('np_complex64', np.zeros(shape=(), dtype=np.complex64)),
      ('np_complex128', np.zeros(shape=(), dtype=np.complex128)),
      ('np_bool', np.zeros(shape=(), dtype=np.bool)),
      ('np_object', np.zeros(shape=(), dtype=np.object)),
  )
  def test_dtype(self, data):
    expected_dtype = np.asarray(data).dtype
    column = trajectory_writer.TrajectoryColumn([FakeWeakCellRef(data)])
    self.assertEqual(column.dtype, expected_dtype)


if __name__ == '__main__':
  absltest.main()
