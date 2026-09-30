"""The orchestrator's tools: each checks its own preconditions and returns the reason it refuses."""

import asyncio
import functools
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.toolsets import FunctionToolset

from pulse.agents.extractor.agent import extractor_prompt, output_checks
from pulse.agents.runner import run_specialist
from pulse.entities.base import Entity
from pulse.entities.errors import Refusal, SpecialistFailed
from pulse.entities.extracts import ExclusionReason, ExtractorOutput
from pulse.entities.lifecycle import Newsletter
from pulse.entities.store import Store
from pulse.entities.submissions import Submission
from pulse.services.operations import Operations, StartResult

# Subjects and bodies stay with the extractor, the only agent that reads them.
_LISTED_FIELDS = {"message_id", "sender_name", "received", "has_attachments", "rejection"}
_SUMMARY_FIELDS = {"newsletter_id", "state", "latest_version", "approved_version", "send_time"}


class ExtractOutcome(Entity):
    message_id: str
    exclusion: ExclusionReason | None = None
    excluded_id: str | None = None
    # Why the submission was not extracted, when it was not.
    error: str | None = None


def _refusable[**P, T](tool: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T | str]]:
    """Return a refusal to the orchestrator as the tool's result, with its reason."""

    @functools.wraps(tool)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | str:
        try:
            return await tool(*args, **kwargs)
        except Refusal as refusal:
            return f"Refused: {refusal}"

    return wrapper


class Tools:
    def __init__(
        self,
        operations: Operations,
        store: Store,
        extractor: Agent[None, ExtractorOutput],
        categories: Mapping[str, str],
    ) -> None:
        self._operations = operations
        self._store = store
        self._extractor = extractor
        self._categories = categories

    def toolset(self) -> FunctionToolset[None]:
        # Add a tool by writing its method and listing it here.
        return FunctionToolset(
            [self.start_newsletter, self.get_newsletter, self.list_submissions, self.extract]
        )

    @_refusable
    async def start_newsletter(self) -> StartResult:
        """Open a newsletter with the pending submissions, or add those that have arrived since
        to the open one, applying the pre-filter to them."""
        return await self._operations.start_newsletter()

    async def get_newsletter(self) -> dict[str, Any] | str:
        """Summarise the open newsletter: its state, versions presented, excluded record count,
        approved version and send time."""
        newsletter = await self._store.get_open_newsletter()
        if newsletter is None:
            return "No newsletter is open."
        records = await self._store.list_extract_records(newsletter.newsletter_id)
        excluded = sum(1 for record in records if record.exclusion is not None)
        summary = newsletter.model_dump(mode="json", include=_SUMMARY_FIELDS)
        return summary | {"excluded_records": excluded}

    @_refusable
    async def list_submissions(self) -> list[dict[str, Any]]:
        """List the open newsletter's submissions: message ID, sender name, received time,
        attachment flag, pre-filter outcome and extract record, if there is one."""
        newsletter = await self._open()
        submissions = await self._store.list_submissions(newsletter.newsletter_id)
        records = {
            record.message_id: record.model_dump(mode="json")
            for record in await self._store.list_extract_records(newsletter.newsletter_id)
        }
        return [
            submission.model_dump(mode="json", include=_LISTED_FIELDS)
            | {"extract": records.get(submission.message_id)}
            for submission in submissions
        ]

    @_refusable
    async def extract(self, message_ids: list[str]) -> list[ExtractOutcome]:
        """Run the extractor on the named submissions, in parallel, and store each extract
        record with its exclusion outcome. Extracting a submission again replaces its record."""
        newsletter = await self._open()
        newsletter_id = newsletter.newsletter_id
        submissions = {s.message_id: s for s in await self._store.list_submissions(newsletter_id)}
        return list(
            await asyncio.gather(
                *(
                    self._extract_one(newsletter_id, message_id, submissions.get(message_id))
                    for message_id in dict.fromkeys(message_ids)
                )
            )
        )

    async def _extract_one(
        self, newsletter_id: str, message_id: str, submission: Submission | None
    ) -> ExtractOutcome:
        if submission is None:
            return ExtractOutcome(
                message_id=message_id, error="not a submission of the open newsletter"
            )
        if submission.rejection is not None:
            return ExtractOutcome(
                message_id=message_id,
                error=f"the pre-filter rejected it: {submission.rejection}",
            )
        try:
            output = await run_specialist(
                self._extractor,
                extractor_prompt(submission),
                lambda output: output_checks(output, submission, self._categories),
            )
        except SpecialistFailed as failure:
            return ExtractOutcome(message_id=message_id, error=str(failure))
        record = await self._store.save_extract(newsletter_id, output)
        return ExtractOutcome(
            message_id=message_id, exclusion=record.exclusion, excluded_id=record.excluded_id
        )

    async def _open(self) -> Newsletter:
        newsletter = await self._store.get_open_newsletter()
        if newsletter is None:
            raise Refusal("no newsletter is open")
        return newsletter
