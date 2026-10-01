"""Operations and the renderer, configured as main configures them, for tests."""

from zoneinfo import ZoneInfo

from pulse.adapters.render import Renderer
from pulse.entities.clock import Clock
from pulse.entities.store import Store
from pulse.services.operations import Operations
from tests.emails import screen_email
from tests.fakes.clock import ControlledClock
from tests.fakes.mailbox import FakeMailbox
from tests.messages import OPENED

REVIEWERS = "pulse-reviewers@example.org"
SUBJECT_TEMPLATE = "Pulse: {date}"
TIMEZONE = ZoneInfo("Europe/London")


def make_renderer() -> Renderer:
    return Renderer(TIMEZONE)


def make_operations(
    store: Store,
    submissions: FakeMailbox | None = None,
    conversation: FakeMailbox | None = None,
    clock: Clock | None = None,
) -> Operations:
    return Operations(
        store,
        submissions or FakeMailbox(),
        conversation or FakeMailbox(),
        screen_email,
        make_renderer().reviewer_email,
        clock or ControlledClock(OPENED),
        reviewers=REVIEWERS,
        subject_template=SUBJECT_TEMPLATE,
        timezone=TIMEZONE,
    )
