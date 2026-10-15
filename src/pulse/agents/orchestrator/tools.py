"""The orchestrator's tools: Tools and the results they return."""

import asyncio
import functools
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, Self

from pydantic import AwareDatetime
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from pulse.agents.base import all_finished
from pulse.agents.consolidator.agent import ConsolidatorAgent, ConsolidatorTask
from pulse.agents.decomposer.agent import DecomposerAgent
from pulse.agents.extractor.agent import ExtractorAgent
from pulse.agents.judge.agent import JudgeAgent, JudgeTask
from pulse.agents.sensitivity.agent import SensitivityAgent
from pulse.agents.writer.agent import WriterAgent, WriterTask
from pulse.entities.base import Entity
from pulse.entities.content import Verdict, draft_changed
from pulse.entities.conversation import ReviewerMessage
from pulse.entities.errors import Refusal, SpecialistFailed
from pulse.entities.extracts import (
    Consolidation,
    ExclusionReason,
    Extraction,
    ExtractorOutput,
    ExtractRecord,
    SourcedItem,
    consolidation_input,
    extracted_news,
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
    "judge": "Checking the draft",
    "present_draft": "Sending the draft to the reviewers",
    "approve": "Recording your approval",
    "withdraw_approval": "Withdrawing the approval",
    "abandon": "Abandoning the newsletter",
}
_DEFAULT_NOTE = "Working on it"


def progress_note(tool: str) -> str:
    return f"{PROGRESS_NOTES.get(tool, _DEFAULT_NOTE)} ({tool})"


# Subjects and bodies stay with the extractor and the sensitivity agent, the only agents that
# read them.
_LISTED_FIELDS = {"message_id", "sender_name", "received", "has_attachments", "rejection"}
_CONSOLIDATED_FIELDS = {"item_id", "category", "source_message_ids"}


class ExtractOutcome(Entity):
    """One email's result from extract, with who sent it and when, so the orchestrator can name
    it to a reviewer."""

    message_id: MessageId
    sender_name: str
    received: AwareDatetime
    exclusion: ExclusionReason | None = None
    excluded_id: str | None = None
    # Why the email was not extracted, when it was not.
    error: str | None = None


class ShowResult(Entity):
    """The newsletter show_draft shows: a version, or the working draft when None."""

    version: int | None


class ExtractResult(Entity):
    """Each email's outcome from extract, with the totals included, excluded, excluded by
    reason, and failed."""

    outcomes: list[ExtractOutcome]
    included: int
    excluded: int
    excluded_by_reason: dict[ExclusionReason, int]
    failed: int

    @classmethod
    def of(cls, outcomes: list[ExtractOutcome]) -> Self:
        extracted = [outcome for outcome in outcomes if outcome.error is None]
        by_reason = Counter(o.exclusion for o in extracted if o.exclusion is not None)
        return cls(
            outcomes=outcomes,
            included=sum(1 for outcome in extracted if outcome.exclusion is None),
            excluded=by_reason.total(),
            excluded_by_reason=by_reason,
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


def _named(email: ScreenedEmail) -> dict[str, Any]:
    """The fields that name an email in its extract outcome."""
    return email.model_dump(include={"message_id", "sender_name", "received"})


class Tools:
    """The orchestrator's toolset. Each tool reads the store, runs a specialist agent or calls
    Operations, and returns a refusal's reason as its result."""

    def __init__(
        self,
        operations: Operations,
        store: Store,
        extractor: ExtractorAgent,
        decomposer: DecomposerAgent,
        sensitivity: SensitivityAgent,
        consolidator: ConsolidatorAgent,
        writer: WriterAgent,
        judge: JudgeAgent,
    ) -> None:
        self._operations = operations
        self._store = store
        self._extractor = extractor
        self._decomposer = decomposer
        self._sensitivity = sensitivity
        self._consolidator = consolidator
        self._writer = writer
        self._judge = judge

    def toolset(self) -> FunctionToolset[ReviewerMessage]:
        # A tool can read what an earlier tool in the same response stored, such as consolidate
        # reading extract's records, so tools called together run one at a time, in call order.
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
                self.judge,
                self.present_draft,
                self.approve,
                self.withdraw_approval,
                self.abandon,
            ],
            sequential=True,
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
        summary = await self._operations.summary()
        return "No newsletter exists yet." if summary is None else summary

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
    async def extract(self) -> ExtractResult:
        """Run the extractor and the sensitivity agent, in parallel, on every screened email of
        the open newsletter that passed the pre-filter and has no extract record yet, and store
        each extract record with its exclusion outcome. An email that failed has no record, so
        calling this again retries it. Returns each email's outcome and the totals included,
        excluded, excluded by reason, and failed."""
        newsletter = await self._open()
        newsletter_id = newsletter.newsletter_id
        extracted = {r.message_id for r in await self._store.list_extract_records(newsletter_id)}
        pending = [
            email
            for email in await self._store.list_screened_emails(newsletter_id)
            if email.rejection is None and email.message_id not in extracted
        ]
        outcomes = await all_finished(self._extract_one(newsletter_id, email) for email in pending)
        return ExtractResult.of(outcomes)

    async def _extract_one(self, newsletter_id: str, email: ScreenedEmail) -> ExtractOutcome:
        message_id = email.message_id
        # Sensitivity is judged in its own call, so a flag is never dropped while the extractor
        # works on the news.
        extracted, flagged = await asyncio.gather(
            self._extracted(email), self._sensitivity.answer(email), return_exceptions=True
        )
        if isinstance(extracted, BaseException) or isinstance(flagged, BaseException):
            for result in (extracted, flagged):
                if isinstance(result, BaseException) and not isinstance(result, SpecialistFailed):
                    raise result
            failures = [str(r) for r in (extracted, flagged) if isinstance(r, SpecialistFailed)]
            return ExtractOutcome(**_named(email), error="; ".join(failures))
        extraction = Extraction(**extracted_news(extracted).model_dump(), **flagged.model_dump())
        record = await self._store.save_extract(newsletter_id, message_id, extraction)
        return ExtractOutcome(
            **_named(email), exclusion=record.exclusion, excluded_id=record.excluded_id
        )

    async def _extracted(self, email: ScreenedEmail) -> ExtractorOutput:
        """The extractor's answer with each line's facts split by the decomposer."""
        return await self._decomposer.split(await self._extractor.answer(email), email)

    @_reported
    async def restore(
        self, ctx: RunContext[ReviewerMessage], excluded_id: str, section: str | None = None
    ) -> RestoreResult:
        """Include the named excluded record, only when a reviewer's feedback asks for it. A
        record that fits no section needs `section`, the title of the section the reviewer
        wants it in; without one, the refusal lists the sections to ask the reviewer about. The
        items are then out of date, so call consolidate next with every included record."""
        # The caller comes from the run, never from the model.
        return await self._operations.restore(excluded_id, ctx.deps.author, section)

    @_reported
    async def consolidate(self) -> dict[str, Any]:
        """Run the consolidator on every included extract record of the open newsletter, and
        store the resulting items and headline, replacing any earlier ones."""
        newsletter = await self._open()
        records = await self._store.list_extract_records(newsletter.newsletter_id)
        selected = consolidation_input(records)
        output = await self._consolidator.answer(ConsolidatorTask(records=selected))
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
        output = await self._writer.answer(
            WriterTask(
                headline=consolidated.headline if consolidated else None,
                items=items,
                excluded=excluded,
                feedback=feedback,
                instruction=instruction,
                draft=draft,
            )
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
    async def judge(self) -> dict[str, Any]:
        """Run the code checks on the working draft, then the judge on its intro and on each
        entry, in parallel, which finds what does not keep to the spirit of the facts and
        feedback, and store its verdicts. Returns every check failure and their count, how many
        parts it judged and, for each part that fails, what does not keep to its facts and
        why."""
        failures = await self._operations.check()
        newsletter = await self._open()
        newsletter_id = newsletter.newsletter_id
        content = (await self._operations.draft_or_version(newsletter, None)).content
        _, items, _ = await self._items(newsletter_id)
        feedback = await self._store.list_feedback(newsletter_id)
        by_id = {item.item_id: item for item in items}
        # One text per call: the intro may draw on any item, an entry only on its own.
        targets = ["intro"]
        tasks = [JudgeTask(part="intro", text=content.intro, items=items, feedback=feedback)]
        for entry in content.entries():
            own = [by_id[entry.item_id]] if entry.item_id in by_id else []
            targets.append(entry.item_id)
            tasks.append(JudgeTask(part="entry", text=entry.text, items=own, feedback=feedback))
        outputs = await all_finished(self._judge.answer(task) for task in tasks)
        verdicts = [
            Verdict(target=target, **output.model_dump())
            for target, output in zip(targets, outputs, strict=True)
        ]
        await self._store.save_verdicts(newsletter_id, verdicts)
        return {
            "failure_count": len(failures),
            "failures": [failure.model_dump(mode="json") for failure in failures],
            "judged": len(verdicts),
            "unsupported": [
                {
                    "target": verdict.target,
                    "unsupported": [part.model_dump() for part in verdict.unsupported],
                }
                for verdict in verdicts
                if not verdict.supported
            ],
        }

    @_reported
    async def present_draft(self, ctx: RunContext[ReviewerMessage]) -> PresentResult:
        """Save the working draft as the next version and email it to the reviewers,
        withdrawing any approval first. When the working draft is unchanged since the latest
        version, email that version again instead, with the same number and no other change.
        Returns the version number; `problems`, the check failures and unsupported claims it was
        presented with, worded as the reviewers see them; `flagged`, how many of its entries hold
        sensitive content that the reviewers check before approving, which is not a problem with
        the text; whether it was resent; and whether an approval was withdrawn and, if so,
        whether the reviewers were told."""
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
