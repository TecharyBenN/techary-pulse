"""The orchestrator's tools: each checks its own preconditions and returns the reason it refuses."""

import asyncio
import functools
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Self

from pydantic_ai import Agent
from pydantic_ai.toolsets import FunctionToolset

from pulse.agents.consolidator import agent as consolidator_agent
from pulse.agents.extractor import agent as extractor_agent
from pulse.agents.runner import run_specialist
from pulse.agents.writer import agent as writer_agent
from pulse.entities.base import Entity
from pulse.entities.content import Version, WriterOutput, draft_changed
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
from pulse.entities.lifecycle import Newsletter, require_open
from pulse.entities.mail import MessageId, ScreenedEmail
from pulse.entities.store import Store
from pulse.services.operations import Operations, PresentResult, StartResult

# What a reviewer sees while each tool runs; a tool without a note shows the default.
PROGRESS_NOTES = {
    "get_newsletter": "Checking the current newsletter",
    "start_newsletter": "Collecting new updates",
    "list_screened_emails": "Looking through the updates",
    "extract": "Reading the updates",
    "consolidate": "Combining related news",
    "get_items": "Looking at the news items",
    "write": "Writing the draft",
    "get_draft": "Reading the draft",
    "show_draft": "Getting the draft ready to show you",
    "present_draft": "Sending the draft to the reviewers",
}
_DEFAULT_NOTE = "Working on it"


def progress_note(tool: str) -> str:
    return f"{PROGRESS_NOTES.get(tool, _DEFAULT_NOTE)} ({tool})"


# Subjects and bodies stay with the extractor, the only agent that reads them.
_LISTED_FIELDS = {"message_id", "sender_name", "received", "has_attachments", "rejection"}
_SUMMARY_FIELDS = {"newsletter_id", "state", "latest_version", "approved_version", "send_time"}
_CONSOLIDATED_FIELDS = {"item_id", "category", "source_message_ids"}


class ExtractOutcome(Entity):
    message_id: MessageId
    exclusion: ExclusionReason | None = None
    excluded_id: str | None = None
    # Why the email was not extracted, when it was not.
    error: str | None = None


class ShowResult(Entity):
    """The newsletter a run shows the reviewer: a version, or the working draft when None."""

    version: int | None


class ExtractResult(Entity):
    """Each email's outcome, with totals, so the orchestrator never has to count."""

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
    def __init__(
        self,
        operations: Operations,
        store: Store,
        extractor: Agent[None, ExtractorOutput],
        consolidator: Agent[None, ConsolidatorOutput],
        writer: Agent[None, WriterOutput],
        categories: Mapping[str, str],
    ) -> None:
        self._operations = operations
        self._store = store
        self._extractor = extractor
        self._consolidator = consolidator
        self._writer = writer
        self._categories = categories

    def toolset(self) -> FunctionToolset[None]:
        # Add a tool by writing its method and listing it here.
        return FunctionToolset(
            [
                self.start_newsletter,
                self.get_newsletter,
                self.list_screened_emails,
                self.extract,
                self.consolidate,
                self.get_items,
                self.write,
                self.get_draft,
                self.show_draft,
                self.present_draft,
            ]
        )

    @_reported
    async def start_newsletter(self) -> StartResult:
        """Open a newsletter with the pending emails, or add those that have arrived since to
        the open one, applying the pre-filter to them."""
        return await self._operations.start_newsletter()

    async def get_newsletter(self) -> dict[str, Any] | str:
        """Summarise the open newsletter: its state, versions presented, item and excluded
        record counts, whether the items are up to date with the included records, whether the
        working draft has changed since the latest version, approved version and send time."""
        newsletter = await self._store.get_open_newsletter()
        if newsletter is None:
            return "No newsletter is open."
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
            "draft_changed": draft_changed(draft, await self._latest_version(newsletter)),
        }

    @_reported
    async def list_screened_emails(self) -> list[dict[str, Any]]:
        """List the open newsletter's screened emails: message ID, sender name, received time,
        attachment flag, pre-filter outcome and extract record, if there is one."""
        newsletter = await self._open()
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
            lambda output: consolidator_agent.output_checks(output, selected),
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
        """Return the headline, the current items with their sender names and received times,
        and the excluded records."""
        newsletter = await self._open()
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
        known = [item.item_id for item in items] + [
            record.excluded_id for record in excluded if record.excluded_id
        ]
        output = await run_specialist(
            self._writer,
            writer_agent.writer_prompt(consolidated, items, excluded, feedback, instruction, draft),
            lambda output: writer_agent.output_checks(output, known),
        )
        await self._store.save_draft(newsletter_id, output)
        return output.model_dump(mode="json", include={"changes", "not_applied"}) | {
            "item_ids": output.content.item_ids,
            "draft_changed": draft_changed(output, await self._latest_version(newsletter)),
        }

    @_reported
    async def get_draft(self, version: int | None = None) -> dict[str, Any]:
        """Return the working draft, or, when a version is named, that presented version."""
        return (await self._draft_or_version(version)).model_dump(mode="json")

    @_reported
    async def show_draft(self, version: int | None = None) -> ShowResult:
        """Show the reviewer the working draft, or, when a version is named, that presented
        version. Pulse shows the newsletter after your reply, so never write it out yourself."""
        await self._draft_or_version(version)
        return ShowResult(version=version)

    @_reported
    async def present_draft(self) -> PresentResult:
        """Save the working draft as the next version and email it to the reviewers. Returns
        the version number."""
        return await self._operations.present_draft()

    async def _draft_or_version(self, version: int | None) -> WriterOutput:
        """The working draft, or the named version; refuse when there is none."""
        newsletter_id = (await self._open()).newsletter_id
        if version is None:
            draft = await self._store.get_draft(newsletter_id)
            if draft is None:
                raise Refusal("there is no working draft")
            return draft
        presented = await self._store.get_version(newsletter_id, version)
        if presented is None:
            raise Refusal(f"v{version} has not been presented")
        return presented

    async def _latest_version(self, newsletter: Newsletter) -> Version | None:
        latest = newsletter.latest_version
        return await self._store.get_version(newsletter.newsletter_id, latest) if latest else None

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
