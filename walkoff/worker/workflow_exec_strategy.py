import logging
import threading
from uuid import UUID

from walkoff.events import WalkoffEvent
from walkoff.executiondb.saved_workflow import SavedWorkflow
from walkoff.executiondb.workflow import Workflow
from walkoff.executiondb.workflowresults import WorkflowStatus, WorkflowStatusEnum
from walkoff.worker.action_exec_strategy import make_execution_strategy
from walkoff.worker.workflow_exec_context import WorkflowExecutionContext

logger = logging.getLogger(__name__)


class SerialWorkflowExecutionStrategy(object):

    def __init__(self, action_execution_strategy):
        self.action_execution_strategy = action_execution_strategy

    def execute(self, workflow_context, start=None, start_arguments=None, resume=False, environment_variables=None):
        """Executes a Workflow by executing all Actions in the Workflow list of Action objects.

        Args:
            workflow_context (WorkflowContext): The context of the workflow to execute
            start (int, optional): The ID of the first Action. Defaults to None.
            start_arguments (list[Argument]): Argument parameters into the first Action. Defaults to None.
            resume (bool, optional): Optional boolean to resume a previously paused workflow. Defaults to False.
            environment_variables (list[EnvironmentVariable], optional): Optional list of environment variables to
                pass into the workflow execution.
        """
        if environment_variables:
            workflow_context.accumulator.update({env_var.id: env_var.value for env_var in environment_variables})

        logger.info('Executing workflow {}'.format(workflow_context.name))
        workflow_context.send_event(WalkoffEvent.WorkflowExecutionStart)
        start = start if start is not None else workflow_context.workflow_start
        if not isinstance(start, UUID):
            start = UUID(start)

        self.do_execute(workflow_context, start, self.action_execution_strategy, start_arguments, resume)

    def do_execute(self, workflow_context, start, action_execution_strategy, start_arguments, resume):
        actions = SerialWorkflowExecutionStrategy.action_iter(workflow_context, action_execution_strategy, start=start)
        for action in (action_ for action_ in actions if action_ is not None):
            workflow_context.executing_action = action
            logger.debug('Executing action {} of workflow {}'.format(action, workflow_context.name))

            if workflow_context.is_paused:
                workflow_context.is_paused = False
                workflow_context.send_event(WalkoffEvent.WorkflowPaused)
                logger.debug('Paused workflow {} (id={})'.format(workflow_context.name, str(workflow_context.id)))
                return

            if workflow_context.is_aborted:
                workflow_context.is_aborted = False
                workflow_context.send_event(WalkoffEvent.WorkflowAborted)
                logger.info('Aborted workflow {} (id={})'.format(workflow_context.name, str(workflow_context.id)))
                return

            device_id = workflow_context.app_instance_repo.setup_app_instance(action, workflow_context)
            if device_id:
                result_status = action.execute(action_execution_strategy, workflow_context.accumulator,
                                               instance=workflow_context.get_app_instance(device_id),
                                               arguments=start_arguments, resume=resume)
            else:
                result_status = action.execute(action_execution_strategy, workflow_context.accumulator,
                                               arguments=start_arguments, resume=resume)

            workflow_context.update_status(result_status)

            if start_arguments:
                start_arguments = None

            if result_status == "trigger":
                return

        workflow_context.shutdown()

    @staticmethod
    def action_iter(workflow_context, action_execution_strategy, start):
        current_id = start
        current_action = workflow_context.get_action_by_id(current_id)

        while current_action:
            yield current_action
            current_id = SerialWorkflowExecutionStrategy.get_branch(workflow_context, action_execution_strategy)
            current_action = workflow_context.get_action_by_id(current_id) if current_id is not None else None
        return

    @staticmethod
    def get_branch(workflow_context, action_execution_strategy):
        """Executes the Branch objects associated with this Workflow to determine which Action should be
            executed next.

        Args:
            workflow_context (WorkflowExecutionContext): The context of the executing workflow
            action_execution_strategy: The strategy with which to execute the actions

        Returns:
            (UUID): The ID of the next Action to be executed if successful, else None.
        """
        if workflow_context.has_branches:
            current_action = workflow_context.executing_action
            for branch in workflow_context.get_branches_by_action_id(current_action.id):
                destination_id = branch.execute(
                    action_execution_strategy,
                    workflow_context.last_status,
                    current_action,
                    workflow_context.accumulator
                )
                if destination_id is not None:
                    logger.debug('Branch {} with destination {} chosen by workflow {} (id={})'.format(
                        str(branch.id),
                        str(destination_id),
                        workflow_context.name,
                        str(workflow_context.id))
                    )
                    return destination_id
            return None
        else:
            return None


class WorkflowExecutor(object):
    workflow_execution_strategies = {
        'serial': SerialWorkflowExecutionStrategy
    }

    def __init__(self, config, max_workflows, execution_db, app_instance_repo_class, executing_workflow_repo=dict):
        self.max_workflows = max_workflows
        self.execution_db = execution_db
        self.config = config
        self._app_instance_repo_class = app_instance_repo_class
        self.executing_workflows = executing_workflow_repo()
        self._lock = threading.Lock()

    @property
    def is_at_capacity(self):
        with self._lock:
            return len(self.executing_workflows) >= self.max_workflows

    def pause(self, workflow_execution_id):
        workflow_context = self.get_workflow_by_execution_id(workflow_execution_id)
        if workflow_context is not None:
            workflow_context.pause()

    def abort(self, workflow_execution_id):
        workflow_context = self.get_workflow_by_execution_id(workflow_execution_id)
        if workflow_context is not None:
            workflow_context.abort()
        else:
            logger.error('Attempted to abort workflow with execution id {}, but it wasn\'t executing'.format(
                workflow_execution_id))

    def make_new_context(self, workflow, workflow_execution_id, user=None):
        app_instance_repo = self._app_instance_repo_class()
        return WorkflowExecutionContext(workflow, app_instance_repo, workflow_execution_id, user)

    def make_resumed_context(self, workflow, workflow_execution_id, user=None):
        saved_state = self.execution_db.session.query(SavedWorkflow).filter_by(
            workflow_execution_id=workflow_execution_id).first()
        if saved_state is None:
            logger.error('Attempted to resume workflow with execution id {}, but no such workflow found'.format(
                workflow_execution_id))
            return None

        workflow_context = WorkflowExecutionContext(workflow, self._app_instance_repo_class(saved_state.app_instances),
                                                    workflow_execution_id, resumed=True, user=user)
        return workflow_context

    def execute(self, workflow_id, workflow_execution_id, start, start_arguments=None, resume=False,
                environment_variables=None, user=None):
        """Execute a workflow

        Args:
            workflow_id (UUID): The ID of the Workflow to be executed
            workflow_execution_id (UUID): The execution ID of the Workflow to be executed
            start (UUID): The ID of the starting Action
            start_arguments (list[Argument], optional): Optional list of starting Arguments. Defaults to None
            resume (bool, optional): Optional boolean to signify that this Workflow is being resumed. Defaults to False.
            environment_variables (list[EnvironmentVariable]): Optional list of environment variables to pass into
                the workflow. These will not be persistent.
            user (str, optional): The username who requested the workflow be executed. Defaults to None.
        """
        self.execution_db.session.expire_all()

        workflow_status = self.execution_db.session.query(WorkflowStatus).filter_by(
            execution_id=workflow_execution_id).first()

        if workflow_status.status == WorkflowStatusEnum.aborted:
            return

        workflow = self.execution_db.session.query(Workflow).filter_by(id=workflow_id).first()

        if not workflow.is_valid:
            logger.error('Workflow is invalid, yet executor attempted to execute.')
            return

        if resume:
            workflow_context = self.make_resumed_context(workflow, workflow_execution_id, user)
            if workflow_context is None:
                return
        else:
            workflow_context = self.make_new_context(workflow, workflow_execution_id, user)

        start = start if start else workflow.start

        with self._lock:
            self.executing_workflows[threading.current_thread().name] = workflow_context

        action_execution_strategy = make_execution_strategy(self.config, workflow_context)
        workflow_execution_strategy = self.workflow_execution_strategies['serial'](action_execution_strategy)
        workflow_execution_strategy.execute(workflow_context, start=start,
                                            start_arguments=start_arguments, resume=resume,
                                            environment_variables=environment_variables)
        with self._lock:
            self.executing_workflows.pop(threading.current_thread().name)

    def get_current_workflow(self):
        with self._lock:
            if threading.currentThread().name in self.executing_workflows:
                return self.executing_workflows[threading.currentThread().name]
            else:
                return None

    def get_workflow_by_execution_id(self, workflow_execution_id):
        with self._lock:
            for workflow_context in self.executing_workflows.values():
                if str(workflow_context.execution_id) == workflow_execution_id:
                    return workflow_context
            return None
