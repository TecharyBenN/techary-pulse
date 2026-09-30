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
from pulse.entities.base import Entity
from pulse.entities.errors import Refusal, SpecialistFailed
from pulse.entities.extracts import (
    ConsolidatorOutput,
    ExclusionReason,
    ExtractorOutput,
    consolidation_input,
    items_up_to_date,
    make_consolidation,
    with_sources,
)
from pulse.entities.lifecycle import Newsletter
from pulse.entities.mail import MessageId, ScreenedEmail
from pulse.entities.store import Store
from pulse.services.operations import Operations, StartResult

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
        categories: Mapping[str, str],
    ) -> None:
        self._operations = operations
        self._store = store
        self._extractor = extractor
        self._consolidator = consolidator
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
            ]
        )

    @_reported
    async def start_newsletter(self) -> StartResult:
        """Open a newsletter with the pending emails, or add those that have arrived since to
        the open one, applying the pre-filter to them."""
        return await self._operations.start_newsletter()

    async def get_newsletter(self) -> dict[str, Any] | str:
        """Summarise the open newsletter: its state, versions presented, item and excluded
        record counts, whether the items are up to date with the included records, approved
        version and send time."""
        newsletter = await self._store.get_open_newsletter()
        if newsletter is None:
            return "No newsletter is open."
        consolidated = await self._store.get_items(newsletter.newsletter_id)
        records = await self._store.list_extract_records(newsletter.newsletter_id)
        excluded = sum(1 for record in records if record.exclusion is not None)
        summary = newsletter.model_dump(mode="json", include=_SUMMARY_FIELDS)
        return summary | {
            "items": len(consolidated.items) if consolidated else 0,
            "items_up_to_date": items_up_to_date(consolidated, records),
            "excluded_records": excluded,
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
        newsletter_id = newsletter.newsletter_id
        consolidated = await self._store.get_items(newsletter_id)
        emails = await self._store.list_screened_emails(newsletter_id)
        records = await self._store.list_extract_records(newsletter_id)
        items = with_sources(consolidated.items, emails) if consolidated else []
        return {
            "headline": consolidated.headline if consolidated else None,
            "items": [item.model_dump(mode="json") for item in items],
            "excluded_records": [
                record.model_dump(mode="json") for record in records if record.exclusion is not None
            ],
        }

    async def _open(self) -> Newsletter:
        newsletter = await self._store.get_open_newsletter()
        if newsletter is None:
            raise Refusal("no newsletter is open")
        return newsletter
