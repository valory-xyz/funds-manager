from unittest import mock

import pytest

from packages.valory.skills.funds_manager.tests import data_for_tests


@pytest.fixture(params=["trader", "optimus"])
def funds_dataset(request):
    """Provide parameterized datasets for funds tests."""
    if request.param == "trader":
        return {
            "fund_requirements": data_for_tests.TRADER_INITIAL_FUND_REQUIREMENTS,
            "funds_response": data_for_tests.TRADER_FUNDS_RESPONSE,
            "multicall": data_for_tests.TRADER_MULTICALL_RETURN_VALUES,
            "id": "trader",
        }
    elif request.param == "optimus":
        return {
            "fund_requirements": data_for_tests.OPTIMUS_INITIAL_FUND_REQUIREMENTS,
            "funds_response": data_for_tests.OPTIMUS_FUNDS_RESPONSE,
            "multicall": data_for_tests.OPTIMUS_MULTICALL_RETURN_VALUES,
            "id": "optimus",
        }


@pytest.fixture
def mock_safe_address() -> str:
    """Return the mock safe address for tests."""
    return data_for_tests.MOCK_SAFE_ADDRESS


@pytest.fixture(autouse=True)
def patch_agent_address(request):
    """Automatically patch `agent_address` for all tests in the class."""
    test_instance = getattr(request, "instance", None)
    if test_instance is None or not hasattr(test_instance, "behaviour"):
        # skip fixture for tests without behaviour
        yield
        return

    with mock.patch.object(
        type(test_instance.behaviour.context),
        "agent_address",
        new_callable=mock.PropertyMock,
        return_value=data_for_tests.MOCK_AGENT_ADDRESS,
    ):
        yield
