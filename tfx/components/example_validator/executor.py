# Copyright 2019 Google LLC. All Rights Reserved.
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
"""Generic TFX example_validator executor."""

import os
from typing import Any, Dict, List

from absl import logging
import tensorflow_data_validation as tfdv
from tfx import types
from tfx.components.example_validator import labels
from tfx.components.statistics_gen import stats_artifact_utils
from tfx.components.util import value_utils
from tfx.dsl.components.base import base_executor
from tfx.types import artifact_utils
from tfx.types import standard_component_specs
from tfx.utils import io_utils
from tfx.utils import json_utils
from tfx.utils import writer_utils

from tensorflow_metadata.proto.v0 import anomalies_pb2

# Default file name for anomalies output.
DEFAULT_FILE_NAME = 'SchemaDiff.pb'

# Keys for artifact (custom) properties.
ARTIFACT_PROPERTY_BLESSED_KEY = 'blessed'

# Values for blessing results.
BLESSED_VALUE = 1
NOT_BLESSED_VALUE = 0


class Executor(base_executor.BaseExecutor):
  """TensorFlow ExampleValidator component executor."""

  def Do(self, input_dict: Dict[str, List[types.Artifact]],
         output_dict: Dict[str, List[types.Artifact]],
         exec_properties: Dict[str, Any]) -> None:
    """TensorFlow ExampleValidator executor entrypoint.

    This validates statistics against the schema.

    Args:
      input_dict: Input dict from input key to a list of artifacts, including:
        - statistics: A list of type `standard_artifacts.ExampleStatistics`
          generated by StatisticsGen.
        - schema: A list of type `standard_artifacts.Schema` which should
          contain a single schema artifact.
      output_dict: Output dict from key to a list of artifacts, including:
        - output: A list of 'standard_artifacts.ExampleAnomalies' of size one.
          It will include a single binary proto file which contains all
          anomalies found.
      exec_properties: A dict of execution properties.
        - exclude_splits: JSON-serialized list of names of splits that the
          example validator should not validate.
        - custom_validation_config: An optional configuration for specifying
          custom validations with SQL.

    Returns:
      None
    """
    self._log_startup(input_dict, output_dict, exec_properties)

    # Load and deserialize exclude splits from execution properties.
    exclude_splits = json_utils.loads(
        exec_properties.get(standard_component_specs.EXCLUDE_SPLITS_KEY,
                            'null')) or []
    if not isinstance(exclude_splits, list):
      raise ValueError('exclude_splits in execution properties needs to be a '
                       'list. Got %s instead.' % type(exclude_splits))
    # Setup output splits.
    stats_artifact = artifact_utils.get_single_instance(
        input_dict[standard_component_specs.STATISTICS_KEY])
    stats_split_names = artifact_utils.decode_split_names(
        stats_artifact.split_names)
    split_names = [
        split for split in stats_split_names if split not in exclude_splits
    ]
    anomalies_artifact = artifact_utils.get_single_instance(
        output_dict[standard_component_specs.ANOMALIES_KEY])
    anomalies_artifact.split_names = artifact_utils.encode_split_names(
        split_names)

    schema = io_utils.SchemaReader().read(
        io_utils.get_only_uri_in_dir(
            artifact_utils.get_single_uri(
                input_dict[standard_component_specs.SCHEMA_KEY])))

    blessed_value_dict = {}
    for split in artifact_utils.decode_split_names(stats_artifact.split_names):
      if split in exclude_splits:
        continue

      logging.info(
          'Validating schema against the computed statistics for '
          'split %s.', split)
      stats = stats_artifact_utils.load_statistics(stats_artifact,
                                                   split).proto()
      label_inputs = {
          standard_component_specs.STATISTICS_KEY:
              stats,
          standard_component_specs.SCHEMA_KEY:
              schema,
          standard_component_specs.CUSTOM_VALIDATION_CONFIG_KEY:
              exec_properties.get(
                  standard_component_specs.CUSTOM_VALIDATION_CONFIG_KEY),
      }
      output_uri = artifact_utils.get_split_uri(
          output_dict[standard_component_specs.ANOMALIES_KEY], split)
      label_outputs = {labels.SCHEMA_DIFF_PATH: output_uri}

      anomalies = self._Validate(label_inputs, label_outputs)
      if anomalies.anomaly_info or anomalies.HasField('dataset_anomaly_info'):
        blessed_value_dict[split] = NOT_BLESSED_VALUE
      else:
        blessed_value_dict[split] = BLESSED_VALUE

      logging.info(
          'Validation complete for split %s. Anomalies written to '
          '%s.', split, output_uri)

      # Set blessed custom property for anomalies artifact.
      anomalies_artifact.set_json_value_custom_property(
          ARTIFACT_PROPERTY_BLESSED_KEY, blessed_value_dict
      )

  def _Validate(
      self, inputs: Dict[str, Any], outputs: Dict[str, Any]
  ) -> anomalies_pb2.Anomalies:
    """Validate the inputs and put validate result into outputs.

      This is the implementation part of example validator executor. This is
      intended for using or extending the executor without artifact dependecy.

    Args:
      inputs: A dictionary of labeled input values, including:
        - STATISTICS_KEY: the feature statistics to validate
        - SCHEMA_KEY: the schema to respect
        - CUSTOM_VALIDATION_CONFIG: an optional config for specifying SQL-based
          custom validations.
        - (Optional) labels.ENVIRONMENT: if an environment is specified, only
          validate the feature statistics of the fields in that environment.
          Otherwise, validate all fields.
        - (Optional) labels.PREV_SPAN_FEATURE_STATISTICS: the feature
          statistics of a previous span.
        - (Optional) labels.PREV_VERSION_FEATURE_STATISTICS: the feature
          statistics of a previous version.
        - (Optional) labels.FEATURES_NEEDED: the feature needed to be
          validated on.
        - (Optional) labels.VALIDATION_CONFIG: the configuration of this
          validation.
        - (Optional) labels.EXTERNAL_CONFIG_VERSION: the version number of
          external config file.
      outputs: A dictionary of labeled output values, including:
          - labels.SCHEMA_DIFF_PATH: the path to write the schema diff to

    Returns:
      An Anomalies proto containing anomalies for the split.
    """
    schema = value_utils.GetSoleValue(inputs,
                                      standard_component_specs.SCHEMA_KEY)
    stats = value_utils.GetSoleValue(inputs,
                                     standard_component_specs.STATISTICS_KEY)
    schema_diff_path = value_utils.GetSoleValue(
        outputs, labels.SCHEMA_DIFF_PATH)
    custom_validation_config = value_utils.GetSoleValue(
        inputs, standard_component_specs.CUSTOM_VALIDATION_CONFIG_KEY)
    anomalies = tfdv.validate_statistics(
        statistics=stats,
        schema=schema,
        custom_validation_config=custom_validation_config)
    writer_utils.write_anomalies(
        os.path.join(schema_diff_path, DEFAULT_FILE_NAME), anomalies
    )
    return anomalies
