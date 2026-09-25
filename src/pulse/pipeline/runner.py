"""Runs the nine pipeline steps in order."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from pulse.agents import Consolidator, Drafter, Extractor, Judge
from pulse.agents.runner import AgentRunner
from pulse.config import Config
from pulse.errors import StopRequested
from pulse.mail import Mailbox
from pulse.models import (
    CleanedEmail,
    Consolidation,
    Draft,
    DraftInput,
    ExtractRecord,
    ItemWithSenders,
    JudgeInput,
    Message,
    OutgoingEmail,
)
from pulse.pipeline.check import check_draft, judge_failures
from pulse.pipeline.render import Review, Source, render_email, subject
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

log = logging.getLogger("pulse.pipeline")

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class RunResult:
    status: Literal["sent", "dry_run", "empty"]
    manifest: Manifest
    artefacts_dir: Path


def _folder(outcome: Outcome, config: Config) -> str:
    if outcome.status == "rejected":
        return config.mailbox.rejected_folder
    return config.mailbox.processed_folder


def _move(
    mailbox: Mailbox, manifest: Manifest, ids: list[str], config: Config, save: Callable[[], None]
) -> None:
    for message_id in ids:
        mailbox.move(message_id, _folder(manifest.outcomes[message_id], config))
        manifest.moved.append(message_id)
        save()


def _resume(root: Path, mailbox: Mailbox, config: Config) -> None:
    """Complete the moves of an earlier run whose draft was sent."""
    found = latest_manifest(root)
    if found is None:
        return
    run_dir, manifest = found
    if not manifest.sent or manifest.complete:
        return
    log.info("completing moves", extra={"run_id": manifest.run_id})
    _move(
        mailbox,
        manifest,
        manifest.pending_moves(),
        config,
        lambda: save_manifest(run_dir, manifest),
    )
    manifest.complete = True
    save_manifest(run_dir, manifest)


def _source(message: Message) -> Source:
    return Source(sender=message.sender_name, subject=message.subject)


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


def run_pipeline(
    config: Config,
    mailbox: Mailbox,
    agents: AgentRunner,
    clock: Clock,
    dry_run: bool = False,
    should_stop: Callable[[], bool] = lambda: False,
) -> RunResult:
    """Run the pipeline once, as the design's process flow describes.

    Raises:
        LockHeldError: If another run is in progress.
        StopRequested: If ``should_stop`` returns True at a step boundary.
        AgentResponseError, GatewayError: If a model step fails; nothing is sent or moved.
    """
    root = config.run_artefacts_dir
    with run_lock(root / "pulse.lock"):
        now = clock()
        # Step 1: lock and resume.
        _resume(root, mailbox, config)
        delete_old_runs(root, now, config.retention_days)
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
        snapshot = sorted(mailbox.list_inbox(), key=lambda m: m.received_at)
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
        records = agents.run_many(extractor, [CleanedEmail.from_message(m) for m in passed])
        artefacts.write_text(
            "extract.json", TypeAdapter(list[ExtractRecord]).dump_json(records, indent=2).decode()
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
            # Empty week: no newsletter; rejected messages still move, everything else stays.
            if not dry_run:
                _move(mailbox, manifest, [r.message.id for r in rejected], config, save)
            manifest.complete = True
            save()
            log.info("no items; no newsletter sent")
            return RunResult("empty", manifest, artefacts.dir)

        # Step 5: consolidate.
        consolidation: Consolidation = agents.run(Consolidator(config.sections), included)
        artefacts.write_json("consolidate.json", consolidation)
        items = [
            ItemWithSenders(
                **item.model_dump(),
                sender_names=list(
                    dict.fromkeys(messages[i].sender_name for i in item.source_message_ids)
                ),
            )
            for item in consolidation.items
        ]
        headline = consolidation.headline
        boundary("consolidate")

        # Steps 6 and 7: draft and check, regenerating once on failure.
        failures: list[str] = []
        for attempt in (1, 2):
            draft = agents.run(
                Drafter(),
                DraftInput(items=items, max_words=config.limits.max_words, failures=failures),
            )
            verdict = agents.run(Judge(), JudgeInput(draft=draft, items=items))
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

        # Step 8: render and send.
        review = _review(failures, rejected, excluded, passed, items, draft, messages)
        html = render_email(config, headline, draft, review)
        artefacts.write_text("reviewer-email.html", html)
        run_date = now.astimezone(ZoneInfo(config.schedule.timezone)).date()
        email = OutgoingEmail(
            to=config.reviewers,
            reply_to=config.reviewers,
            subject=subject(config, run_date),
            html=html,
        )
        if not dry_run:
            mailbox.send(email)
            manifest.sent = True
            log.info("draft sent", extra={"reviewers": len(config.reviewers)})
        boundary("render and send")

        # Step 9: move messages, only after the send succeeded.
        if not dry_run:
            _move(mailbox, manifest, manifest.pending_moves(), config, save)
        manifest.complete = True
        save()
        return RunResult("dry_run" if dry_run else "sent", manifest, artefacts.dir)
