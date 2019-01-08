# Licensed under LICENSE.md; also available at https://www.prefect.io/licenses/alpha-eula

import warnings
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import prefect
from prefect.client import Client
from prefect.client.result_handlers import ResultHandler
from prefect.core import Edge, Task
from prefect.utilities.graphql import with_args
from prefect.engine.runner import ENDRUN
from prefect.engine.state import Failed, State, Mapped
from prefect.engine.task_runner import TaskRunner


class CloudTaskRunner(TaskRunner):
    """
    TaskRunners handle the execution of Tasks and determine the State of a Task
    before, during and after the Task is run.

    In particular, through the TaskRunner you can specify the states of any upstream dependencies,
    any inputs required for this Task to run, and what state the Task should be initialized with.

    Args:
        - task (Task): the Task to be run / executed
        - result_handler (ResultHandler, optional): the handler to use for
            retrieving and storing state results during execution
        - state_handlers (Iterable[Callable], optional): A list of state change handlers
            that will be called whenever the task changes state, providing an
            opportunity to inspect or modify the new state. The handler
            will be passed the task runner instance, the old (prior) state, and the new
            (current) state, with the following signature:

            ```
                state_handler(
                    task_runner: TaskRunner,
                    old_state: State,
                    new_state: State) -> State
            ```

            If multiple functions are passed, then the `new_state` argument will be the
            result of the previous handler.
    """

    def __init__(
        self,
        task: Task,
        result_handler: ResultHandler = None,
        state_handlers: Iterable[Callable] = None,
    ) -> None:
        self.client = Client()
        super().__init__(
            task=task, result_handler=result_handler, state_handlers=state_handlers
        )

    def _heartbeat(self) -> None:
        try:
            task_run_id = self.task_run_id
            self.client.update_task_run_heartbeat(task_run_id)
        except:
            warnings.warn("Heartbeat failed for Task '{}'".format(self.task.name))

    def call_runner_target_handlers(self, old_state: State, new_state: State) -> State:
        """
        A special state handler that the TaskRunner uses to call its task's state handlers.
        This method is called as part of the base Runner's `handle_state_change()` method.

        Args:
            - old_state (State): the old (previous) state
            - new_state (State): the new (current) state

        Returns:
            - State: the new state
        """
        new_state = super().call_runner_target_handlers(
            old_state=old_state, new_state=new_state
        )

        task_run_id = prefect.context.get("task_run_id")
        version = prefect.context.get("task_run_version")

        try:
            self.client.set_task_run_state(
                task_run_id=task_run_id,
                version=version,
                state=new_state,
                cache_for=self.task.cache_for,
                result_handler=self.result_handler,
            )
        except Exception as exc:
            raise ENDRUN(state=new_state)

        prefect.context.update(task_run_version=version + 1)  # type: ignore

        # assign task run id and version to make state lookups simpler in the future
        new_state._task_run_id = task_run_id
        new_state._version = version + 1

        return new_state

    def initialize_run(  # type: ignore
        self,
        state: Optional[State],
        context: Dict[str, Any],
        upstream_states: Dict[Edge, State],
        inputs: Dict[str, Any],
    ) -> Tuple[State, Dict[str, Any], Dict[Edge, State], Dict[str, Any]]:
        """
        Initializes the Task run by initializing state and context appropriately.

        Args:
            - state (State): the proposed initial state of the flow run; can be `None`
            - context (Dict[str, Any]): the context to be updated with relevant information
            - upstream_states (Dict[Edge, State]): a dictionary
                representing the states of tasks upstream of this one
            - inputs (Dict[str, Any]): a dictionary of inputs to the task that should override
                the inputs taken from upstream states

        Returns:
            - tuple: a tuple of the updated state, context, upstream_states, and inputs objects
        """
        try:
            task_run_info = self.client.get_task_run_info(
                flow_run_id=context.get("flow_run_id", ""),
                task_id=self.task.id,
                map_index=context.get("map_index"),
                result_handler=self.result_handler,
            )
        except Exception as exc:
            if state is None:
                state = Failed(
                    message="Could not retrieve state from Prefect Cloud", result=exc
                )
            raise ENDRUN(state=state)

        # if state was provided, keep it; otherwise use the one from db
        state = state or task_run_info.state  # type: ignore
        context.update(
            task_run_version=task_run_info.version,  # type: ignore
            task_run_id=task_run_info.id,  # type: ignore
        )
        # we assign this so it can be shared with heartbeat thread
        self.task_run_id = task_run_info.id  # type: ignore

        # update upstream_states
        upstream_states = self.get_latest_upstream_states(
            context=context, upstream_states=upstream_states
        )

        # update inputs, prioritizing kwarg-provided inputs
        if hasattr(state, "cached_inputs") and isinstance(
            state.cached_inputs, dict  # type: ignore
        ):
            updated_inputs = state.cached_inputs.copy()  # type: ignore
        else:
            updated_inputs = {}
        updated_inputs.update(inputs)

        return super().initialize_run(
            state=state,
            context=context,
            upstream_states=upstream_states,
            inputs=updated_inputs,
        )

    def get_latest_upstream_states(
        self, context: Dict[str, Any], upstream_states: Dict[Edge, State]
    ) -> Dict[Edge, State]:
        """
        Reloads the latest upstream states from Prefect Cloud.

        There are two possibilities:
            - if we've already confirmed a state with Prefect Cloud, then it will have `_task_run_id`
                and `_version` attributes. We query Cloud for any state with that id and NOT that version.
                If a result comes back, it's more recent and we update our state. If nothing comes
                back, then we're current.

            - if we haven't loaded this state yet (for example, it was created as a stand-in by
                a FlowRunner), then we get-or-create its task run info. If the resulting state
                is `Mapped` and the edge in question is being mapped over, then we make one
                more query to get the state at the appropriate `map_index`.

        Args:
            - context (Dict[str, Any]): the context
            - upstream_states (Dict[Edge, State]): the upstream states.

        Returns:
            - Dict[Edge, State]: a dictionary of current upstream states
        """

        updated_states = self.client.get_latest_task_run_states(
            flow_run_id=context.get("flow_run_id", ""),
            states={e.upstream_task: s for e, s in upstream_states.items()},
            result_handler=self.result_handler,
        )

        new_upstream_states = {}
        for edge, state in upstream_states.items():
            new_state = updated_states.get(edge.upstream_task, state)
            if edge.mapped and new_state.is_mapped():
                new_state = self.client.get_task_run_info(  # type: ignore
                    flow_run_id=context.get("flow_run_id", ""),
                    task_id=edge.upstream_task.id,
                    map_index=context.get("map_index"),
                    result_handler=self.result_handler,
                ).state
            new_upstream_states[edge] = new_state
        return new_upstream_states

    def wait_for_mapped_upstream(
        self,
        upstream_states: Dict[Edge, State],
        executor: "prefect.engine.executors.Executor",
    ) -> Dict[Edge, State]:
        """
        Loads the results of any mapped upstream tasks

        Args:
            - upstream_states (Dict[Edge, State]): the upstream states
            - executor (Executor): the executor

        Returns:
            - Dict[Edge, State]: the upstream states
        """
        # first, block until any futures finish
        upstream_states = super().wait_for_mapped_upstream(
            upstream_states=upstream_states, executor=executor
        )

        # then load any required children map_states -- we need them if we are NOT mapping
        # (meaning we are reducing) and if the upstream is mapped
        for edge, upstream_state in upstream_states.items():

            if not edge.mapped and upstream_state.is_mapped():
                assert isinstance(upstream_state, Mapped)  # mypy assert
                upstream_state.map_states = self.client.get_mapped_children_states(
                    flow_run_id=prefect.context.get("flow_run_id", ""),
                    task_id=edge.upstream_task.id,
                    result_handler=self.result_handler,
                )
                upstream_state.result = [s.result for s in upstream_state.map_states]

        return upstream_states
