"""Structured representations of system events."""
import logging
import os
from enum import Enum
from typing import TYPE_CHECKING, AbstractSet, Any, Dict, List, NamedTuple, Optional, Union, cast

from dagster import check
from dagster.core.definitions import (
    AssetKey,
    AssetMaterialization,
    EventMetadataEntry,
    ExpectationResult,
    HookDefinition,
    Materialization,
    NodeHandle,
)
from dagster.core.definitions.events import AssetLineageInfo, ObjectStoreOperationType
from dagster.core.errors import DagsterError, HookExecutionError
from dagster.core.execution.context.hook import HookContext
from dagster.core.execution.context.system import (
    IPlanContext,
    IStepContext,
    PlanExecutionContext,
    PlanOrchestrationContext,
    StepExecutionContext,
)
from dagster.core.execution.plan.handle import ResolvedFromDynamicStepHandle, StepHandle
from dagster.core.execution.plan.outputs import StepOutputData
from dagster.core.log_manager import DagsterLogManager
from dagster.core.storage.pipeline_run import PipelineRunStatus
from dagster.serdes import register_serdes_tuple_fallbacks, whitelist_for_serdes
from dagster.utils.error import SerializableErrorInfo, serializable_error_info_from_exc_info
from dagster.utils.timing import format_duration

if TYPE_CHECKING:
    from dagster.core.execution.plan.plan import ExecutionPlan
    from dagster.core.execution.plan.step import ExecutionStep, StepKind
    from dagster.core.execution.plan.inputs import StepInputData
    from dagster.core.execution.plan.objects import (
        StepSuccessData,
        StepFailureData,
        StepRetryData,
    )
    from dagster.core.definitions.events import ObjectStoreOperation

    EventSpecificData = Union[
        StepOutputData,
        StepFailureData,
        StepSuccessData,
        "StepMaterializationData",
        "StepExpectationResultData",
        StepInputData,
        "EngineEventData",
        "HookErroredData",
        StepRetryData,
        "PipelineFailureData",
        "PipelineCanceledData",
        "ObjectStoreOperationResultData",
        "HandledOutputData",
        "LoadedInputData",
        "ComputeLogsCaptureData",
    ]


class DagsterEventType(Enum):
    """The types of events that may be yielded by solid and pipeline execution."""

    STEP_OUTPUT = "STEP_OUTPUT"
    STEP_INPUT = "STEP_INPUT"
    STEP_FAILURE = "STEP_FAILURE"
    STEP_START = "STEP_START"
    STEP_SUCCESS = "STEP_SUCCESS"
    STEP_SKIPPED = "STEP_SKIPPED"

    STEP_UP_FOR_RETRY = "STEP_UP_FOR_RETRY"  # "failed" but want to retry
    STEP_RESTARTED = "STEP_RESTARTED"

    ASSET_MATERIALIZATION = "ASSET_MATERIALIZATION"
    STEP_EXPECTATION_RESULT = "STEP_EXPECTATION_RESULT"

    PIPELINE_ENQUEUED = "PIPELINE_ENQUEUED"
    PIPELINE_DEQUEUED = "PIPELINE_DEQUEUED"
    PIPELINE_STARTING = "PIPELINE_STARTING"  # Launch is happening, execution hasn't started yet

    PIPELINE_START = "PIPELINE_START"  # Execution has started
    PIPELINE_SUCCESS = "PIPELINE_SUCCESS"
    PIPELINE_FAILURE = "PIPELINE_FAILURE"

    PIPELINE_CANCELING = "PIPELINE_CANCELING"
    PIPELINE_CANCELED = "PIPELINE_CANCELED"

    OBJECT_STORE_OPERATION = "OBJECT_STORE_OPERATION"
    ASSET_STORE_OPERATION = "ASSET_STORE_OPERATION"
    LOADED_INPUT = "LOADED_INPUT"
    HANDLED_OUTPUT = "HANDLED_OUTPUT"

    ENGINE_EVENT = "ENGINE_EVENT"

    HOOK_COMPLETED = "HOOK_COMPLETED"
    HOOK_ERRORED = "HOOK_ERRORED"
    HOOK_SKIPPED = "HOOK_SKIPPED"

    ALERT_START = "ALERT_START"
    ALERT_SUCCESS = "ALERT_SUCCESS"
    LOGS_CAPTURED = "LOGS_CAPTURED"


STEP_EVENTS = {
    DagsterEventType.STEP_INPUT,
    DagsterEventType.STEP_START,
    DagsterEventType.STEP_OUTPUT,
    DagsterEventType.STEP_FAILURE,
    DagsterEventType.STEP_SUCCESS,
    DagsterEventType.STEP_SKIPPED,
    DagsterEventType.ASSET_MATERIALIZATION,
    DagsterEventType.STEP_EXPECTATION_RESULT,
    DagsterEventType.OBJECT_STORE_OPERATION,
    DagsterEventType.HANDLED_OUTPUT,
    DagsterEventType.LOADED_INPUT,
    DagsterEventType.STEP_RESTARTED,
    DagsterEventType.STEP_UP_FOR_RETRY,
}

FAILURE_EVENTS = {
    DagsterEventType.PIPELINE_FAILURE,
    DagsterEventType.STEP_FAILURE,
    DagsterEventType.PIPELINE_CANCELED,
}

PIPELINE_EVENTS = {
    DagsterEventType.PIPELINE_ENQUEUED,
    DagsterEventType.PIPELINE_DEQUEUED,
    DagsterEventType.PIPELINE_STARTING,
    DagsterEventType.PIPELINE_START,
    DagsterEventType.PIPELINE_SUCCESS,
    DagsterEventType.PIPELINE_FAILURE,
    DagsterEventType.PIPELINE_CANCELING,
    DagsterEventType.PIPELINE_CANCELED,
}

HOOK_EVENTS = {
    DagsterEventType.HOOK_COMPLETED,
    DagsterEventType.HOOK_ERRORED,
    DagsterEventType.HOOK_SKIPPED,
}

ALERT_EVENTS = {
    DagsterEventType.ALERT_START,
    DagsterEventType.ALERT_SUCCESS,
}


EVENT_TYPE_TO_PIPELINE_RUN_STATUS = {
    DagsterEventType.PIPELINE_START: PipelineRunStatus.STARTED,
    DagsterEventType.PIPELINE_SUCCESS: PipelineRunStatus.SUCCESS,
    DagsterEventType.PIPELINE_FAILURE: PipelineRunStatus.FAILURE,
    DagsterEventType.PIPELINE_ENQUEUED: PipelineRunStatus.QUEUED,
    DagsterEventType.PIPELINE_STARTING: PipelineRunStatus.STARTING,
    DagsterEventType.PIPELINE_CANCELING: PipelineRunStatus.CANCELING,
    DagsterEventType.PIPELINE_CANCELED: PipelineRunStatus.CANCELED,
}

PIPELINE_RUN_STATUS_TO_EVENT_TYPE = {v: k for k, v in EVENT_TYPE_TO_PIPELINE_RUN_STATUS.items()}


def _assert_type(
    method: str, expected_type: DagsterEventType, actual_type: DagsterEventType
) -> None:
    check.invariant(
        expected_type == actual_type,
        (
            "{method} only callable when event_type is {expected_type}, called on {actual_type}"
        ).format(method=method, expected_type=expected_type, actual_type=actual_type),
    )


def _validate_event_specific_data(
    event_type: DagsterEventType, event_specific_data: Optional["EventSpecificData"]
) -> Optional["EventSpecificData"]:
    from dagster.core.execution.plan.objects import StepFailureData, StepSuccessData
    from dagster.core.execution.plan.inputs import StepInputData

    if event_type == DagsterEventType.STEP_OUTPUT:
        check.inst_param(event_specific_data, "event_specific_data", StepOutputData)
    elif event_type == DagsterEventType.STEP_FAILURE:
        check.inst_param(event_specific_data, "event_specific_data", StepFailureData)
    elif event_type == DagsterEventType.STEP_SUCCESS:
        check.inst_param(event_specific_data, "event_specific_data", StepSuccessData)
    elif event_type == DagsterEventType.ASSET_MATERIALIZATION:
        check.inst_param(event_specific_data, "event_specific_data", StepMaterializationData)
    elif event_type == DagsterEventType.STEP_EXPECTATION_RESULT:
        check.inst_param(event_specific_data, "event_specific_data", StepExpectationResultData)
    elif event_type == DagsterEventType.STEP_INPUT:
        check.inst_param(event_specific_data, "event_specific_data", StepInputData)
    elif event_type == DagsterEventType.ENGINE_EVENT:
        check.inst_param(event_specific_data, "event_specific_data", EngineEventData)
    elif event_type == DagsterEventType.HOOK_ERRORED:
        check.inst_param(event_specific_data, "event_specific_data", HookErroredData)

    return event_specific_data


def log_step_event(step_context: IStepContext, event: "DagsterEvent") -> None:
    event_type = DagsterEventType(event.event_type_value)
    log_level = logging.ERROR if event_type in FAILURE_EVENTS else logging.DEBUG

    step_context.log.log_dagster_event(
        level=log_level,
        msg=event.message or f"{event_type} for step {step_context.step.key}",
        dagster_event=event,
    )


def log_pipeline_event(pipeline_context: IPlanContext, event: "DagsterEvent") -> None:
    event_type = DagsterEventType(event.event_type_value)
    log_level = logging.ERROR if event_type in FAILURE_EVENTS else logging.DEBUG

    pipeline_context.log.log_dagster_event(
        level=log_level,
        msg=event.message or f"{event_type} for pipeline {pipeline_context.pipeline_name}",
        dagster_event=event,
    )


def log_resource_event(log_manager: DagsterLogManager, event: "DagsterEvent") -> None:
    event_specific_data = cast(EngineEventData, event.event_specific_data)

    log_level = logging.ERROR if event_specific_data.error else logging.DEBUG
    log_manager.log_dagster_event(level=log_level, msg=event.message or "", dagster_event=event)


@whitelist_for_serdes
class DagsterEvent(
    NamedTuple(
        "_DagsterEvent",
        [
            ("event_type_value", str),
            ("pipeline_name", str),
            ("step_handle", Optional[Union[StepHandle, ResolvedFromDynamicStepHandle]]),
            ("solid_handle", Optional[NodeHandle]),
            ("step_kind_value", Optional[str]),
            ("logging_tags", Optional[Dict[str, str]]),
            ("event_specific_data", Optional["EventSpecificData"]),
            ("message", Optional[str]),
            ("pid", Optional[int]),
            ("step_key", Optional[str]),
        ],
    )
):
    """Events yielded by solid and pipeline execution.

    Users should not instantiate this class.

    Attributes:
        event_type_value (str): Value for a DagsterEventType.
        pipeline_name (str)
        step_key (str)
        solid_handle (NodeHandle)
        step_kind_value (str): Value for a StepKind.
        logging_tags (Dict[str, str])
        event_specific_data (Any): Type must correspond to event_type_value.
        message (str)
        pid (int)
        step_key (Optional[str]): DEPRECATED
    """

    @staticmethod
    def from_step(
        event_type: "DagsterEventType",
        step_context: IStepContext,
        event_specific_data: Optional["EventSpecificData"] = None,
        message: Optional[str] = None,
    ) -> "DagsterEvent":

        event = DagsterEvent(
            event_type_value=check.inst_param(event_type, "event_type", DagsterEventType).value,
            pipeline_name=step_context.pipeline_name,
            step_handle=step_context.step.handle,
            solid_handle=step_context.step.solid_handle,
            step_kind_value=step_context.step.kind.value,
            logging_tags=step_context.logging_tags,
            event_specific_data=_validate_event_specific_data(event_type, event_specific_data),
            message=check.opt_str_param(message, "message"),
            pid=os.getpid(),
        )

        log_step_event(step_context, event)

        return event

    @staticmethod
    def from_pipeline(
        event_type: DagsterEventType,
        pipeline_context: IPlanContext,
        message: Optional[str] = None,
        event_specific_data: Optional["EventSpecificData"] = None,
        step_handle: Optional[Union[StepHandle, ResolvedFromDynamicStepHandle]] = None,
    ) -> "DagsterEvent":
        check.opt_inst_param(
            step_handle, "step_handle", (StepHandle, ResolvedFromDynamicStepHandle)
        )

        event = DagsterEvent(
            event_type_value=check.inst_param(event_type, "event_type", DagsterEventType).value,
            pipeline_name=pipeline_context.pipeline_name,
            message=check.opt_str_param(message, "message"),
            event_specific_data=_validate_event_specific_data(event_type, event_specific_data),
            step_handle=step_handle,
            pid=os.getpid(),
        )

        log_pipeline_event(pipeline_context, event)

        return event

    @staticmethod
    def from_resource(
        pipeline_name: str,
        execution_plan: "ExecutionPlan",
        log_manager: DagsterLogManager,
        message: Optional[str] = None,
        event_specific_data: Optional["EngineEventData"] = None,
    ) -> "DagsterEvent":

        event = DagsterEvent(
            DagsterEventType.ENGINE_EVENT.value,
            pipeline_name=pipeline_name,
            message=check.opt_str_param(message, "message"),
            event_specific_data=_validate_event_specific_data(
                DagsterEventType.ENGINE_EVENT, event_specific_data
            ),
            step_handle=execution_plan.step_handle_for_single_step_plans(),
            pid=os.getpid(),
        )
        log_resource_event(log_manager, event)
        return event

    def __new__(
        cls,
        event_type_value: str,
        pipeline_name: str,
        step_handle: Optional[Union[StepHandle, ResolvedFromDynamicStepHandle]] = None,
        solid_handle: Optional[NodeHandle] = None,
        step_kind_value: Optional[str] = None,
        logging_tags: Optional[Dict[str, str]] = None,
        event_specific_data: Optional["EventSpecificData"] = None,
        message: Optional[str] = None,
        pid: Optional[int] = None,
        # legacy
        step_key: Optional[str] = None,
    ):
        event_type_value, event_specific_data = _handle_back_compat(
            event_type_value, event_specific_data
        )

        # old events may contain solid_handle but not step_handle
        if solid_handle is not None and step_handle is None:
            step_handle = StepHandle(solid_handle)

        # Legacy events may have step_key set directly, preserve those to stay in sync
        # with legacy execution plan snapshots.
        if step_handle is not None and step_key is None:
            step_key = step_handle.to_key()

        return super(DagsterEvent, cls).__new__(
            cls,
            check.str_param(event_type_value, "event_type_value"),
            check.str_param(pipeline_name, "pipeline_name"),
            check.opt_inst_param(
                step_handle, "step_handle", (StepHandle, ResolvedFromDynamicStepHandle)
            ),
            check.opt_inst_param(solid_handle, "solid_handle", NodeHandle),
            check.opt_str_param(step_kind_value, "step_kind_value"),
            check.opt_dict_param(logging_tags, "logging_tags"),
            _validate_event_specific_data(DagsterEventType(event_type_value), event_specific_data),
            check.opt_str_param(message, "message"),
            check.opt_int_param(pid, "pid"),
            check.opt_str_param(step_key, "step_key"),
        )

    @property
    def solid_name(self) -> str:
        check.invariant(self.solid_handle is not None)
        solid_handle = cast(NodeHandle, self.solid_handle)
        return solid_handle.name

    @property
    def event_type(self) -> DagsterEventType:
        """DagsterEventType: The type of this event."""
        return DagsterEventType(self.event_type_value)

    @property
    def is_step_event(self) -> bool:
        return self.event_type in STEP_EVENTS

    @property
    def is_hook_event(self) -> bool:
        return self.event_type in HOOK_EVENTS

    @property
    def is_alert_event(self) -> bool:
        return self.event_type in ALERT_EVENTS

    @property
    def step_kind(self) -> "StepKind":
        from dagster.core.execution.plan.step import StepKind

        return StepKind(self.step_kind_value)

    @property
    def is_step_success(self) -> bool:
        return self.event_type == DagsterEventType.STEP_SUCCESS

    @property
    def is_successful_output(self) -> bool:
        return self.event_type == DagsterEventType.STEP_OUTPUT

    @property
    def is_step_start(self) -> bool:
        return self.event_type == DagsterEventType.STEP_START

    @property
    def is_step_failure(self) -> bool:
        return self.event_type == DagsterEventType.STEP_FAILURE

    @property
    def is_step_skipped(self) -> bool:
        return self.event_type == DagsterEventType.STEP_SKIPPED

    @property
    def is_step_up_for_retry(self) -> bool:
        return self.event_type == DagsterEventType.STEP_UP_FOR_RETRY

    @property
    def is_step_restarted(self) -> bool:
        return self.event_type == DagsterEventType.STEP_RESTARTED

    @property
    def is_pipeline_success(self) -> bool:
        return self.event_type == DagsterEventType.PIPELINE_SUCCESS

    @property
    def is_pipeline_failure(self) -> bool:
        return self.event_type == DagsterEventType.PIPELINE_FAILURE

    @property
    def is_failure(self) -> bool:
        return self.event_type in FAILURE_EVENTS

    @property
    def is_pipeline_event(self) -> bool:
        return self.event_type in PIPELINE_EVENTS

    @property
    def is_engine_event(self) -> bool:
        return self.event_type == DagsterEventType.ENGINE_EVENT

    @property
    def is_handled_output(self) -> bool:
        return self.event_type == DagsterEventType.HANDLED_OUTPUT

    @property
    def is_loaded_input(self) -> bool:
        return self.event_type == DagsterEventType.LOADED_INPUT

    @property
    def is_step_materialization(self) -> bool:
        return self.event_type == DagsterEventType.ASSET_MATERIALIZATION

    @property
    def asset_key(self) -> Optional[AssetKey]:
        if self.event_type != DagsterEventType.ASSET_MATERIALIZATION:
            return None
        return self.step_materialization_data.materialization.asset_key

    @property
    def partition(self) -> Optional[str]:
        if self.event_type != DagsterEventType.ASSET_MATERIALIZATION:
            return None
        return self.step_materialization_data.materialization.partition

    @property
    def step_input_data(self) -> "StepInputData":
        from dagster.core.execution.plan.inputs import StepInputData

        _assert_type("step_input_data", DagsterEventType.STEP_INPUT, self.event_type)
        return cast(StepInputData, self.event_specific_data)

    @property
    def step_output_data(self) -> StepOutputData:
        _assert_type("step_output_data", DagsterEventType.STEP_OUTPUT, self.event_type)
        return cast(StepOutputData, self.event_specific_data)

    @property
    def step_success_data(self) -> "StepSuccessData":
        from dagster.core.execution.plan.objects import StepSuccessData

        _assert_type("step_success_data", DagsterEventType.STEP_SUCCESS, self.event_type)
        return cast(StepSuccessData, self.event_specific_data)

    @property
    def step_failure_data(self) -> "StepFailureData":
        from dagster.core.execution.plan.objects import StepFailureData

        _assert_type("step_failure_data", DagsterEventType.STEP_FAILURE, self.event_type)
        return cast(StepFailureData, self.event_specific_data)

    @property
    def step_retry_data(self) -> "StepRetryData":
        from dagster.core.execution.plan.objects import StepRetryData

        _assert_type("step_retry_data", DagsterEventType.STEP_UP_FOR_RETRY, self.event_type)
        return cast(StepRetryData, self.event_specific_data)

    @property
    def step_materialization_data(self) -> "StepMaterializationData":
        _assert_type(
            "step_materialization_data", DagsterEventType.ASSET_MATERIALIZATION, self.event_type
        )
        return cast(StepMaterializationData, self.event_specific_data)

    @property
    def step_expectation_result_data(self) -> "StepExpectationResultData":
        _assert_type(
            "step_expectation_result_data",
            DagsterEventType.STEP_EXPECTATION_RESULT,
            self.event_type,
        )
        return cast(StepExpectationResultData, self.event_specific_data)

    @property
    def pipeline_failure_data(self) -> "PipelineFailureData":
        _assert_type("pipeline_failure_data", DagsterEventType.PIPELINE_FAILURE, self.event_type)
        return cast(PipelineFailureData, self.event_specific_data)

    @property
    def engine_event_data(self) -> "EngineEventData":
        _assert_type("engine_event_data", DagsterEventType.ENGINE_EVENT, self.event_type)
        return cast(EngineEventData, self.event_specific_data)

    @property
    def hook_completed_data(self) -> Optional["EventSpecificData"]:
        _assert_type("hook_completed_data", DagsterEventType.HOOK_COMPLETED, self.event_type)
        return self.event_specific_data

    @property
    def hook_errored_data(self) -> "HookErroredData":
        _assert_type("hook_errored_data", DagsterEventType.HOOK_ERRORED, self.event_type)
        return cast(HookErroredData, self.event_specific_data)

    @property
    def hook_skipped_data(self) -> Optional["EventSpecificData"]:
        _assert_type("hook_skipped_data", DagsterEventType.HOOK_SKIPPED, self.event_type)
        return self.event_specific_data

    @property
    def logs_captured_data(self):
        _assert_type("logs_captured_data", DagsterEventType.LOGS_CAPTURED, self.event_type)
        return self.event_specific_data

    @staticmethod
    def step_output_event(
        step_context: StepExecutionContext, step_output_data: StepOutputData
    ) -> "DagsterEvent":

        output_def = step_context.solid.output_def_named(
            step_output_data.step_output_handle.output_name
        )

        return DagsterEvent.from_step(
            event_type=DagsterEventType.STEP_OUTPUT,
            step_context=step_context,
            event_specific_data=step_output_data,
            message='Yielded output "{output_name}"{mapping_clause} of type "{output_type}".{type_check_clause}'.format(
                output_name=step_output_data.step_output_handle.output_name,
                output_type=output_def.dagster_type.display_name,
                type_check_clause=(
                    " Warning! Type check failed."
                    if not step_output_data.type_check_data.success
                    else " (Type check passed)."
                )
                if step_output_data.type_check_data
                else " (No type check).",
                mapping_clause=f' mapping key "{step_output_data.step_output_handle.mapping_key}"'
                if step_output_data.step_output_handle.mapping_key
                else "",
            ),
        )

    @staticmethod
    def step_failure_event(
        step_context: IStepContext, step_failure_data: "StepFailureData"
    ) -> "DagsterEvent":
        return DagsterEvent.from_step(
            event_type=DagsterEventType.STEP_FAILURE,
            step_context=step_context,
            event_specific_data=step_failure_data,
            message='Execution of step "{step_key}" failed.'.format(step_key=step_context.step.key),
        )

    @staticmethod
    def step_retry_event(
        step_context: IStepContext, step_retry_data: "StepRetryData"
    ) -> "DagsterEvent":
        return DagsterEvent.from_step(
            event_type=DagsterEventType.STEP_UP_FOR_RETRY,
            step_context=step_context,
            event_specific_data=step_retry_data,
            message='Execution of step "{step_key}" failed and has requested a retry{wait_str}.'.format(
                step_key=step_context.step.key,
                wait_str=" in {n} seconds".format(n=step_retry_data.seconds_to_wait)
                if step_retry_data.seconds_to_wait
                else "",
            ),
        )

    @staticmethod
    def step_input_event(
        step_context: StepExecutionContext, step_input_data: "StepInputData"
    ) -> "DagsterEvent":
        step_input = step_context.step.step_input_named(step_input_data.input_name)
        input_def = step_input.source.get_input_def(step_context.pipeline_def)

        return DagsterEvent.from_step(
            event_type=DagsterEventType.STEP_INPUT,
            step_context=step_context,
            event_specific_data=step_input_data,
            message='Got input "{input_name}" of type "{input_type}".{type_check_clause}'.format(
                input_name=step_input_data.input_name,
                input_type=input_def.dagster_type.display_name,
                type_check_clause=(
                    " Warning! Type check failed."
                    if not step_input_data.type_check_data.success
                    else " (Type check passed)."
                )
                if step_input_data.type_check_data
                else " (No type check).",
            ),
        )

    @staticmethod
    def step_start_event(step_context: IStepContext) -> "DagsterEvent":
        return DagsterEvent.from_step(
            event_type=DagsterEventType.STEP_START,
            step_context=step_context,
            message='Started execution of step "{step_key}".'.format(
                step_key=step_context.step.key
            ),
        )

    @staticmethod
    def step_restarted_event(step_context: IStepContext, previous_attempts: int) -> "DagsterEvent":
        return DagsterEvent.from_step(
            event_type=DagsterEventType.STEP_RESTARTED,
            step_context=step_context,
            message='Started re-execution (attempt # {n}) of step "{step_key}".'.format(
                step_key=step_context.step.key, n=previous_attempts + 1
            ),
        )

    @staticmethod
    def step_success_event(
        step_context: IStepContext, success: "StepSuccessData"
    ) -> "DagsterEvent":
        return DagsterEvent.from_step(
            event_type=DagsterEventType.STEP_SUCCESS,
            step_context=step_context,
            event_specific_data=success,
            message='Finished execution of step "{step_key}" in {duration}.'.format(
                step_key=step_context.step.key,
                duration=format_duration(success.duration_ms),
            ),
        )

    @staticmethod
    def step_skipped_event(step_context: IStepContext) -> "DagsterEvent":
        return DagsterEvent.from_step(
            event_type=DagsterEventType.STEP_SKIPPED,
            step_context=step_context,
            message='Skipped execution of step "{step_key}".'.format(
                step_key=step_context.step.key
            ),
        )

    @staticmethod
    def asset_materialization(
        step_context: IStepContext,
        materialization: Union[AssetMaterialization, Materialization],
        asset_lineage: List[AssetLineageInfo] = None,
    ) -> "DagsterEvent":
        return DagsterEvent.from_step(
            event_type=DagsterEventType.ASSET_MATERIALIZATION,
            step_context=step_context,
            event_specific_data=StepMaterializationData(materialization, asset_lineage),
            message=materialization.description
            if materialization.description
            else "Materialized value{label_clause}.".format(
                label_clause=" {label}".format(label=materialization.label)
                if materialization.label
                else ""
            ),
        )

    @staticmethod
    def step_expectation_result(
        step_context: IStepContext, expectation_result: ExpectationResult
    ) -> "DagsterEvent":
        def _msg():
            if expectation_result.description:
                return expectation_result.description

            return "Expectation{label_clause} {result_verb}".format(
                label_clause=" " + expectation_result.label if expectation_result.label else "",
                result_verb="passed" if expectation_result.success else "failed",
            )

        return DagsterEvent.from_step(
            event_type=DagsterEventType.STEP_EXPECTATION_RESULT,
            step_context=step_context,
            event_specific_data=StepExpectationResultData(expectation_result),
            message=_msg(),
        )

    @staticmethod
    def pipeline_start(pipeline_context: IPlanContext) -> "DagsterEvent":
        return DagsterEvent.from_pipeline(
            DagsterEventType.PIPELINE_START,
            pipeline_context,
            message='Started execution of pipeline "{pipeline_name}".'.format(
                pipeline_name=pipeline_context.pipeline_name
            ),
        )

    @staticmethod
    def pipeline_success(pipeline_context: IPlanContext) -> "DagsterEvent":
        return DagsterEvent.from_pipeline(
            DagsterEventType.PIPELINE_SUCCESS,
            pipeline_context,
            message='Finished execution of pipeline "{pipeline_name}".'.format(
                pipeline_name=pipeline_context.pipeline_name
            ),
        )

    @staticmethod
    def pipeline_failure(
        pipeline_context_or_name: Union[IPlanContext, str],
        context_msg: str,
        error_info: Optional[SerializableErrorInfo] = None,
    ) -> "DagsterEvent":
        check.str_param(context_msg, "context_msg")
        if isinstance(pipeline_context_or_name, IPlanContext):
            return DagsterEvent.from_pipeline(
                DagsterEventType.PIPELINE_FAILURE,
                pipeline_context_or_name,
                message='Execution of pipeline "{pipeline_name}" failed. {context_msg}'.format(
                    pipeline_name=pipeline_context_or_name.pipeline_name,
                    context_msg=context_msg,
                ),
                event_specific_data=PipelineFailureData(error_info),
            )
        else:
            # when the failure happens trying to bring up context, the pipeline_context hasn't been
            # built and so can't use from_pipeline
            check.str_param(pipeline_context_or_name, "pipeline_name")
            event = DagsterEvent(
                event_type_value=DagsterEventType.PIPELINE_FAILURE.value,
                pipeline_name=pipeline_context_or_name,
                event_specific_data=PipelineFailureData(error_info),
                message='Execution of pipeline "{pipeline_name}" failed. {context_msg}'.format(
                    pipeline_name=pipeline_context_or_name,
                    context_msg=context_msg,
                ),
                pid=os.getpid(),
            )
            return event

    @staticmethod
    def pipeline_canceled(
        pipeline_context: IPlanContext, error_info: Optional[SerializableErrorInfo] = None
    ) -> "DagsterEvent":
        return DagsterEvent.from_pipeline(
            DagsterEventType.PIPELINE_CANCELED,
            pipeline_context,
            message='Execution of pipeline "{pipeline_name}" canceled.'.format(
                pipeline_name=pipeline_context.pipeline_name
            ),
            event_specific_data=PipelineCanceledData(
                check.opt_inst_param(error_info, "error_info", SerializableErrorInfo)
            ),
        )

    @staticmethod
    def resource_init_start(
        pipeline_name: str,
        execution_plan: "ExecutionPlan",
        log_manager: DagsterLogManager,
        resource_keys: AbstractSet[str],
    ) -> "DagsterEvent":

        return DagsterEvent.from_resource(
            pipeline_name=pipeline_name,
            execution_plan=execution_plan,
            log_manager=log_manager,
            message="Starting initialization of resources [{}].".format(
                ", ".join(sorted(resource_keys))
            ),
            event_specific_data=EngineEventData(metadata_entries=[], marker_start="resources"),
        )

    @staticmethod
    def resource_init_success(
        pipeline_name: str,
        execution_plan: "ExecutionPlan",
        log_manager: DagsterLogManager,
        resource_instances: Dict[str, Any],
        resource_init_times: Dict[str, str],
    ) -> "DagsterEvent":

        metadata_entries = []
        for resource_key in resource_instances.keys():
            resource_obj = resource_instances[resource_key]
            resource_time = resource_init_times[resource_key]
            metadata_entries.append(
                EventMetadataEntry.python_artifact(
                    resource_obj.__class__, resource_key, "Initialized in {}".format(resource_time)
                )
            )

        return DagsterEvent.from_resource(
            pipeline_name=pipeline_name,
            execution_plan=execution_plan,
            log_manager=log_manager,
            message="Finished initialization of resources [{}].".format(
                ", ".join(sorted(resource_init_times.keys()))
            ),
            event_specific_data=EngineEventData(
                metadata_entries=metadata_entries,
                marker_end="resources",
            ),
        )

    @staticmethod
    def resource_init_failure(
        pipeline_name: str,
        execution_plan: "ExecutionPlan",
        log_manager: DagsterLogManager,
        resource_keys: AbstractSet[str],
        error: SerializableErrorInfo,
    ) -> "DagsterEvent":

        return DagsterEvent.from_resource(
            pipeline_name=pipeline_name,
            execution_plan=execution_plan,
            log_manager=log_manager,
            message="Initialization of resources [{}] failed.".format(", ".join(resource_keys)),
            event_specific_data=EngineEventData(
                metadata_entries=[],
                marker_end="resources",
                error=error,
            ),
        )

    @staticmethod
    def resource_teardown_failure(
        pipeline_name: str,
        execution_plan: "ExecutionPlan",
        log_manager: DagsterLogManager,
        resource_keys: AbstractSet[str],
        error: SerializableErrorInfo,
    ) -> "DagsterEvent":

        return DagsterEvent.from_resource(
            pipeline_name=pipeline_name,
            execution_plan=execution_plan,
            log_manager=log_manager,
            message="Teardown of resources [{}] failed.".format(", ".join(resource_keys)),
            event_specific_data=EngineEventData(
                metadata_entries=[],
                marker_start=None,
                marker_end=None,
                error=error,
            ),
        )

    @staticmethod
    def engine_event(
        pipeline_context: IPlanContext,
        message: str,
        event_specific_data: Optional["EngineEventData"] = None,
        step_handle: Optional[StepHandle] = None,
    ) -> "DagsterEvent":
        return DagsterEvent.from_pipeline(
            DagsterEventType.ENGINE_EVENT,
            pipeline_context,
            message,
            event_specific_data=event_specific_data,
            step_handle=step_handle,
        )

    @staticmethod
    def object_store_operation(
        step_context: IStepContext, object_store_operation_result: "ObjectStoreOperation"
    ) -> "DagsterEvent":

        object_store_name = (
            "{object_store_name} ".format(
                object_store_name=object_store_operation_result.object_store_name
            )
            if object_store_operation_result.object_store_name
            else ""
        )

        serialization_strategy_modifier = (
            " using {serialization_strategy_name}".format(
                serialization_strategy_name=object_store_operation_result.serialization_strategy_name
            )
            if object_store_operation_result.serialization_strategy_name
            else ""
        )

        value_name = object_store_operation_result.value_name

        if (
            ObjectStoreOperationType(object_store_operation_result.op)
            == ObjectStoreOperationType.SET_OBJECT
        ):
            message = (
                "Stored intermediate object for output {value_name} in "
                "{object_store_name}object store{serialization_strategy_modifier}."
            ).format(
                value_name=value_name,
                object_store_name=object_store_name,
                serialization_strategy_modifier=serialization_strategy_modifier,
            )
        elif (
            ObjectStoreOperationType(object_store_operation_result.op)
            == ObjectStoreOperationType.GET_OBJECT
        ):
            message = (
                "Retrieved intermediate object for input {value_name} in "
                "{object_store_name}object store{serialization_strategy_modifier}."
            ).format(
                value_name=value_name,
                object_store_name=object_store_name,
                serialization_strategy_modifier=serialization_strategy_modifier,
            )
        elif (
            ObjectStoreOperationType(object_store_operation_result.op)
            == ObjectStoreOperationType.CP_OBJECT
        ):
            message = (
                "Copied intermediate object for input {value_name} from {key} to {dest_key}"
            ).format(
                value_name=value_name,
                key=object_store_operation_result.key,
                dest_key=object_store_operation_result.dest_key,
            )
        else:
            message = ""

        return DagsterEvent.from_step(
            DagsterEventType.OBJECT_STORE_OPERATION,
            step_context,
            event_specific_data=ObjectStoreOperationResultData(
                op=object_store_operation_result.op,
                value_name=value_name,
                address=object_store_operation_result.key,
                metadata_entries=[
                    EventMetadataEntry.path(object_store_operation_result.key, label="key")
                ],
                version=object_store_operation_result.version,
                mapping_key=object_store_operation_result.mapping_key,
            ),
            message=message,
        )

    @staticmethod
    def handled_output(
        step_context: IStepContext,
        output_name: str,
        manager_key: str,
        message_override: Optional[str] = None,
        metadata_entries: Optional[List[EventMetadataEntry]] = None,
    ) -> "DagsterEvent":
        message = f'Handled output "{output_name}" using IO manager "{manager_key}"'
        return DagsterEvent.from_step(
            event_type=DagsterEventType.HANDLED_OUTPUT,
            step_context=step_context,
            event_specific_data=HandledOutputData(
                output_name=output_name,
                manager_key=manager_key,
                metadata_entries=metadata_entries if metadata_entries else [],
            ),
            message=message_override or message,
        )

    @staticmethod
    def loaded_input(
        step_context: IStepContext,
        input_name: str,
        manager_key: str,
        upstream_output_name: Optional[str] = None,
        upstream_step_key: Optional[str] = None,
        message_override: Optional[str] = None,
    ) -> "DagsterEvent":

        message = f'Loaded input "{input_name}" using input manager "{manager_key}"'
        if upstream_output_name:
            message += f', from output "{upstream_output_name}" of step ' f'"{upstream_step_key}"'

        return DagsterEvent.from_step(
            event_type=DagsterEventType.LOADED_INPUT,
            step_context=step_context,
            event_specific_data=LoadedInputData(
                input_name=input_name,
                manager_key=manager_key,
                upstream_output_name=upstream_output_name,
                upstream_step_key=upstream_step_key,
            ),
            message=message_override or message,
        )

    @staticmethod
    def hook_completed(
        step_context: StepExecutionContext, hook_def: HookDefinition
    ) -> "DagsterEvent":
        event_type = DagsterEventType.HOOK_COMPLETED

        event = DagsterEvent(
            event_type_value=event_type.value,
            pipeline_name=step_context.pipeline_name,
            step_handle=step_context.step.handle,
            solid_handle=step_context.step.solid_handle,
            step_kind_value=step_context.step.kind.value,
            logging_tags=step_context.logging_tags,
            message=(
                'Finished the execution of hook "{hook_name}" triggered for solid "{solid_name}".'
            ).format(hook_name=hook_def.name, solid_name=step_context.solid.name),
        )

        step_context.log.log_dagster_event(
            level=logging.DEBUG, msg=event.message or "", dagster_event=event
        )

        return event

    @staticmethod
    def hook_errored(
        step_context: StepExecutionContext, error: HookExecutionError
    ) -> "DagsterEvent":
        event_type = DagsterEventType.HOOK_ERRORED

        event = DagsterEvent(
            event_type_value=event_type.value,
            pipeline_name=step_context.pipeline_name,
            step_handle=step_context.step.handle,
            solid_handle=step_context.step.solid_handle,
            step_kind_value=step_context.step.kind.value,
            logging_tags=step_context.logging_tags,
            event_specific_data=_validate_event_specific_data(
                event_type,
                HookErroredData(
                    error=serializable_error_info_from_exc_info(error.original_exc_info)
                ),
            ),
        )

        step_context.log.log_dagster_event(level=logging.ERROR, msg=str(error), dagster_event=event)

        return event

    @staticmethod
    def hook_skipped(
        step_context: StepExecutionContext, hook_def: HookDefinition
    ) -> "DagsterEvent":
        event_type = DagsterEventType.HOOK_SKIPPED

        event = DagsterEvent(
            event_type_value=event_type.value,
            pipeline_name=step_context.pipeline_name,
            step_handle=step_context.step.handle,
            solid_handle=step_context.step.solid_handle,
            step_kind_value=step_context.step.kind.value,
            logging_tags=step_context.logging_tags,
            message=(
                'Skipped the execution of hook "{hook_name}". It did not meet its triggering '
                'condition during the execution of solid "{solid_name}".'
            ).format(hook_name=hook_def.name, solid_name=step_context.solid.name),
        )

        step_context.log.log_dagster_event(
            level=logging.DEBUG, msg=event.message or "", dagster_event=event
        )

        return event

    @staticmethod
    def capture_logs(pipeline_context: IPlanContext, log_key: str, steps: List["ExecutionStep"]):
        step_keys = [step.key for step in steps]
        if len(step_keys) == 1:
            message = f"Started capturing logs for solid: {step_keys[0]}."
        else:
            message = f"Started capturing logs in process (pid: {os.getpid()})."

        if isinstance(pipeline_context, StepExecutionContext):
            return DagsterEvent.from_step(
                DagsterEventType.LOGS_CAPTURED,
                pipeline_context,
                message=message,
                event_specific_data=ComputeLogsCaptureData(
                    step_keys=step_keys,
                    log_key=log_key,
                ),
            )

        return DagsterEvent.from_pipeline(
            DagsterEventType.LOGS_CAPTURED,
            pipeline_context,
            message=message,
            event_specific_data=ComputeLogsCaptureData(
                step_keys=step_keys,
                log_key=log_key,
            ),
        )


def get_step_output_event(
    events: List[DagsterEvent], step_key: str, output_name: Optional[str] = "result"
) -> Optional["DagsterEvent"]:
    check.list_param(events, "events", of_type=DagsterEvent)
    check.str_param(step_key, "step_key")
    check.str_param(output_name, "output_name")
    for event in events:
        if (
            event.event_type == DagsterEventType.STEP_OUTPUT
            and event.step_key == step_key
            and event.step_output_data.output_name == output_name
        ):
            return event
    return None


@whitelist_for_serdes
class StepMaterializationData(
    NamedTuple(
        "_StepMaterializationData",
        [
            ("materialization", Union[Materialization, AssetMaterialization]),
            ("asset_lineage", List[AssetLineageInfo]),
        ],
    )
):
    def __new__(
        cls,
        materialization: Union[Materialization, AssetMaterialization],
        asset_lineage: Optional[List[AssetLineageInfo]] = None,
    ):
        return super(StepMaterializationData, cls).__new__(
            cls,
            materialization=check.inst_param(
                materialization, "materialization", (Materialization, AssetMaterialization)
            ),
            asset_lineage=check.opt_list_param(
                asset_lineage, "asset_lineage", of_type=AssetLineageInfo
            ),
        )


@whitelist_for_serdes
class StepExpectationResultData(
    NamedTuple(
        "_StepExpectationResultData",
        [
            ("expectation_result", ExpectationResult),
        ],
    )
):
    def __new__(cls, expectation_result: ExpectationResult):
        return super(StepExpectationResultData, cls).__new__(
            cls,
            expectation_result=check.inst_param(
                expectation_result, "expectation_result", ExpectationResult
            ),
        )


@whitelist_for_serdes
class ObjectStoreOperationResultData(
    NamedTuple(
        "_ObjectStoreOperationResultData",
        [
            ("op", str),
            ("value_name", Optional[str]),
            ("metadata_entries", List[EventMetadataEntry]),
            ("address", Optional[str]),
            ("version", Optional[str]),
            ("mapping_key", Optional[str]),
        ],
    )
):
    def __new__(
        cls,
        op: str,
        value_name: Optional[str] = None,
        metadata_entries: Optional[List[EventMetadataEntry]] = None,
        address: Optional[str] = None,
        version: Optional[str] = None,
        mapping_key: Optional[str] = None,
    ):
        return super(ObjectStoreOperationResultData, cls).__new__(
            cls,
            op=check.str_param(op, "op"),
            value_name=check.opt_str_param(value_name, "value_name"),
            metadata_entries=check.opt_list_param(
                metadata_entries, "metadata_entries", of_type=EventMetadataEntry
            ),
            address=check.opt_str_param(address, "address"),
            version=check.opt_str_param(version, "version"),
            mapping_key=check.opt_str_param(mapping_key, "mapping_key"),
        )


@whitelist_for_serdes
class EngineEventData(
    NamedTuple(
        "_EngineEventData",
        [
            ("metadata_entries", List[EventMetadataEntry]),
            ("error", Optional[SerializableErrorInfo]),
            ("marker_start", Optional[str]),
            ("marker_end", Optional[str]),
        ],
    )
):
    # serdes log
    # * added optional error
    # * added marker_start / marker_end
    #
    def __new__(
        cls,
        metadata_entries: Optional[List[EventMetadataEntry]] = None,
        error: Optional[SerializableErrorInfo] = None,
        marker_start: Optional[str] = None,
        marker_end: Optional[str] = None,
    ):
        return super(EngineEventData, cls).__new__(
            cls,
            metadata_entries=check.opt_list_param(
                metadata_entries, "metadata_entries", of_type=EventMetadataEntry
            ),
            error=check.opt_inst_param(error, "error", SerializableErrorInfo),
            marker_start=check.opt_str_param(marker_start, "marker_start"),
            marker_end=check.opt_str_param(marker_end, "marker_end"),
        )

    @staticmethod
    def in_process(
        pid: int, step_keys_to_execute: Optional[List[str]] = None, marker_end: Optional[str] = None
    ) -> "EngineEventData":
        return EngineEventData(
            metadata_entries=[EventMetadataEntry.text(str(pid), "pid")]
            + (
                [EventMetadataEntry.text(str(step_keys_to_execute), "step_keys")]
                if step_keys_to_execute
                else []
            ),
            marker_end=marker_end,
        )

    @staticmethod
    def multiprocess(
        pid: int, step_keys_to_execute: Optional[List[str]] = None
    ) -> "EngineEventData":
        return EngineEventData(
            metadata_entries=[EventMetadataEntry.text(str(pid), "pid")]
            + (
                [EventMetadataEntry.text(str(step_keys_to_execute), "step_keys")]
                if step_keys_to_execute
                else []
            )
        )

    @staticmethod
    def interrupted(steps_interrupted: List[str]) -> "EngineEventData":
        return EngineEventData(
            metadata_entries=[EventMetadataEntry.text(str(steps_interrupted), "steps_interrupted")]
        )

    @staticmethod
    def engine_error(error: SerializableErrorInfo) -> "EngineEventData":
        return EngineEventData(metadata_entries=[], error=error)


@whitelist_for_serdes
class PipelineFailureData(
    NamedTuple(
        "_PipelineFailureData",
        [
            ("error", Optional[SerializableErrorInfo]),
        ],
    )
):
    def __new__(cls, error: Optional[SerializableErrorInfo]):
        return super(PipelineFailureData, cls).__new__(
            cls, error=check.opt_inst_param(error, "error", SerializableErrorInfo)
        )


@whitelist_for_serdes
class PipelineCanceledData(
    NamedTuple(
        "_PipelineCanceledData",
        [
            ("error", Optional[SerializableErrorInfo]),
        ],
    )
):
    def __new__(cls, error: Optional[SerializableErrorInfo]):
        return super(PipelineCanceledData, cls).__new__(
            cls, error=check.opt_inst_param(error, "error", SerializableErrorInfo)
        )


@whitelist_for_serdes
class HookErroredData(
    NamedTuple(
        "_HookErroredData",
        [
            ("error", SerializableErrorInfo),
        ],
    )
):
    def __new__(cls, error: SerializableErrorInfo):
        return super(HookErroredData, cls).__new__(
            cls, error=check.inst_param(error, "error", SerializableErrorInfo)
        )


@whitelist_for_serdes
class HandledOutputData(
    NamedTuple(
        "_HandledOutputData",
        [
            ("output_name", str),
            ("manager_key", str),
            ("metadata_entries", List[EventMetadataEntry]),
        ],
    )
):
    def __new__(
        cls,
        output_name: str,
        manager_key: str,
        metadata_entries: Optional[List[EventMetadataEntry]] = None,
    ):
        return super(HandledOutputData, cls).__new__(
            cls,
            output_name=check.str_param(output_name, "output_name"),
            manager_key=check.str_param(manager_key, "manager_key"),
            metadata_entries=check.opt_list_param(
                metadata_entries, "metadata_entries", of_type=EventMetadataEntry
            ),
        )


@whitelist_for_serdes
class LoadedInputData(
    NamedTuple(
        "_LoadedInputData",
        [
            ("input_name", str),
            ("manager_key", str),
            ("upstream_output_name", Optional[str]),
            ("upstream_step_key", Optional[str]),
        ],
    )
):
    def __new__(
        cls,
        input_name: str,
        manager_key: str,
        upstream_output_name: Optional[str] = None,
        upstream_step_key: Optional[str] = None,
    ):
        return super(LoadedInputData, cls).__new__(
            cls,
            input_name=check.str_param(input_name, "input_name"),
            manager_key=check.str_param(manager_key, "manager_key"),
            upstream_output_name=check.opt_str_param(upstream_output_name, "upstream_output_name"),
            upstream_step_key=check.opt_str_param(upstream_step_key, "upstream_step_key"),
        )


@whitelist_for_serdes
class ComputeLogsCaptureData(
    NamedTuple(
        "_ComputeLogsCaptureData",
        [
            ("log_key", str),
            ("step_keys", List[str]),
        ],
    )
):
    def __new__(cls, log_key, step_keys):
        return super(ComputeLogsCaptureData, cls).__new__(
            cls,
            log_key=check.str_param(log_key, "log_key"),
            step_keys=check.opt_list_param(step_keys, "step_keys", of_type=str),
        )


###################################################################################################
# THE GRAVEYARD
#
#            -|-                  -|-                  -|-
#             |                    |                    |
#        _-'~~~~~`-_ .        _-'~~~~~`-_          _-'~~~~~`-_
#      .'           '.      .'           '.      .'           '.
#      |    R I P    |      |    R I P    |      |    R I P    |
#      |             |      |             |      |             |
#      |  Synthetic  |      |    Asset    |      |   Pipeline  |
#      |   Process   |      |    Store    |      |    Init     |
#      |   Events    |      |  Operations |      |   Failures  |
#      |             |      |             |      |             |
###################################################################################################

# Keep these around to prevent issues like https://github.com/dagster-io/dagster/issues/3533
@whitelist_for_serdes
class AssetStoreOperationData(NamedTuple):
    op: str
    step_key: str
    output_name: str
    asset_store_key: str


@whitelist_for_serdes
class AssetStoreOperationType(Enum):
    SET_ASSET = "SET_ASSET"
    GET_ASSET = "GET_ASSET"


@whitelist_for_serdes
class PipelineInitFailureData(NamedTuple):
    error: SerializableErrorInfo


def _handle_back_compat(event_type_value, event_specific_data):
    # transform old specific process events in to engine events
    if event_type_value == "PIPELINE_PROCESS_START":
        return DagsterEventType.ENGINE_EVENT.value, EngineEventData([])
    elif event_type_value == "PIPELINE_PROCESS_STARTED":
        return DagsterEventType.ENGINE_EVENT.value, EngineEventData([])
    elif event_type_value == "PIPELINE_PROCESS_EXITED":
        return DagsterEventType.ENGINE_EVENT.value, EngineEventData([])

    # changes asset store ops in to get/set asset
    elif event_type_value == "ASSET_STORE_OPERATION":
        if event_specific_data.op in ("GET_ASSET", AssetStoreOperationType.GET_ASSET):
            return (
                DagsterEventType.LOADED_INPUT.value,
                LoadedInputData(
                    event_specific_data.output_name, event_specific_data.asset_store_key
                ),
            )
        if event_specific_data.op in ("SET_ASSET", AssetStoreOperationType.SET_ASSET):
            return (
                DagsterEventType.HANDLED_OUTPUT.value,
                HandledOutputData(
                    event_specific_data.output_name, event_specific_data.asset_store_key, []
                ),
            )

    # previous name for ASSET_MATERIALIZATION was STEP_MATERIALIZATION
    if event_type_value == "STEP_MATERIALIZATION":
        return DagsterEventType.ASSET_MATERIALIZATION.value, event_specific_data

    # transform PIPELINE_INIT_FAILURE to PIPELINE_FAILURE
    if event_type_value == "PIPELINE_INIT_FAILURE":
        return DagsterEventType.PIPELINE_FAILURE.value, PipelineFailureData(
            event_specific_data.error
        )

    return event_type_value, event_specific_data


register_serdes_tuple_fallbacks(
    {
        "PipelineProcessStartedData": None,
        "PipelineProcessExitedData": None,
        "PipelineProcessStartData": None,
    }
)
