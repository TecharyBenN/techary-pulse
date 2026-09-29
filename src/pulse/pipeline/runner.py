"""The build workflow's nine steps, and the revision workflow that shares its check loop."""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelRequest, UserPromptPart

from pulse.agents import Consolidator, Drafter, Extractor, Judge, Reviser
from pulse.agents.runner import AgentRunner
from pulse.config import Config
from pulse.errors import LockHeldError, StopRequested
from pulse.mail import Mailbox
from pulse.models import (
    CleanedEmail,
    Consolidation,
    Draft,
    DraftInput,
    Edition,
    ExcludedForReviser,
    ExtractRecord,
    ItemWithSenders,
    JudgeInput,
    Message,
    ReviseInput,
    Revision,
    Trigger,
    Version,
)
from pulse.pipeline.check import check_draft, judge_failures
from pulse.pipeline.render import version_email
from pulse.pipeline.rules import Exclusion, exclude, prefilter
from pulse.state import (
    Manifest,
    Outcome,
    RunArtefacts,
    delete_old_runs,
    latest_manifest,
    run_lock,
    save_manifest,
)
from pulse.store import EditionStore, ItemRow, SourceRow

log = logging.getLogger("pulse.pipeline")

Clock = Callable[[], datetime]

# The build's creator of record for version 1; later versions are created by a reviewer.
BUILDER = "pulse"

DraftAttempt = Callable[[list[str]], Awaitable[tuple[Draft, str, Sequence[ItemWithSenders]]]]


@dataclass(frozen=True)
class BuildResult:
    status: Literal["created", "open", "in_progress", "empty", "dry_run"]
    edition: Edition | None
    manifest: Manifest | None
    artefacts_dir: Path | None


def _folder(outcome: Outcome, config: Config) -> str:
    if outcome.status == "rejected":
        return config.mailboxes.rejected_folder
    return config.mailboxes.processed_folder


async def _move(
    mailbox: Mailbox, manifest: Manifest, ids: list[str], config: Config, save: Callable[[], None]
) -> None:
    for message_id in ids:
        await mailbox.move(message_id, _folder(manifest.outcomes[message_id], config))
        manifest.moved.append(message_id)
        save()


async def _resume(root: Path, submissions: Mailbox, config: Config, store: EditionStore) -> None:
    """Complete the moves of an earlier build whose edition was saved."""
    found = latest_manifest(root)
    if found is None:
        return
    run_dir, manifest = found
    if manifest.complete:
        return
    if await store.edition_for_build(manifest.run_id) is None:
        # No edition was saved for this build, so no message may be moved yet.
        return
    log.info("completing moves", extra={"run_id": manifest.run_id})
    await _move(
        submissions,
        manifest,
        manifest.pending_moves(),
        config,
        lambda: save_manifest(run_dir, manifest),
    )
    manifest.complete = True
    save_manifest(run_dir, manifest)


def _history_note(version: int, trigger: Trigger, requested_by: str | None) -> ModelRequest:
    """The conversation history entry recording that a version was sent."""
    text = f"Pulse note: version {version} was sent to reviewers."
    if trigger == "reviewer" and requested_by:
        text += f" {requested_by} asked for this build."
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _source_rows(snapshot: list[Message], outcomes: dict[str, Outcome]) -> dict[str, SourceRow]:
    """Each snapshot message with its outcome, in received order.

    A rejected message keeps only its subject.
    """
    rows = {}
    for message in snapshot:
        status = outcomes[message.id].status
        if status == "rejected":
            rows[message.id] = SourceRow(message.id, "rejected", message.subject)
        else:
            rows[message.id] = SourceRow(
                message_id=message.id,
                outcome=status,
                subject=message.subject,
                sender_name=message.sender_name,
                sender_address=message.sender_address,
                received_at=message.received_at,
                body=message.body,
                has_attachments=message.has_attachments,
            )
    return rows


def _item_rows(items: list[ItemWithSenders], excluded: list[Exclusion]) -> list[ItemRow]:
    rows = [
        ItemRow(
            item_id=item.item_id,
            kind="item",
            record=item.model_dump(mode="json"),
            source_message_ids=item.source_message_ids,
        )
        for item in items
    ]
    rows += [
        ItemRow(
            item_id=f"excluded-{n}",
            kind="excluded",
            record=exclusion.record.model_dump(mode="json") | {"reason": exclusion.reason},
            source_message_ids=[exclusion.record.message_id],
        )
        for n, exclusion in enumerate(excluded, start=1)
    ]
    return rows


async def draft_check_loop(
    agents: AgentRunner,
    draft_once: DraftAttempt,
    sources: Mapping[str, SourceRow],
    config: Config,
    feedback: Sequence[str] = (),
    on_attempt: Callable[[int, Draft, list[str]], None] = lambda *_: None,
) -> tuple[Draft, str, list[str]]:
    """Run the draft, judge and check loop (steps 6 and 7), regenerating once on failure.

    `draft_once` receives the previous attempt's failures (empty on the first attempt) and
    returns the next draft, headline and the items it was written from, so a build's fixed
    items and a revision's items, which can change with each attempt, work the same way.
    Returns the final draft, headline and failures (empty if the draft passed).
    """
    failures: list[str] = []
    draft: Draft
    headline: str
    for attempt in (1, 2):
        draft, headline, items = await draft_once(failures)
        verdict = await agents.run(
            Judge(), JudgeInput(draft=draft, items=list(items), feedback=list(feedback))
        )
        failures = check_draft(draft, headline, items, sources, config, feedback) + judge_failures(
            verdict
        )
        on_attempt(attempt, draft, failures)
        if not failures:
            break
    return draft, headline, failures


async def run_build(
    config: Config,
    submissions: Mailbox,
    conversation: Mailbox,
    store: EditionStore,
    agents: AgentRunner,
    clock: Clock,
    trigger: Trigger,
    requested_by: str | None = None,
    dry_run: bool = False,
    should_stop: Callable[[], bool] = lambda: False,
) -> BuildResult:
    """Run the build workflow once, as the design's process flow describes.

    Every trigger makes the same request: if an edition is open, it is returned unchanged;
    otherwise a new one is built. If a build is already running, the request returns
    "build in progress".

    Raises:
        StopRequested: If ``should_stop`` returns True at a step boundary.
        AgentResponseError, GatewayError: If a model step fails; nothing is sent or moved.
        EditionStoreError: If the edition cannot be saved after version 1 is sent.
    """
    root = config.run_artefacts_dir
    try:
        with run_lock(root / "build.lock"):
            now = clock()
            # Step 1: lock and check for an open edition.
            await _resume(root, submissions, config, store)
            open_edition = await store.open_edition()
            if open_edition is not None:
                return BuildResult("open", open_edition, None, None)
            delete_old_runs(root, now, config.retention_days)
            await store.delete_closed_before(now - timedelta(days=config.retention_days))

            artefacts = RunArtefacts(root, now)
            manifest = Manifest(run_id=artefacts.dir.name, started_at=now, dry_run=dry_run)

            def save() -> None:
                artefacts.save(manifest)

            def boundary(step: str) -> None:
                manifest.step = step
                save()
                if should_stop():
                    raise StopRequested(f"stopped after {step}")

            boundary("lock and resume")

            # Step 2: snapshot.
            snapshot = sorted(await submissions.list_inbox(), key=lambda m: m.received_at)
            messages = {m.id: m for m in snapshot}
            manifest.message_ids = list(messages)
            artefacts.write_text(
                "messages.json", TypeAdapter(list[Message]).dump_json(snapshot, indent=2).decode()
            )
            log.info("snapshot", extra={"messages": len(snapshot)})
            boundary("snapshot")

            # Step 3: pre-filter.
            passed, rejected = prefilter(snapshot, config)
            for rejection in rejected:
                manifest.outcomes[rejection.message.id] = Outcome(
                    status="rejected", reason=rejection.reason
                )
            log.info("pre-filter", extra={"passed": len(passed), "rejected": len(rejected)})
            boundary("pre-filter")

            # Step 4: extract.
            extractor = Extractor(config.sections)
            records = await agents.run_many(
                extractor, [CleanedEmail.from_message(m) for m in passed]
            )
            artefacts.write_text(
                "extract.json",
                TypeAdapter(list[ExtractRecord]).dump_json(records, indent=2).decode(),
            )
            included, excluded = exclude(records)
            for record in included:
                manifest.outcomes[record.message_id] = Outcome(status="included")
            for exclusion in excluded:
                manifest.outcomes[exclusion.record.message_id] = Outcome(
                    status="excluded", reason=exclusion.reason
                )
            log.info("extract", extra={"included": len(included), "excluded": len(excluded)})
            boundary("extract")
            sources = _source_rows(snapshot, manifest.outcomes)

            if not included:
                # Empty build: no newsletter; rejected messages still move, everything else stays.
                if not dry_run:
                    await _move(
                        submissions, manifest, [r.message.id for r in rejected], config, save
                    )
                manifest.complete = True
                save()
                log.info("no items; no newsletter sent")
                return BuildResult("empty", None, manifest, artefacts.dir)

            # Step 5: consolidate.
            consolidation: Consolidation = await agents.run(Consolidator(config.sections), included)
            artefacts.write_json("consolidate.json", consolidation)
            items = [
                ItemWithSenders(
                    **item.model_dump(),
                    sender_names=list(
                        dict.fromkeys(messages[i].sender_name for i in item.source_message_ids)
                    ),
                    received_dates=[messages[i].received_at for i in item.source_message_ids],
                )
                for item in consolidation.items
            ]
            headline = consolidation.headline
            boundary("consolidate")

            # Steps 6 and 7: draft and check, regenerating once on failure.
            async def drafter_attempt(
                failures: list[str],
            ) -> tuple[Draft, str, Sequence[ItemWithSenders]]:
                draft = await agents.run(
                    Drafter(),
                    DraftInput(items=items, max_words=config.limits.max_words, failures=failures),
                )
                return draft, headline, items

            def on_attempt(attempt: int, draft: Draft, failures: list[str]) -> None:
                artefacts.write_json(f"draft-{attempt}.json", draft)
                artefacts.write_text(
                    f"checks-{attempt}.json",
                    TypeAdapter(list[str]).dump_json(failures, indent=2).decode(),
                )
                log.info("draft checked", extra={"attempt": attempt, "failures": len(failures)})

            draft, headline, failures = await draft_check_loop(
                agents, drafter_attempt, sources, config, on_attempt=on_attempt
            )
            boundary("check")

            # Step 8: send and save version 1.
            version = Version(
                number=1,
                draft=draft,
                headline=headline,
                item_ids=[item.item_id for item in items],
                check_results=failures,
                creator=BUILDER,
                created_at=now,
            )
            item_rows = _item_rows(items, excluded)
            email = version_email(config, now, version, item_rows, sources)
            artefacts.write_text("reviewer-email.html", email.html)
            if not dry_run:
                await conversation.send(email)
                log.info("draft sent", extra={"reviewers": len(config.reviewers)})
            boundary("send")

            if dry_run:
                manifest.complete = True
                save()
                return BuildResult("dry_run", None, manifest, artefacts.dir)

            edition = await store.create_edition(
                build_id=manifest.run_id,
                trigger=trigger,
                items=item_rows,
                sources=list(sources.values()),
                version=version,
                history=[_history_note(1, trigger, requested_by)],
            )

            # Step 9: move messages, only after the edition is saved.
            await _move(submissions, manifest, manifest.pending_moves(), config, save)
            manifest.complete = True
            save()
            return BuildResult("created", edition, manifest, artefacts.dir)
    except LockHeldError:
        return BuildResult("in_progress", None, None, None)


# The revision workflow, run by the chat agent's revise_draft tool.


def _category_for(item_id: str, draft: Draft) -> str:
    return next((s.category for s in draft.sections for e in s.entries if e.item_id == item_id), "")


def _sender_names(message_id: str, sources: Mapping[str, SourceRow]) -> list[str]:
    name = sources[message_id].sender_name
    return [name] if name else []


def _excluded_for_reviser(row: ItemRow, sources: Mapping[str, SourceRow]) -> ExcludedForReviser:
    record, reason = row.excluded()
    return ExcludedForReviser(
        item_id=row.item_id,
        record=record,
        reason=reason,
        sender_names=_sender_names(record.message_id, sources),
    )


def _item(row: ItemRow, sources: Mapping[str, SourceRow], draft: Draft) -> ItemWithSenders:
    """The item a draft was written from.

    A restored excluded record takes the category of the section the draft placed it in.
    """
    if row.kind == "item":
        return ItemWithSenders.model_validate(row.record)
    record, _ = row.excluded()
    received_at = sources[record.message_id].received_at
    return ItemWithSenders(
        item_id=row.item_id,
        category=_category_for(row.item_id, draft),
        facts=record.facts,
        people=record.people,
        source_message_ids=[record.message_id],
        sender_names=_sender_names(record.message_id, sources),
        received_dates=[received_at] if received_at else [],
    )


def _items(
    item_ids: Sequence[str],
    rows: Mapping[str, ItemRow],
    sources: Mapping[str, SourceRow],
    draft: Draft,
) -> list[ItemWithSenders]:
    return [_item(rows[item_id], sources, draft) for item_id in item_ids]


async def run_revision(
    config: Config,
    store: EditionStore,
    agents: AgentRunner,
    edition_id: str,
    new_version_number: int,
    instruction: str,
    reviewer_message: str,
    creator: str,
    now: datetime,
) -> tuple[Version, list[str]]:
    """Revise an edition's current draft from reviewer feedback.

    Runs the reviser, then the same judge and check loop as a build, with reviewer feedback
    counting as a source, regenerating once on failure. A second failing revision is still
    returned, with its failures listed first in the review section.

    Raises:
        AgentResponseError: If the reviser returns an invalid response twice.
        GatewayError: If the gateway fails.
    """
    current = await store.current_version(edition_id)
    rows = {row.item_id: row for row in await store.items(edition_id)}
    sources = await store.sources(edition_id)
    feedback = [f.text for f in await store.feedback(edition_id)]

    included = _items(current.item_ids, rows, sources, current.draft)
    revise_input = ReviseInput(
        draft=current.draft,
        headline=current.headline,
        items=included,
        excluded=[
            _excluded_for_reviser(row, sources)
            for row in rows.values()
            if row.kind == "excluded" and row.item_id not in current.item_ids
        ],
        feedback=feedback,
        instruction=instruction,
        reviewer_message=reviewer_message,
        failures=[],
    )
    reviser = Reviser(config.sections)
    latest: list[Revision] = []

    async def draft_once(failures: list[str]) -> tuple[Draft, str, Sequence[ItemWithSenders]]:
        result = await agents.run(reviser, revise_input.model_copy(update={"failures": failures}))
        latest.append(result)
        return result.draft, result.headline, _items(result.item_ids, rows, sources, result.draft)

    draft, headline, failures = await draft_check_loop(
        agents, draft_once, sources, config, feedback=feedback
    )
    revision = latest[-1]
    version = Version(
        number=new_version_number,
        draft=draft,
        headline=headline,
        item_ids=revision.item_ids,
        check_results=failures,
        changes=revision.changes,
        not_applied=revision.not_applied,
        creator=creator,
        created_at=now,
    )
    return version, failures
