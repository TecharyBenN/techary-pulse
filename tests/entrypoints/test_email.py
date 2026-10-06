from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo

from pulse.adapters.store import SqliteStore
from pulse.entities.content import Version, version_of
from pulse.entities.extracts import Sensitivity
from pulse.entities.lifecycle import Newsletter, present
from pulse.entities.mail import InboundEmail
from pulse.entrypoints.email import EmailChannel
from tests.emails import (
    ENTRY,
    NO_NOTES,
    make_consolidation,
    make_draft,
    make_email,
    make_item,
    make_output,
    screen_email,
)
from tests.fakes.mailbox import FakeMailbox
from tests.fakes.models import (
    ModelFunction,
    Tools,
    gateway_error,
    make_orchestrator,
    present_call,
    reply_with,
    responses,
    show_call,
    text_response,
)
from tests.messages import OPENED, make_newsletter
from tests.operations import OPERATORS, REVIEWERS, make_operations, make_outbox, make_renderer

pytestmark = pytest.mark.anyio

REVIEWER_ADDRESS = "priya.shah@example.org"
INTERNAL = {"X-MS-Exchange-Organization-AuthAs": "Internal"}
THREADED = make_newsletter().model_copy(update={"thread_message_id": "sent-1"})


@pytest.fixture
async def store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "pulse.db")
    await store.initialise()
    return store


async def _never(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise AssertionError("the orchestrator was run")


def _email(message_id: str = "c01", **changes: object) -> InboundEmail:
    fields: dict[str, object] = {
        "sender_address": REVIEWER_ADDRESS,
        "subject": "RE: Draft: Pulse: 25 September 2026",
        "body": "Please shorten the intro.",
        "headers": INTERNAL,
    }
    return make_email(message_id, **(fields | changes))


def _channel(
    store: SqliteStore,
    conversation: FakeMailbox,
    model: ModelFunction,
    max_attempts: int = 3,
) -> EmailChannel:
    renderer = make_renderer()
    return EmailChannel(
        conversation,
        make_orchestrator(store, model, Tools()),
        store,
        make_outbox(conversation, store),
        make_operations(store).reviewer_email,
        renderer.newsletter,
        renderer.reply,
        max_attempts=max_attempts,
        processed_folder="Processed",
        rejected_folder="Rejected",
    )


async def _newsletter(store: SqliteStore) -> Newsletter:
    newsletter = await store.get_latest_newsletter()
    assert newsletter is not None
    return newsletter


async def _version_1(store: SqliteStore, newsletter: Newsletter) -> Version:
    """Store version 1, which the stand-in present_draft presents."""
    version = version_of(make_draft(), 1, OPENED, [], None, NO_NOTES)
    await store.save_version(present(newsletter), version)
    return version


def _folder(conversation: FakeMailbox, folder: str) -> list[str]:
    return [email.message_id for email in conversation.folders.get(folder, [])]


@pytest.mark.parametrize(
    "email",
    [
        _email(headers={}),
        _email(headers={"X-MS-Exchange-Organization-AuthAs": "Anonymous"}),
        _email(headers=INTERNAL | {"Auto-Submitted": "auto-replied"}),
        _email(headers=INTERNAL | {"X-Auto-Response-Suppress": "All"}),
    ],
)
async def test_unverified_and_automatic_messages_are_rejected_without_a_run(
    store: SqliteStore, email: InboundEmail
) -> None:
    await store.save_start(THREADED, [])
    conversation = FakeMailbox([email])

    await _channel(store, conversation, _never).poll()

    assert _folder(conversation, "Rejected") == ["c01"]
    assert (conversation.sent, conversation.replies) == ([], [])
    assert await store.list_feedback("n-1") == []


async def test_a_reply_goes_in_the_thread_to_the_reviewers(store: SqliteStore) -> None:
    await store.save_start(THREADED, [])
    conversation = FakeMailbox([_email()])

    await _channel(store, conversation, reply_with("I will shorten it.")).poll()

    [reply] = conversation.replies
    assert (reply.message_id, reply.to, reply.body.content_type) == ("sent-1", (REVIEWERS,), "html")
    assert "I will shorten it." in reply.body.content
    assert (await _newsletter(store)).thread_message_id == reply.reply_id
    assert _folder(conversation, "Processed") == ["c01"]


async def test_the_message_is_recorded_as_email_feedback_from_its_sender(
    store: SqliteStore,
) -> None:
    await store.save_start(THREADED, [])

    await _channel(store, FakeMailbox([_email()]), reply_with("Noted.")).poll()

    [feedback] = await store.list_feedback("n-1")
    assert (feedback.message_id, feedback.author, feedback.channel, feedback.text) == (
        "c01",
        REVIEWER_ADDRESS,
        "email",
        "Please shorten the intro.",
    )


async def test_only_the_reviewers_new_text_is_feedback(store: SqliteStore) -> None:
    await store.save_start(THREADED, [])
    # The whole body quotes the reviewer email, which holds staff subjects and evidence.
    quoted = "Please shorten the intro.\n\nFrom: Pulse\nExcluded for sensitivity: Board pack"
    email = _email(unique_body="Please shorten the intro.", body=quoted)

    await _channel(store, FakeMailbox([email]), reply_with("Noted.")).poll()

    [feedback] = await store.list_feedback("n-1")
    assert feedback.text == "Please shorten the intro."


async def test_a_presented_version_comes_in_one_reply_with_its_reviewer_email(
    store: SqliteStore,
) -> None:
    await store.save_start(THREADED, [])
    await _version_1(store, THREADED)
    conversation = FakeMailbox([_email()])
    model = responses(present_call(), text_response("Version 1 is ready."))

    await _channel(store, conversation, model).poll()

    [reply] = conversation.replies
    content = reply.body.content
    assert content.index("Version 1 is ready.") < content.index("Version 1</p>")
    assert make_draft().content.intro in content
    assert "This version was not judged." in content
    assert conversation.sent == []


async def test_a_first_version_starts_the_thread(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    await _version_1(store, make_newsletter())
    conversation = FakeMailbox([_email(subject="Please draft a newsletter")])
    model = responses(present_call(), text_response("Version 1 is ready."))

    await _channel(store, conversation, model).poll()

    [email] = conversation.sent
    assert (email.to, email.subject) == ([REVIEWERS], "Draft: Pulse: 25 September 2026")
    assert conversation.replies == []
    assert (await _newsletter(store)).thread_message_id == "sent-1"


async def test_a_shown_draft_comes_after_the_reply(store: SqliteStore) -> None:
    await store.save_start(THREADED, [])
    await store.save_draft("n-1", make_draft())
    conversation = FakeMailbox([_email()])
    model = responses(show_call(), text_response("Here is the draft."))

    await _channel(store, conversation, model).poll()

    [reply] = conversation.replies
    content = reply.body.content
    assert content.index("Here is the draft.") < content.index(make_draft().content.intro)
    assert "Version" not in content


async def test_a_shown_draft_shows_its_flags(store: SqliteStore) -> None:
    await store.save_start(THREADED, [screen_email(make_email("m01"))])
    flag = Sensitivity(kind="financial", withheld=False, evidence="mentions a contract value")
    await store.save_extract("n-1", "m01", make_output(sensitivity=[flag]))
    await store.save_items("n-1", make_consolidation(make_item(source_message_ids=["m01"])))
    await store.save_draft("n-1", make_draft())
    conversation = FakeMailbox([_email()])
    model = responses(show_call(), text_response("Here is the draft."))

    await _channel(store, conversation, model).poll()

    [reply] = conversation.replies
    content = reply.body.content
    assert "Financial</span>" in content
    assert "- Priya Shah" in content
    assert content.index("Flagged for review") > content.index(ENTRY)
    assert "Financial: mentions a contract value" in content


async def test_no_reply_sends_nothing_and_the_message_is_processed(store: SqliteStore) -> None:
    await store.save_start(THREADED, [])
    conversation = FakeMailbox([_email()])

    await _channel(store, conversation, reply_with("NO_REPLY")).poll()

    assert (conversation.sent, conversation.replies) == ([], [])
    assert [m.message_id for m in await store.list_feedback("n-1")] == ["c01"]
    assert _folder(conversation, "Processed") == ["c01"]
    assert (await _newsletter(store)).thread_message_id == "sent-1"


async def test_a_reply_before_any_thread_goes_to_the_reviewer_message(store: SqliteStore) -> None:
    await store.save_start(make_newsletter(), [])
    conversation = FakeMailbox([_email()])

    await _channel(store, conversation, reply_with("Nothing is presented yet.")).poll()

    [reply] = conversation.replies
    assert (reply.message_id, reply.to) == ("c01", (REVIEWERS,))
    assert (await _newsletter(store)).thread_message_id is None


async def test_a_reply_before_any_newsletter_goes_to_the_reviewer_message(
    store: SqliteStore,
) -> None:
    conversation = FakeMailbox([_email()])

    await _channel(store, conversation, reply_with("No newsletter exists yet.")).poll()

    [reply] = conversation.replies
    assert (reply.message_id, reply.to) == ("c01", (REVIEWERS,))


async def test_a_failed_run_stays_in_the_inbox_and_stops_the_poll(store: SqliteStore) -> None:
    await store.save_start(THREADED, [])
    calls = 0

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        raise gateway_error()

    conversation = FakeMailbox([_email("c01"), _email("c02")])

    await _channel(store, conversation, model).poll()

    assert calls == 1
    assert [email.message_id for email in conversation.inbox] == ["c01", "c02"]
    assert conversation.replies == []


async def test_a_message_failing_max_attempts_times_is_rejected_with_an_alert(
    store: SqliteStore,
) -> None:
    await store.save_start(THREADED, [])
    conversation = FakeMailbox([_email()])
    channel = _channel(store, conversation, responses(gateway_error(), gateway_error()), 2)

    await channel.poll()
    assert _folder(conversation, "Rejected") == []
    await channel.poll()

    assert _folder(conversation, "Rejected") == ["c01"]
    [alert] = conversation.sent
    assert alert.to == OPERATORS
    assert "c01" in alert.body.content
    assert "shorten" not in alert.body.content


async def test_a_message_whose_runs_were_interrupted_is_rejected_without_a_run(
    store: SqliteStore,
) -> None:
    await store.save_start(THREADED, [])
    for _ in range(2):
        await store.record_attempt("c01")
    conversation = FakeMailbox([_email()])

    await _channel(store, conversation, _never, max_attempts=2).poll()

    assert _folder(conversation, "Rejected") == ["c01"]
    [alert] = conversation.sent
    assert alert.to == OPERATORS


async def test_a_reply_that_failed_is_sent_at_the_retry_without_a_second_run(
    store: SqliteStore,
) -> None:
    await store.save_start(THREADED, [])
    conversation = FakeMailbox([_email()])
    conversation.failures = 1
    # One answer only, so a second run would fail.
    channel = _channel(store, conversation, responses(text_response("I will shorten it.")))

    await channel.poll()
    assert conversation.inbox != [] and conversation.replies == []
    await channel.poll()

    [reply] = conversation.replies
    assert "I will shorten it." in reply.body.content
    assert _folder(conversation, "Processed") == ["c01"]


async def test_a_handled_message_left_in_the_inbox_is_moved_without_a_run(
    store: SqliteStore,
) -> None:
    await store.save_start(THREADED, [])
    await store.record_attempt("c01")
    await store.mark_handled("c01")
    conversation = FakeMailbox([_email()])

    await _channel(store, conversation, _never).poll()

    assert _folder(conversation, "Processed") == ["c01"]
    assert conversation.replies == []
