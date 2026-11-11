"""Regression coverage for https://github.com/langgenius/dify/issues/46034.

A tool that returns a variable without streaming it (``stream=False``) only
emits an empty ``is_final=True`` chunk for that variable. The response stream
filter must not treat that empty marker as the variable's streamed content:
the value still has to reach the Answer node from the variable pool, so the
streamed answer matches the persisted workflow run record.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from typing import override

from core.workflow.node_runtime import DifyToolFileManager
from core.workflow.system_variables import build_system_variables
from core.workflow.workflow_entry import iter_dify_graph_engine_events
from graphon.engine import Engine
from graphon.engine.command import InMemoryChannel
from graphon.engine_events import EngineEvent, GraphRunSucceededEvent, NodeRunStreamChunkEvent
from graphon.graph import Graph
from graphon.model_runtime.entities.llm_entities import LLMUsage
from graphon.nodes.answer.answer_node import AnswerNode
from graphon.nodes.answer.entities import AnswerNodeData
from graphon.nodes.runtime import ToolNodeRuntimeProtocol
from graphon.nodes.start.entities import StartNodeData
from graphon.nodes.start.start_node import StartNode
from graphon.nodes.tool.entities import ToolNodeData
from graphon.nodes.tool.tool_node import ToolNode
from graphon.nodes.tool_runtime_entities import ToolRuntimeHandle, ToolRuntimeMessage, ToolRuntimeParameter
from graphon.runtime import RuntimeState, VariablePool
from tests.workflow_test_utils import build_test_graph_init_params


class _StubToolRuntime(ToolNodeRuntimeProtocol):
    """Minimal tool runtime yielding canned tool messages.

    Preconditions: the caller supplies the exact messages the tool returns.
    Postconditions: ``invoke`` yields those messages unchanged.
    """

    def __init__(self, messages: Sequence[ToolRuntimeMessage]) -> None:
        self._messages = list(messages)

    @override
    def get_runtime(
        self,
        *,
        node_id: str,
        node_data: ToolNodeData,
        variable_pool: VariablePool | None,
        node_execution_id: str | None = None,
    ) -> ToolRuntimeHandle:
        return ToolRuntimeHandle(raw=object())

    @override
    def get_runtime_parameters(
        self,
        *,
        tool_runtime: ToolRuntimeHandle,
    ) -> Sequence[ToolRuntimeParameter]:
        return []

    @override
    def invoke(
        self,
        *,
        tool_runtime: ToolRuntimeHandle,
        tool_parameters: Mapping[str, object],
        workflow_call_depth: int,
        provider_name: str,
    ) -> Generator[ToolRuntimeMessage]:
        yield from self._messages

    @override
    def get_usage(self, *, tool_runtime: ToolRuntimeHandle) -> LLMUsage:
        return LLMUsage.empty_usage()

    @override
    def build_file_reference(self, *, mapping: Mapping[str, object]) -> object:
        # File messages are out of scope for this regression test: the canned
        # messages never include one, so this is never called.
        return None


def _variable_message(
    variable_name: str,
    variable_value: str | int | float | bool | dict[str, object] | list[object] | None,
    *,
    stream: bool = False,
) -> ToolRuntimeMessage:
    return ToolRuntimeMessage(
        type=ToolRuntimeMessage.MessageType.VARIABLE,
        message=ToolRuntimeMessage.VariableMessage(
            variable_name=variable_name, variable_value=variable_value, stream=stream
        ),
    )


def _text_message(text: str) -> ToolRuntimeMessage:
    return ToolRuntimeMessage(
        type=ToolRuntimeMessage.MessageType.TEXT,
        message=ToolRuntimeMessage.TextMessage(text=text),
    )


def _json_message(json_object: dict[str, object] | list[object]) -> ToolRuntimeMessage:
    return ToolRuntimeMessage(
        type=ToolRuntimeMessage.MessageType.JSON,
        message=ToolRuntimeMessage.JsonMessage(json_object=json_object),
    )


def _build_runtime_state() -> RuntimeState:
    variable_pool = VariablePool.from_bootstrap(
        system_variables=build_system_variables(
            workflow_execution_id="wf-exec-46034",
            app_id="app",
            workflow_id="wf",
            user_id="user",
        ),
        user_inputs={},
        conversation_variables=[],
    )
    return RuntimeState(workflow_id="test-workflow", variable_pool=variable_pool, start_at=0.0)


def _run_tool_answer_graph(messages: Sequence[ToolRuntimeMessage]) -> list[EngineEvent]:
    runtime_state = _build_runtime_state()
    params = build_test_graph_init_params(
        workflow_id="wf",
        graph_config={"nodes": [], "edges": []},
    )

    start_node = StartNode(
        node_id="start",
        data=StartNodeData(title="start", variables=[]),
        init_params=params,
        runtime_state=runtime_state,
    )
    tool_node = ToolNode(
        node_id="tool",
        data=ToolNodeData.model_validate(
            {
                "type": "tool",
                "title": "Tool",
                "provider_id": "test-provider",
                "provider_type": "workflow",
                "provider_name": "test-provider",
                "tool_name": "test-tool",
                "tool_label": "Test Tool",
                "tool_configurations": {},
                "tool_parameters": {},
                "version": "1",
            }
        ),
        init_params=params,
        runtime_state=runtime_state,
        tool_file_manager=DifyToolFileManager(params.run_context),
        runtime=_StubToolRuntime(messages),
    )
    answer_node = AnswerNode(
        node_id="answer",
        data=AnswerNodeData(title="answer", answer="Result: {{#tool.answer#}}"),
        init_params=params,
        runtime_state=runtime_state,
    )

    graph = (
        Graph.new()
        .add_root(start_node)
        .add_node(tool_node, from_node_id="start")
        .add_node(answer_node, from_node_id="tool")
        .build()
    )
    engine = Engine(
        graph=graph,
        runtime_state=runtime_state,
        command_channel=InMemoryChannel(),
    )
    return list(iter_dify_graph_engine_events(engine))


def _streamed_answer(events: list[EngineEvent]) -> str:
    """Join every stream chunk the response filter emitted, in order.

    The filter replays a referenced variable segment's chunks under the
    source node's id, so the streamed answer is the concatenation of all
    chunk events, not only the answer node's own chunks (this matches
    ``test_dify_response_stream_filter_handles_issue_170_shape``).
    """
    chunks = [event for event in events if isinstance(event, NodeRunStreamChunkEvent)]
    assert chunks, "workflow produced no stream chunks"
    assert chunks[-1].is_final is True
    return "".join(event.chunk for event in chunks)


def _persisted_answer(events: list[EngineEvent]) -> str:
    success_events = [event for event in events if isinstance(event, GraphRunSucceededEvent)]
    assert success_events, "workflow did not succeed"
    final_answer = success_events[-1].outputs["answer"]
    assert isinstance(final_answer, str)
    return final_answer


class TestNonStreamedToolVariableStreaming:
    def test_non_streamed_variable_value_reaches_answer_stream(self) -> None:
        events = _run_tool_answer_graph(
            [
                _variable_message("answer", "hello from tool"),
                _text_message("hello from tool"),
                _json_message({"answer": "hello from tool"}),
            ]
        )

        assert _streamed_answer(events) == "Result: hello from tool"
        # The streamed answer must match the persisted run record: before the
        # fix the stream rendered "Result: " while the record held the value.
        assert _streamed_answer(events) == _persisted_answer(events)

    def test_streamed_variable_value_still_streams_incrementally(self) -> None:
        events = _run_tool_answer_graph(
            [
                _variable_message("answer", "hello ", stream=True),
                _variable_message("answer", "from tool", stream=True),
                _text_message("hello from tool"),
                _json_message({"answer": "hello from tool"}),
            ]
        )

        assert _streamed_answer(events) == "Result: hello from tool"
        assert _streamed_answer(events) == _persisted_answer(events)

    def test_genuinely_empty_variable_completes_without_stalling(self) -> None:
        events = _run_tool_answer_graph(
            [
                _variable_message("answer", ""),
                _text_message(""),
                _json_message({"answer": ""}),
            ]
        )

        assert _streamed_answer(events) == "Result: "
        assert _streamed_answer(events) == _persisted_answer(events)
