from dagster.core.definitions.decorators.hook import event_list_hook
from dagster.core.definitions.events import HookExecutionResult
from dagster.core.execution.context.hook import HookContext
from mlflow.entities.run_status import RunStatus


@event_list_hook(required_resource_keys={"mlflow"})
def end_mlflow_run_on_pipeline_finished(context: HookContext, event_list) -> HookExecutionResult:
    for event in event_list:
        print(event)
        if event.is_step_success and _is_last_solid(context):
            context.resources.mlflow.end_run()
            return HookExecutionResult(hook_name="mlflow", is_skipped=False)
        elif event.is_step_failure:
            context.resources.mlflow.end_run(status=RunStatus.to_string(RunStatus.FAILED))
            return HookExecutionResult(hook_name="mlflow", is_skipped=False)
    return HookExecutionResult(hook_name="mlflow", is_skipped=True)


def _is_last_solid(context):
    """
    Checks if the current solid in the context is the last solid in the pipeline.
    """
    last_solid_name = context._step_execution_context.pipeline_def.solids_in_topological_order[  # pylint: disable=protected-access
        -1
    ].name

    return context.solid.name == last_solid_name
