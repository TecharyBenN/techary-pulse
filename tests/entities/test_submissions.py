from datetime import UTC, datetime

import pytest

from pulse.entities.mail import Email, InboundEmail
from pulse.entities.submissions import screen, source_text
from tests.emails import make_email

LABEL = "3a1b7f0e-5c2d-4e8a-9b6f-1d2c3e4f5a6b"
OTHER_LABEL = "9c8d7e6f-0000-4e8a-9b6f-1d2c3e4f5a6b"


def _email(
    sender_address: str = "priya.shah@techary.ai", headers: dict[str, str] | None = None
) -> InboundEmail:
    return make_email(sender_address=sender_address, headers=headers or {}, has_attachments=True)


def _screen(
    email: InboundEmail, senders: list[str] | None = None, labels: list[str] | None = None
) -> str | None:
    return screen(email, ["techary.ai"], senders or [], labels or []).rejection


def test_submission_is_the_email_with_its_outcome() -> None:
    email = _email()

    submission = screen(email, ["techary.ai"], [], [])

    assert isinstance(submission, Email)
    assert submission.model_dump(include=set(Email.model_fields)) == email.model_dump(
        include=set(Email.model_fields)
    )


def test_passing_submission_keeps_its_fields_and_body() -> None:
    submission = screen(_email(), ["techary.ai"], [], [])

    assert submission.rejection is None
    assert submission.message_id == "m01"
    assert submission.sender_name == "Priya Shah"
    assert submission.sender_address == "priya.shah@techary.ai"
    assert submission.subject == "Signed Northwind Retail today"
    assert submission.received == datetime(2026, 9, 22, 15, 30, tzinfo=UTC)
    assert submission.has_attachments
    assert submission.body == "Tom Evans and I signed Northwind Retail on 22 September."


def test_rejected_submission_drops_its_body() -> None:
    submission = screen(_email("alex.morgan@example.com"), ["techary.ai"], [], [])

    assert submission.rejection == "sender_domain"
    assert submission.body is None
    assert submission.subject == "Signed Northwind Retail today"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("priya.shah@techary.ai", None),
        ("Priya.Shah@TECHARY.ai", None),
        ("alex.morgan@example.com", "sender_domain"),
        ("alex.morgan@mail.techary.ai", "sender_domain"),
        ("alex.morgan@techary.ai.example.com", "sender_domain"),
        ("no-domain", "sender_domain"),
    ],
)
def test_sender_domain(address: str, expected: str | None) -> None:
    assert _screen(_email(address)) == expected


def test_empty_allowed_senders_allows_any_sender_in_domain() -> None:
    assert _screen(_email("anyone@techary.ai"), senders=[]) is None


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("priya.shah@techary.ai", None),
        ("PRIYA.SHAH@techary.ai", None),
        ("tom.evans@techary.ai", "sender_not_allowed"),
    ],
)
def test_allowed_senders(address: str, expected: str | None) -> None:
    assert _screen(_email(address), senders=["priya.shah@techary.ai"]) == expected


def test_domain_is_checked_before_allowed_senders() -> None:
    email = _email("alex.morgan@example.com")

    assert _screen(email, senders=["alex.morgan@example.com"]) == "sender_domain"


def _labels(value: str) -> InboundEmail:
    return _email(headers={"msip_labels": value})


@pytest.mark.parametrize(
    ("header", "allowed", "expected"),
    [
        (f"MSIP_Label_{LABEL}_Enabled=true", [], "sensitivity_label"),
        (f"MSIP_Label_{LABEL}_Enabled=TRUE", [], "sensitivity_label"),
        (f"MSIP_Label_{LABEL}_Enabled=true", [LABEL], None),
        (f"MSIP_Label_{LABEL}_Enabled=true", [LABEL.upper()], None),
        (f"MSIP_Label_{LABEL}_Enabled=false", [], None),
        (f"MSIP_Label_{LABEL}_Name=Confidential; MSIP_Label_{LABEL}_SiteId=x", [], None),
        (
            f"MSIP_Label_{LABEL}_Enabled=true; MSIP_Label_{LABEL}_Name=General;"
            f" MSIP_Label_{OTHER_LABEL}_Enabled=true;",
            [LABEL],
            "sensitivity_label",
        ),
        (
            f"MSIP_Label_{LABEL}_Enabled=true; MSIP_Label_{OTHER_LABEL}_Enabled=true",
            [LABEL, OTHER_LABEL],
            None,
        ),
        ("", [], None),
        ("not a label entry", [LABEL], "sensitivity_label"),
        (f"MSIP_Label_{LABEL}_Enabled=true; garbage", [LABEL], "sensitivity_label"),
    ],
)
def test_sensitivity_labels(header: str, allowed: list[str], expected: str | None) -> None:
    assert _screen(_labels(header), labels=allowed) == expected


def test_label_header_name_ignores_case() -> None:
    email = _email(headers={"MSIP_Labels": f"MSIP_Label_{LABEL}_Enabled=true"})

    assert _screen(email) == "sensitivity_label"


def test_unlabelled_message_is_allowed() -> None:
    assert _screen(_email(), labels=[LABEL]) is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Auto-Submitted": "no"}, None),
        ({"Auto-Submitted": "auto-replied"}, "automatic_reply"),
        ({"X-Auto-Response-Suppress": "All"}, "automatic_reply"),
    ],
)
def test_automatic_replies(headers: dict[str, str], expected: str | None) -> None:
    assert _screen(_email(headers=headers)) == expected


def test_source_text_is_subject_and_body() -> None:
    text = source_text(screen(_email(), ["techary.ai"], [], []))

    assert "Signed Northwind Retail today" in text
    assert "22 September" in text


def test_source_text_of_rejected_submission_is_subject_only() -> None:
    text = source_text(screen(_email("alex.morgan@example.com"), ["techary.ai"], [], []))

    assert text == "Signed Northwind Retail today"
