import pytest

from pulse.entities.conversation import (
    ReviewerMessage,
    StartInstruction,
    StartSource,
    channel_changed,
    start_instruction,
)
from tests.messages import make_message


def _feedback(*messages: ReviewerMessage) -> list[ReviewerMessage]:
    return list(messages)


def test_the_first_message_is_no_change() -> None:
    message = make_message("c01", channel="email")

    assert not channel_changed(_feedback(message), message)


def test_the_same_channel_is_no_change() -> None:
    message = make_message("r02")

    assert not channel_changed(_feedback(make_message("r01"), message), message)


def test_a_different_channel_is_a_change() -> None:
    message = make_message("c02", channel="email")

    assert channel_changed(_feedback(make_message("r01"), message), message)


def test_any_reviewer_previous_message_counts() -> None:
    # The conversation is one, whoever wrote each message.
    other = make_message("r01", author="other-reviewer")
    message = make_message("c02", channel="email")

    assert channel_changed(_feedback(other, message), message)


def test_messages_after_this_one_are_ignored() -> None:
    # A retried message is already recorded, with later messages after it.
    message = make_message("c01", channel="email")

    assert not channel_changed(_feedback(message, make_message("r02")), message)


def test_a_message_not_yet_recorded_compares_with_the_latest() -> None:
    message = make_message("c02", channel="email")

    assert channel_changed(_feedback(make_message("r01")), message)


def test_a_new_start_instruction_has_its_own_id_tagged_with_its_source() -> None:
    first, second = StartInstruction.new("scheduler"), StartInstruction.new("scheduler")

    assert first.message_id.startswith("start-scheduler-")
    assert StartInstruction.new("pulse draft").message_id.startswith("start-draft-")
    assert first.message_id != second.message_id


@pytest.mark.parametrize("source", ["scheduler", "pulse draft"])
def test_a_start_instruction_tag_gives_back_its_instruction(source: StartSource) -> None:
    instruction = StartInstruction.new(source)

    assert start_instruction(instruction.message_id) == instruction


def test_a_reviewer_message_id_is_no_start_instruction() -> None:
    assert start_instruction("r01") is None
