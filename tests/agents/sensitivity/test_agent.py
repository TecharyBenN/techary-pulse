import pytest

from pulse.agents.sensitivity.agent import SensitivityAgent
from pulse.entities.extracts import SensitivityOutput
from tests.emails import make_output, make_screened_email, share
from tests.fakes.models import specialist_request

pytestmark = pytest.mark.anyio

BODY = "Northwind Retail signed on 22 September."
EMAIL = make_screened_email("m01", body=BODY)


async def test_sensitivity_agent_has_no_tools_and_keeps_the_email_out_of_its_instructions() -> None:
    output = share(make_output(), SensitivityOutput)

    request = await specialist_request(SensitivityAgent, EMAIL, output)

    assert request.instructions is not None
    assert BODY not in request.instructions
