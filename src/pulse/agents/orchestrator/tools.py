"""The orchestrator's tools: Tools and the results they return."""

import asyncio
import functools
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Self

from pydantic_ai import Agent, RunContext
from pydantic_ai.toolsets import FunctionToolset

from pulse.agents.consolidator import agent as consolidator_agent
from pulse.agents.extractor import agent as extractor_agent
from pulse.agents.judge import agent as judge_agent
from pulse.agents.runner import run_specialist
from pulse.agents.writer import agent as writer_agent
from pulse.entities.base import Entity
from pulse.entities.content import JudgeOutput, WriterOutput, draft_changed
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import Refusal, SpecialistFailed
from pulse.entities.extracts import (
    Consolidation,
    ConsolidatorOutput,
    ExclusionReason,
    ExtractorOutput,
    ExtractRecord,
    SourcedItem,
    consolidation_input,
    items_up_to_date,
    make_consolidation,
    with_sources,
)
from pulse.entities.lifecycle import Newsletter, require_latest, require_open
from pulse.entities.mail import MessageId, ScreenedEmail
from pulse.entities.store import Store
from pulse.services.operations import (
    ApproveResult,
    NoticeResult,
    Operations,
    PresentResult,
    RestoreResult,
    StartResult,
)

# What a reviewer sees while each tool runs; a tool without a note shows the default.
PROGRESS_NOTES = {
    "get_newsletter": "Checking the current newsletter",
    "start_newsletter": "Collecting new updates",
    "list_screened_emails": "Looking through the updates",
    "extract": "Reading the updates",
    "restore": "Putting the update back in",
    "consolidate": "Combining related news",
    "get_items": "Looking at the news items",
    "write": "Writing the draft",
    "get_draft": "Reading the draft",
    "show_draft": "Getting the draft ready to show you",
    "check": "Checking the draft",
    "judge": "Checking the draft against the facts",
    "present_draft": "Sending the draft to the reviewers",
    "approve": "Recording your approval",
    "withdraw_approval": "Withdrawing the approval",
    "abandon": "Abandoning the newsletter",
}
_DEFAULT_NOTE = "Working on it"


def progress_note(tool: str) -> str:
    return f"{PROGRESS_NOTES.get(tool, _DEFAULT_NOTE)} ({tool})"


# Subjects and bodies stay with the extractor, the only agent that reads them.
_LISTED_FIELDS = {"message_id", "sender_name", "received", "has_attachments", "rejection"}
_SUMMARY_FIELDS = {
    "newsletter_id",
    "state",
    "latest_version",
    "approved_version",
    "send_time",
    "sent_at",
}
_CONSOLIDATED_FIELDS = {"item_id", "category", "source_message_ids"}


class ExtractOutcome(Entity):
    """One email's result from extract."""

    message_id: MessageId
    exclusion: ExclusionReason | None = None
    excluded_id: str | None = None
    # Why the email was not extracted, when it was not.
    error: str | None = None


class ShowResult(Entity):
    """The newsletter show_draft shows: a version, or the working draft when None."""

    version: int | None


class ExtractResult(Entity):
    """Each email's outcome from extract, with the totals included, excluded and failed."""

    outcomes: list[ExtractOutcome]
    included: int
    excluded: dict[ExclusionReason, int]
    failed: int

    @classmethod
    def of(cls, outcomes: list[ExtractOutcome]) -> Self:
        extracted = [outcome for outcome in outcomes if outcome.error is None]
        return cls(
            outcomes=outcomes,
            included=sum(1 for outcome in extracted if outcome.exclusion is None),
            excluded=Counter(o.exclusion for o in extracted if o.exclusion is not None),
            failed=len(outcomes) - len(extracted),
        )


def _reported[**P, T](tool: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T | str]]:
    """Return a refusal, or a specialist agent's failure, to the orchestrator as the tool's
    result, with its reason."""

    @functools.wraps(tool)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | str:
        try:
            return await tool(*args, **kwargs)
        except Refusal as refusal:
            return f"Refused: {refusal}"
        except SpecialistFailed as failure:
            return f"Failed: {failure}"

    return wrapper


class Tools:
    """The orchestrator's toolset. Each tool reads the store, runs a specialist agent or calls
    Operations, and returns a refusal's reason as its result."""

    def __init__(
        self,
        operations: Operations,
        store: Store,
        extractor: Agent[None, ExtractorOutput],
        consolidator: Agent[None, ConsolidatorOutput],
        writer: Agent[None, WriterOutput],
        judge: Agent[None, JudgeOutput],
        categories: Mapping[str, str],
    ) -> None:
        self._operations = operations
        self._store = store
        self._extractor = extractor
        self._consolidator = consolidator
        self._writer = writer
        self._judge = judge
        self._categories = categories

    def toolset(self) -> FunctionToolset[ReviewerMessage]:
        return FunctionToolset(
            [
                self.start_newsletter,
                self.get_newsletter,
                self.list_screened_emails,
                self.extract,
                self.restore,
                self.consolidate,
                self.get_items,
                self.write,
                self.get_draft,
                self.show_draft,
                self.check,
                self.judge,
                self.present_draft,
                self.approve,
                self.withdraw_approval,
                self.abandon,
            ]
        )

    @_reported
    async def start_newsletter(self) -> StartResult:
        """Open a newsletter with the pending emails, or add those that have arrived since to
        the open one, applying the pre-filter to them."""
        return await self._operations.start_newsletter()

    async def get_newsletter(self) -> dict[str, Any] | str:
        """Summarise the latest newsletter, open, sent or abandoned: its state, versions
        presented, item and excluded record counts, whether the items are up to date with the
        included records, whether the working draft has changed since the latest version,
        approved version, send time and sent time."""
        newsletter = await self._store.get_latest_newsletter()
        if newsletter is None:
            return "No newsletter exists yet."
        newsletter_id = newsletter.newsletter_id
        consolidated = await self._store.get_items(newsletter_id)
        records = await self._store.list_extract_records(newsletter_id)
        excluded = sum(1 for record in records if record.exclusion is not None)
        draft = await self._store.get_draft(newsletter_id)
        summary = newsletter.model_dump(mode="json", include=_SUMMARY_FIELDS)
        return summary | {
            "items": len(consolidated.items) if consolidated else 0,
            "items_up_to_date": items_up_to_date(consolidated, records),
            "excluded_records": excluded,
            "draft_changed": draft_changed(
                draft, await self._operations.latest_version(newsletter)
            ),
        }

    @_reported
    async def list_screened_emails(self) -> list[dict[str, Any]]:
        """List the latest newsletter's screened emails: message ID, sender name, received time,
        attachment flag, pre-filter outcome and extract record, if there is one."""
        newsletter = await self._latest()
        emails = await self._store.list_screened_emails(newsletter.newsletter_id)
        records = {
            record.message_id: record.model_dump(mode="json")
            for record in await self._store.list_extract_records(newsletter.newsletter_id)
        }
        return [
            email.model_dump(mode="json", include=_LISTED_FIELDS)
            | {"extract": records.get(email.message_id)}
            for email in emails
        ]

    @_reported
    async def extract(self, message_ids: list[MessageId]) -> ExtractResult:
        """Run the extractor on the named screened emails, in parallel, and store each extract
        record with its exclusion outcome. Extracting an email again replaces its record.
        Returns each email's outcome and the totals included, excluded by reason, and failed."""
        newsletter = await self._open()
        newsletter_id = newsletter.newsletter_id
        emails = {e.message_id: e for e in await self._store.list_screened_emails(newsletter_id)}
        outcomes = await asyncio.gather(
            *(
                self._extract_one(newsletter_id, message_id, emails.get(message_id))
                for message_id in dict.fromkeys(message_ids)
            )
        )
        return ExtractResult.of(list(outcomes))

    async def _extract_one(
        self, newsletter_id: str, message_id: MessageId, email: ScreenedEmail | None
    ) -> ExtractOutcome:
        if email is None:
            return ExtractOutcome(
                message_id=message_id, error="not a screened email of the open newsletter"
            )
        if email.rejection is not None:
            return ExtractOutcome(
                message_id=message_id, error=f"the pre-filter rejected it: {email.rejection}"
            )
        try:
            output = await run_specialist(
                self._extractor,
                extractor_agent.extractor_prompt(email),
                lambda output: extractor_agent.output_checks(output, self._categories),
            )
        except SpecialistFailed as failure:
            return ExtractOutcome(message_id=message_id, error=str(failure))
        record = await self._store.save_extract(newsletter_id, message_id, output)
        return ExtractOutcome(
            message_id=message_id, exclusion=record.exclusion, excluded_id=record.excluded_id
        )

    @_reported
    async def restore(self, ctx: RunContext[ReviewerMessage], excluded_id: str) -> RestoreResult:
        """Include the named excluded record, only when a reviewer's feedback asks for it. The
        items are then out of date, so call consolidate next with every included record."""
        # The caller comes from the run, never from the model.
        return await self._operations.restore(excluded_id, ctx.deps.author)

    @_reported
    async def consolidate(self, message_ids: list[MessageId]) -> dict[str, Any]:
        """Run the consolidator on the named extract records, which must all be included, and
        store the resulting items and headline, replacing any earlier ones. Name every included
        record, because records not named are left out of the items."""
        newsletter = await self._open()
        records = await self._store.list_extract_records(newsletter.newsletter_id)
        selected = consolidation_input(records, message_ids)
        output = await run_specialist(
            self._consolidator,
            consolidator_agent.consolidator_prompt(selected),
            lambda output: consolidator_agent.output_checks(output, selected, self._categories),
        )
        consolidation = make_consolidation(output, selected)
        await self._store.save_items(newsletter.newsletter_id, consolidation)
        return {
            "headline": consolidation.headline,
            "item_count": len(consolidation.items),
            "items": [
                item.model_dump(mode="json", include=_CONSOLIDATED_FIELDS)
                for item in consolidation.items
            ],
        }

    @_reported
    async def get_items(self) -> dict[str, Any]:
        """Return the latest newsletter's headline, current items with their sender names and
        received times, and excluded records."""
        newsletter = await self._latest()
        consolidated, items, excluded = await self._items(newsletter.newsletter_id)
        return {
            "headline": consolidated.headline if consolidated else None,
            "items": [item.model_dump(mode="json") for item in items],
            "excluded_records": [record.model_dump(mode="json") for record in excluded],
        }

    @_reported
    async def write(self, instruction: str) -> dict[str, Any]:
        """Run the writer on the items, the excluded records and all feedback, following your
        instruction, and store the result as the working draft. When a working draft exists,
        the writer revises it. Returns the included IDs, the changes, any feedback not applied,
        and whether the draft now differs from the latest version."""
        newsletter = await self._open()
        newsletter_id = newsletter.newsletter_id
        consolidated, items, excluded = await self._items(newsletter_id)
        feedback = await self._store.list_feedback(newsletter_id)
        draft = await self._store.get_draft(newsletter_id)
        known = [item.item_id for item in items]
        output = await run_specialist(
            self._writer,
            writer_agent.writer_prompt(consolidated, items, excluded, feedback, instruction, draft),
            lambda output: writer_agent.output_checks(output, known),
        )
        await self._store.save_draft(newsletter_id, output)
        return output.model_dump(mode="json", include={"changes", "not_applied"}) | {
            "item_ids": output.content.item_ids,
            "draft_changed": draft_changed(
                output, await self._operations.latest_version(newsletter)
            ),
        }

    @_reported
    async def get_draft(self, version: int | None = None) -> dict[str, Any]:
        """Return the latest newsletter's working draft, or, when a version is named, that
        presented version."""
        newsletter = await self._latest()
        draft = await self._operations.draft_or_version(newsletter, version)
        return draft.model_dump(mode="json")

    @_reported
    async def show_draft(self, version: int | None = None) -> ShowResult:
        """Show the reviewer the latest newsletter's working draft, or, when a version is named,
        that presented version. Pulse shows the newsletter after your reply, so never write it
        out yourself."""
        await self._operations.draft_or_version(await self._latest(), version)
        return ShowResult(version=version)

    @_reported
    async def check(self) -> dict[str, Any]:
        """Run the code checks on the working draft. Returns every failure and their count."""
        failures = await self._operations.check()
        return {
            "failure_count": len(failures),
            "failures": [failure.model_dump(mode="json") for failure in failures],
        }

    @_reported
    async def judge(self) -> dict[str, Any]:
        """Run the judge on the working draft, which says whether the intro and each entry are
        supported by the facts and feedback, and store its verdicts. Returns how many parts it
        judged and each unsupported claim."""
        newsletter = await self._open()
        newsletter_id = newsletter.newsletter_id
        content = (await self._operations.draft_or_version(newsletter, None)).content
        _, items, _ = await self._items(newsletter_id)
        feedback = await self._store.list_feedback(newsletter_id)
        output = await run_specialist(
            self._judge,
            judge_agent.judge_prompt(content, items, feedback),
            lambda output: judge_agent.output_checks(output, content),
        )
        await self._store.save_verdicts(newsletter_id, output.verdicts)
        return {
            "judged": len(output.verdicts),
            "unsupported": [
                verdict.model_dump(mode="json", include={"target", "claim"})
                for verdict in output.verdicts
                if not verdict.supported
            ],
        }

    @_reported
    async def present_draft(self, ctx: RunContext[ReviewerMessage]) -> PresentResult:
        """Save the working draft as the next version and email it to the reviewers,
        withdrawing any approval first. When the working draft is unchanged since the latest
        version, email that version again instead, with the same number and no other change.
        Returns the version number, whether it was resent, whether an approval was withdrawn
        and, if so, whether the reviewers were told."""
        # In the email channel, the reply carries the version, so reviewers get one email.
        email_reviewers = ctx.deps.channel != "email"
        return await self._operations.present_draft(email_reviewers=email_reviewers)

    @_reported
    async def approve(self, ctx: RunContext[ReviewerMessage], version: int) -> ApproveResult:
        """Record the reviewer's approval of the named version, which must be the latest
        presented version, and set its send time. Pulse checks that the reviewer's own message
        starts with approve v{version}. Returns the version and the send time."""
        # The caller and their words come from the run, never from the model.
        return await self._operations.approve(version, ctx.deps.author, ctx.deps.text)

    @_reported
    async def withdraw_approval(self, ctx: RunContext[ReviewerMessage]) -> NoticeResult:
        """Withdraw the approval, so the newsletter is not sent, and email the reviewers that
        the send is cancelled. Returns whether that email was sent."""
        return await self._operations.withdraw_approval(ctx.deps.author)

    @_reported
    async def abandon(self, ctx: RunContext[ReviewerMessage]) -> NoticeResult:
        """Close the newsletter unsent, only when a reviewer explicitly asks, and email the
        reviewers that it was abandoned. Returns whether that email was sent."""
        return await self._operations.abandon(ctx.deps.author)

    async def _items(
        self, newsletter_id: str
    ) -> tuple[Consolidation | None, list[SourcedItem], list[ExtractRecord]]:
        """The current items with their sources, and the excluded records."""
        consolidated = await self._store.get_items(newsletter_id)
        emails = await self._store.list_screened_emails(newsletter_id)
        records = await self._store.list_extract_records(newsletter_id)
        items = with_sources(consolidated.items, emails) if consolidated else []
        return consolidated, items, [r for r in records if r.exclusion is not None]

    async def _open(self) -> Newsletter:
        return require_open(await self._store.get_open_newsletter())

    async def _latest(self) -> Newsletter:
        """The newsletter read tools work on, so reviewers can still ask about it once closed."""
        return require_latest(await self._store.get_latest_newsletter())
