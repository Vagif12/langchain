from typing import Any, Dict, List, Optional
from uuid import UUID

import pytest
from freezegun import freeze_time
from pytest_mock import MockerFixture
from syrupy import SnapshotAssertion

from langchain.callbacks.manager import Callbacks
from langchain.callbacks.tracers.base import BaseTracer
from langchain.callbacks.tracers.schemas import Run
from langchain.chat_models.fake import FakeListChatModel
from langchain.llms.fake import FakeListLLM
from langchain.load.dump import dumps
from langchain.output_parsers.list import CommaSeparatedListOutputParser
from langchain.prompts.chat import (
    ChatPromptTemplate,
    ChatPromptValue,
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate,
)
from langchain.schema.document import Document
from langchain.schema.messages import AIMessage, HumanMessage, SystemMessage
from langchain.schema.retriever import BaseRetriever
from langchain.schema.runnable import (
    Runnable,
    RunnableConfig,
    RunnableLambda,
    RunnableMap,
    RunnablePassthrough,
    RunnableSequence,
)


class FakeTracer(BaseTracer):
    """Fake tracer that records LangChain execution."""

    def __init__(self) -> None:
        """Initialize the tracer."""
        super().__init__()
        self.runs: List[Run] = []

    def _persist_run(self, run: Run) -> None:
        """Persist a run."""
        self.runs.append(run)


class FakeRunnable(Runnable[str, int]):
    def invoke(
        self,
        input: str,
        config: Optional[RunnableConfig] = None,
    ) -> int:
        return len(input)


class FakeRetriever(BaseRetriever):
    def _get_relevant_documents(
        self,
        query: str,
        *,
        callbacks: Callbacks = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[Document]:
        return [Document(page_content="foo"), Document(page_content="bar")]

    async def _aget_relevant_documents(
        self,
        query: str,
        *,
        callbacks: Callbacks = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[Document]:
        return [Document(page_content="foo"), Document(page_content="bar")]


@pytest.fixture()
def fixed_uuids(mocker: MockerFixture) -> MockerFixture._Patcher:
    """Note this mock only works with `import uuid; uuid.uuid4()`,
    it does not work with `from uuid import uuid4; uuid4()`."""

    # Disable tracing to avoid fixed UUIDs causing tracing errors.
    mocker.patch.dict("os.environ", {"LANGCHAIN_TRACING_V2": "false"})

    side_effect = (
        UUID(f"00000000-0000-4000-8000-{i:012}", version=4) for i in range(10000)
    )
    return mocker.patch("uuid.uuid4", side_effect=side_effect)


@pytest.mark.asyncio
async def test_default_method_implementations(mocker: MockerFixture) -> None:
    fake = FakeRunnable()
    spy = mocker.spy(fake, "invoke")

    assert fake.invoke("hello", dict(tags=["a-tag"])) == 5
    assert spy.call_args_list == [
        mocker.call("hello", dict(tags=["a-tag"])),
    ]
    spy.reset_mock()

    assert [*fake.stream("hello", dict(metadata={"key": "value"}))] == [5]
    assert spy.call_args_list == [
        mocker.call("hello", dict(metadata={"key": "value"})),
    ]
    spy.reset_mock()

    assert fake.batch(
        ["hello", "wooorld"], [dict(tags=["a-tag"]), dict(metadata={"key": "value"})]
    ) == [5, 7]
    assert spy.call_args_list == [
        mocker.call("hello", dict(tags=["a-tag"])),
        mocker.call("wooorld", dict(metadata={"key": "value"})),
    ]
    spy.reset_mock()

    assert fake.batch(["hello", "wooorld"], dict(tags=["a-tag"])) == [5, 7]
    assert spy.call_args_list == [
        mocker.call("hello", dict(tags=["a-tag"])),
        mocker.call("wooorld", dict(tags=["a-tag"])),
    ]
    spy.reset_mock()

    assert await fake.ainvoke("hello", config={"callbacks": []}) == 5
    assert spy.call_args_list == [
        mocker.call("hello", dict(callbacks=[])),
    ]
    spy.reset_mock()

    assert [part async for part in fake.astream("hello")] == [5]
    assert spy.call_args_list == [
        mocker.call("hello", None),
    ]
    spy.reset_mock()

    assert await fake.abatch(["hello", "wooorld"], dict(metadata={"key": "value"})) == [
        5,
        7,
    ]
    assert spy.call_args_list == [
        mocker.call("hello", dict(metadata={"key": "value"})),
        mocker.call("wooorld", dict(metadata={"key": "value"})),
    ]


@pytest.mark.asyncio
async def test_prompt() -> None:
    prompt = ChatPromptTemplate.from_messages(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessagePromptTemplate.from_template("{question}"),
        ]
    )
    expected = ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )

    assert prompt.invoke({"question": "What is your name?"}) == expected

    assert prompt.batch(
        [
            {"question": "What is your name?"},
            {"question": "What is your favorite color?"},
        ]
    ) == [
        expected,
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your favorite color?"),
            ]
        ),
    ]

    assert [*prompt.stream({"question": "What is your name?"})] == [expected]

    assert await prompt.ainvoke({"question": "What is your name?"}) == expected

    assert await prompt.abatch(
        [
            {"question": "What is your name?"},
            {"question": "What is your favorite color?"},
        ]
    ) == [
        expected,
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your favorite color?"),
            ]
        ),
    ]

    assert [
        part async for part in prompt.astream({"question": "What is your name?"})
    ] == [expected]


@pytest.mark.asyncio
@freeze_time("2023-01-01")
async def test_prompt_with_chat_model(
    mocker: MockerFixture, snapshot: SnapshotAssertion, fixed_uuids: None
) -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    chat = FakeListChatModel(responses=["foo", "bar"])

    chain = prompt | chat

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == []
    assert chain.last == chat
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke(
        {"question": "What is your name?"}, dict(callbacks=[tracer])
    ) == AIMessage(content="foo")
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert tracer.runs == snapshot
    mocker.stop(prompt_spy)
    mocker.stop(chat_spy)

    # Test batch
    prompt_spy = mocker.spy(prompt.__class__, "batch")
    chat_spy = mocker.spy(chat.__class__, "batch")
    tracer = FakeTracer()
    assert chain.batch(
        [
            {"question": "What is your name?"},
            {"question": "What is your favorite color?"},
        ],
        dict(callbacks=[tracer]),
    ) == [
        AIMessage(content="bar"),
        AIMessage(content="foo"),
    ]
    assert prompt_spy.call_args.args[1] == [
        {"question": "What is your name?"},
        {"question": "What is your favorite color?"},
    ]
    assert chat_spy.call_args.args[1] == [
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your name?"),
            ]
        ),
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your favorite color?"),
            ]
        ),
    ]
    assert tracer.runs == snapshot
    mocker.stop(prompt_spy)
    mocker.stop(chat_spy)

    # Test stream
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "stream")
    tracer = FakeTracer()
    assert [
        *chain.stream({"question": "What is your name?"}, dict(callbacks=[tracer]))
    ] == [AIMessage(content="bar")]
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )


@pytest.mark.asyncio
@freeze_time("2023-01-01")
async def test_prompt_with_llm(
    mocker: MockerFixture, snapshot: SnapshotAssertion, fixed_uuids: None
) -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    llm = FakeListLLM(responses=["foo", "bar"])

    chain = prompt | llm

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == []
    assert chain.last == llm
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "ainvoke")
    llm_spy = mocker.spy(llm.__class__, "ainvoke")
    tracer = FakeTracer()
    assert (
        await chain.ainvoke(
            {"question": "What is your name?"}, dict(callbacks=[tracer])
        )
        == "foo"
    )
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert llm_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert tracer.runs == snapshot
    mocker.stop(prompt_spy)
    mocker.stop(llm_spy)

    # Test batch
    prompt_spy = mocker.spy(prompt.__class__, "abatch")
    llm_spy = mocker.spy(llm.__class__, "abatch")
    tracer = FakeTracer()
    assert await chain.abatch(
        [
            {"question": "What is your name?"},
            {"question": "What is your favorite color?"},
        ],
        dict(callbacks=[tracer]),
    ) == ["bar", "foo"]
    assert prompt_spy.call_args.args[1] == [
        {"question": "What is your name?"},
        {"question": "What is your favorite color?"},
    ]
    assert llm_spy.call_args.args[1] == [
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your name?"),
            ]
        ),
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your favorite color?"),
            ]
        ),
    ]
    assert tracer.runs == snapshot
    mocker.stop(prompt_spy)
    mocker.stop(llm_spy)

    # Test stream
    prompt_spy = mocker.spy(prompt.__class__, "ainvoke")
    llm_spy = mocker.spy(llm.__class__, "astream")
    tracer = FakeTracer()
    assert [
        token
        async for token in chain.astream(
            {"question": "What is your name?"}, dict(callbacks=[tracer])
        )
    ] == ["bar"]
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert llm_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )


@freeze_time("2023-01-01")
def test_prompt_with_chat_model_and_parser(
    mocker: MockerFixture, snapshot: SnapshotAssertion, fixed_uuids: None
) -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    chat = FakeListChatModel(responses=["foo, bar"])
    parser = CommaSeparatedListOutputParser()

    chain = prompt | chat | parser

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == [chat]
    assert chain.last == parser
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    parser_spy = mocker.spy(parser.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke(
        {"question": "What is your name?"}, dict(callbacks=[tracer])
    ) == ["foo", "bar"]
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert parser_spy.call_args.args[1] == AIMessage(content="foo, bar")
    assert tracer.runs == snapshot


@freeze_time("2023-01-01")
def test_seq_dict_prompt_llm(
    mocker: MockerFixture, snapshot: SnapshotAssertion, fixed_uuids: None
) -> None:
    passthrough = mocker.Mock(side_effect=lambda x: x)

    retriever = FakeRetriever()

    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + """Context:
{documents}

Question:
{question}"""
    )

    chat = FakeListChatModel(responses=["foo, bar"])

    parser = CommaSeparatedListOutputParser()

    chain = (
        {
            "question": RunnablePassthrough[str]() | passthrough,
            "documents": passthrough | retriever,
            "just_to_test_lambda": passthrough,
        }
        | prompt
        | chat
        | parser
    )

    assert isinstance(chain, RunnableSequence)
    assert isinstance(chain.first, RunnableMap)
    assert chain.middle == [prompt, chat]
    assert chain.last == parser
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    parser_spy = mocker.spy(parser.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke("What is your name?", dict(callbacks=[tracer])) == [
        "foo",
        "bar",
    ]
    assert prompt_spy.call_args.args[1] == {
        "documents": [Document(page_content="foo"), Document(page_content="bar")],
        "question": "What is your name?",
        "just_to_test_lambda": "What is your name?",
    }
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(
                content="""Context:
[Document(page_content='foo', metadata={}), Document(page_content='bar', metadata={})]

Question:
What is your name?"""
            ),
        ]
    )
    assert parser_spy.call_args.args[1] == AIMessage(content="foo, bar")
    assert tracer.runs == snapshot


@freeze_time("2023-01-01")
def test_seq_prompt_dict(
    mocker: MockerFixture, snapshot: SnapshotAssertion, fixed_uuids: None
) -> None:
    passthrough = mocker.Mock(side_effect=lambda x: x)

    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )

    chat = FakeListChatModel(responses=["i'm a chatbot"])

    llm = FakeListLLM(responses=["i'm a textbot"])

    chain = (
        prompt
        | passthrough
        | {
            "chat": chat,
            "llm": llm,
        }
    )

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == [RunnableLambda(passthrough)]
    assert isinstance(chain.last, RunnableMap)
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    llm_spy = mocker.spy(llm.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke(
        {"question": "What is your name?"}, dict(callbacks=[tracer])
    ) == {
        "chat": AIMessage(content="i'm a chatbot"),
        "llm": "i'm a textbot",
    }
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert llm_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert tracer.runs == snapshot


@freeze_time("2023-01-01")
def test_seq_prompt_map(
    mocker: MockerFixture, snapshot: SnapshotAssertion, fixed_uuids: None
) -> None:
    passthrough = mocker.Mock(side_effect=lambda x: x)

    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )

    chat = FakeListChatModel(responses=["i'm a chatbot"])

    llm = FakeListLLM(responses=["i'm a textbot"])

    chain = (
        prompt
        | passthrough
        | {
            "chat": chat.bind(stop=["Thought:"]),
            "llm": llm,
            "passthrough": passthrough,
        }
    )

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == [RunnableLambda(passthrough)]
    assert isinstance(chain.last, RunnableMap)
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    llm_spy = mocker.spy(llm.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke(
        {"question": "What is your name?"}, dict(callbacks=[tracer])
    ) == {
        "chat": AIMessage(content="i'm a chatbot"),
        "llm": "i'm a textbot",
        "passthrough": ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your name?"),
            ]
        ),
    }
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert llm_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert tracer.runs == snapshot
