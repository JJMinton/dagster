from unittest.mock import Mock, MagicMock
from dagster import Failure, solid, pipeline, ModeDefinition, ResourceDefinition, execute_pipeline, resource
from dagster.core.execution.context.hook import build_hook_context

from dagster_mlflow.hooks import _is_last_solid, end_mlflow_run_on_pipeline_finished
import pytest


@pytest.fixture
def mock_solids():
    return [Mock(name="solid_1"), Mock(name="solid_2")]


@pytest.fixture(scope="function")
def mock_context(mock_solids):
    mock_context = Mock()
    mock_context._step_execution_context.pipeline_def.solids_in_topological_order = (
        mock_solids  # pylint: disable=protected-access
    )
    return mock_context


def test_is_last_solid(mock_solids, mock_context):

    # Check positive case
    mock_context.solid = mock_solids[-1]
    assert _is_last_solid(mock_context)

    # Check negative case
    mock_context.reset_mock()
    mock_context.solid = mock_solids[0]
    assert not _is_last_solid(mock_context)


def test_end_mlflow_run_on_pipeline_finished_no_events():
    mlflow_mock = Mock()
    with build_hook_context(resources={"mlflow": mlflow_mock}) as dummy_context:
        result = end_mlflow_run_on_pipeline_finished(dummy_context, [])
        assert result.is_skipped == True


def test_end_mlflow_run_on_pipeline_finished_success():
    mlflow_mock = MagicMock()
    @resource
    def mlflow_mock_resource():
        return mlflow_mock

    @solid
    def dummy_solid():
        pass

    @end_mlflow_run_on_pipeline_finished
    @pipeline(mode_defs=[ModeDefinition(resource_defs={"mlflow": mlflow_mock_resource})])
    def dummy_pipeline():
        dummy_solid()
  
    execute_pipeline(dummy_pipeline, mode="default")
    mlflow_mock.end_run.assert_called_once()