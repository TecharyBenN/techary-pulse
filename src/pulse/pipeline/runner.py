"""Runs the build workflow's nine steps in order."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelRequest, UserPromptPart

from pulse.agents import Consolidator, Drafter, Extractor, Judge
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
    ExtractRecord,
    ItemWithSenders,
    JudgeInput,
    Message,
    OutgoingEmail,
    Trigger,
)
from pulse.pipeline.check import check_draft, judge_failures
from pulse.pipeline.render import Review, Source, render_email, reviewer_subject
from pulse.pipeline.rules import Exclusion, Rejection, exclude, prefilter
from pulse.state import (
    Manifest,
    Outcome,
    RunArtefacts,
    delete_old_runs,
    latest_manifest,
    run_lock,
    save_manifest,
)
from pulse.store import EditionStore, ItemRow

log = logging.getLogger("pulse.pipeline")

Clock = Callable[[], datetime]

# The build's creator of record for version 1; later versions are created by a reviewer.
BUILDER = "pulse"


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


def _source(message: Message) -> Source:
    return Source(
        sender=message.sender_name, subject=message.subject, received_at=message.received_at
    )


def _review(
    failures: list[str],
    rejected: list[Rejection],
    excluded: list[Exclusion],
    passed: list[Message],
    items: list[ItemWithSenders],
    draft: Draft,
    messages: dict[str, Message],
) -> Review:
    entry_text = {e.item_id: e.text for s in draft.sections for e in s.entries}
    return Review(
        check_failures=failures,
        sensitivity_exclusions=[
            (_source(messages[e.record.message_id]), s.type, s.evidence)
            for e in excluded
            for s in e.record.sensitivity
        ],
        other_exclusions=[
            (_source(messages[e.record.message_id]), e.reason)
            for e in excluded
            if e.reason != "sensitivity"
        ],
        rejected_subjects=[r.message.subject for r in rejected],
        with_attachments=[_source(m) for m in passed if m.has_attachments],
        source_map=[
            (
                entry_text.get(item.item_id, item.item_id),
                [_source(messages[i]) for i in item.source_message_ids],
            )
            for item in items
        ],
    )


def _history_note(version: int, trigger: Trigger, requested_by: str | None) -> ModelRequest:
    """The conversation history entry recording that a version was sent."""
    text = f"Pulse note: version {version} was sent to reviewers."
    if trigger == "reviewer" and requested_by:
        text += f" {requested_by} asked for this build."
    return ModelRequest(parts=[UserPromptPart(content=text)])


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
            failures: list[str] = []
            for attempt in (1, 2):
                draft = await agents.run(
                    Drafter(),
                    DraftInput(items=items, max_words=config.limits.max_words, failures=failures),
                )
                verdict = await agents.run(Judge(), JudgeInput(draft=draft, items=items))
                failures = check_draft(draft, headline, items, messages, config) + judge_failures(
                    verdict
                )
                artefacts.write_json(f"draft-{attempt}.json", draft)
                artefacts.write_text(
                    f"checks-{attempt}.json",
                    TypeAdapter(list[str]).dump_json(failures, indent=2).decode(),
                )
                log.info("draft checked", extra={"attempt": attempt, "failures": len(failures)})
                if not failures:
                    break
            boundary("check")

            # Step 8: send and save version 1.
            review = _review(failures, rejected, excluded, passed, items, draft, messages)
            html = render_email(config, headline, draft, review)
            artefacts.write_text("reviewer-email.html", html)
            run_date = now.astimezone(ZoneInfo(config.timezone)).date()
            email = OutgoingEmail(
                to=config.reviewers, subject=reviewer_subject(config, 1, run_date), html=html
            )
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
                created_at=now,
                items=_item_rows(items, excluded),
                draft=draft.model_dump(mode="json"),
                headline=headline,
                included_item_ids=[item.item_id for item in items],
                check_results=failures,
                creator=BUILDER,
                history=[_history_note(1, trigger, requested_by)],
            )

            # Step 9: move messages, only after the edition is saved.
            await _move(submissions, manifest, manifest.pending_moves(), config, save)
            manifest.complete = True
            save()
            return BuildResult("created", edition, manifest, artefacts.dir)
    except LockHeldError:
        return BuildResult("in_progress", None, None, None)
