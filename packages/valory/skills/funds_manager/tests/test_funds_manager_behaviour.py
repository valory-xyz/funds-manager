# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2025-2026 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This module contains the tests for valory/fund_managers' behaviour."""

from pathlib import Path
from typing import Any, Dict, cast
from unittest import mock
from unittest.mock import MagicMock

import pytest
from aea.configurations.base import PackageConfiguration
from aea.test_tools.test_skill import BaseSkillTestCase

from packages.valory.skills.funds_manager.behaviours import (
    FundsManagerBehaviour,
    GET_FUNDS_STATUS_METHOD_NAME,
)
from packages.valory.skills.funds_manager.models import FundRequirements
from packages.valory.skills.funds_manager.tests import data_for_tests

CURRENT_FILE_PATH = Path(__file__).resolve()
PACKAGE_DIR = CURRENT_FILE_PATH.parents[1]


class TestFundsManagerBehaviour(BaseSkillTestCase):
    """Test `TestFundsManagerBehaviour`."""

    # behaviour: FundsManager  # type: ignore
    path_to_skill = PACKAGE_DIR
    _skill = MagicMock()

    @property
    def behaviour(self) -> FundsManagerBehaviour:
        """Get the behaviour."""
        return cast(FundsManagerBehaviour, self.skill.behaviours["funds_manager"])

    @classmethod
    def setup_class(cls, **kwargs: Any) -> None:
        """Set up the class."""
        kwargs["config_overrides"] = {
            "models": {
                "params": {
                    "args": {
                        "fund_requirements": data_for_tests.TRADER_INITIAL_FUND_REQUIREMENTS,
                        "safe_contract_addresses": {
                            "gnosis": data_for_tests.MOCK_SAFE_ADDRESS,
                            "base": data_for_tests.MOCK_SAFE_ADDRESS,
                            "optimism": data_for_tests.MOCK_SAFE_ADDRESS,
                            "mode": data_for_tests.MOCK_SAFE_ADDRESS,
                            "celo": data_for_tests.MOCK_SAFE_ADDRESS_CELO,
                        },
                        "rpc_urls": data_for_tests.MOCK_RPC_URLS,
                    },
                }
            }
        }

        with mock.patch.object(PackageConfiguration, "check_overrides_valid"):
            super().setup_class(**kwargs)

    def setup_method(self, **kwargs: Any) -> None:
        """Setup."""
        super().setup_method(**kwargs)
        self.behaviour.setup()

    @pytest.mark.parametrize(
        "account_name, chain_name, expected_address",
        [
            ("agent", "gnosis", data_for_tests.MOCK_AGENT_ADDRESS),
            ("safe", "gnosis", data_for_tests.MOCK_SAFE_ADDRESS),
            ("safe", "celo", data_for_tests.MOCK_SAFE_ADDRESS_CELO),
        ],
    )
    def test_account_name_to_actual_address(
        self, account_name: str, chain_name: str, expected_address: str
    ) -> None:
        """Test the `_account_name_to_actual_address` method."""
        behaviour = self.behaviour
        assert (
            behaviour._account_name_to_actual_address(account_name, chain_name)
            == expected_address
        )

    def test_get_funds_status_exists_in_shared_state(self) -> None:
        """Test the `get_funds_status` method is correctly set in the shared state."""

        assert GET_FUNDS_STATUS_METHOD_NAME in self.behaviour.context.shared_state
        assert callable(
            self.behaviour.context.shared_state[GET_FUNDS_STATUS_METHOD_NAME]
        )

    def test_shared_state_callable_returns_funds_status(
        self, funds_dataset: Dict
    ) -> None:
        """The callable registered in shared_state must reach the same code path."""
        behaviour = self.behaviour
        fund_requirements = funds_dataset["fund_requirements"]
        funds_response = funds_dataset["funds_response"]
        mock_multicall_response = funds_dataset["multicall"]
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            fund_requirements
        )
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=mock_multicall_response
        )

        shared_callable = behaviour.context.shared_state[GET_FUNDS_STATUS_METHOD_NAME]
        funds = shared_callable()

        assert funds.get_response_body() == funds_response

    def test_get_funds_status(self, funds_dataset: Dict) -> None:
        """Test the `get_funds_status` method."""
        behaviour = self.behaviour
        fund_requirements = funds_dataset["fund_requirements"]
        funds_response = funds_dataset["funds_response"]
        mock_multicall_response = funds_dataset["multicall"]
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            fund_requirements
        )

        # patch the instance method
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=mock_multicall_response
        )

        funds = behaviour.get_funds_status()

        assert behaviour._perform_try_aggregate.call_count == len(
            mock_multicall_response
        )

        assert funds.get_response_body() == funds_response

    def test_chain_failure_leaves_other_chains_populated(self) -> None:
        """One chain raising must not abort the loop or wipe other chains' data."""
        behaviour = self.behaviour
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            data_for_tests.OPTIMUS_INITIAL_FUND_REQUIREMENTS
        )
        # 3 chains in OPTIMUS_INITIAL_FUND_REQUIREMENTS; raise on the middle one
        first_chain_results = data_for_tests.OPTIMUS_MULTICALL_RETURN_VALUES[0]
        third_chain_results = data_for_tests.OPTIMUS_MULTICALL_RETURN_VALUES[2]
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=[
                first_chain_results,
                RuntimeError("simulated RPC outage on chain 2"),
                third_chain_results,
            ]
        )

        funds = behaviour.get_funds_status()
        response = funds.get_response_body()

        # First chain: populated as normal.
        first_chain = list(response.values())[0]
        first_token = list(list(first_chain.values())[0].values())[0]
        assert first_token["balance"] is not None
        assert first_token["deficit"] is not None
        assert first_token["decimals"] is not None

        # Second chain: every token has balance=None / deficit=None.
        second_chain = list(response.values())[1]
        for account in second_chain.values():
            for token in account.values():
                assert token["balance"] is None
                assert token["deficit"] is None

        # Third chain: populated, untouched by the second chain's failure.
        third_chain = list(response.values())[2]
        third_token = list(list(third_chain.values())[0].values())[0]
        assert third_token["balance"] is not None
        assert third_token["deficit"] is not None

    def test_sub_call_failure_does_not_become_zero_balance(self) -> None:
        """A None sub-call result must not be treated as a legitimate zero balance."""
        behaviour = self.behaviour
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            data_for_tests.TRADER_INITIAL_FUND_REQUIREMENTS
        )
        # Trader chain has 2 native balance calls. Simulate the first reverting.
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=[[None, 2500000000000000000]]
        )

        funds = behaviour.get_funds_status()
        response = funds.get_response_body()
        gnosis = response["gnosis"]

        agent_eth = gnosis[data_for_tests.MOCK_AGENT_ADDRESS][
            "0x0000000000000000000000000000000000000000"
        ]
        safe_eth = gnosis[data_for_tests.MOCK_SAFE_ADDRESS][
            "0x0000000000000000000000000000000000000000"
        ]

        # The reverted sub-call must NOT be reported as "zero balance,
        # deficit=topup"; otherwise the consumer would trigger an
        # unnecessary top-up for a token whose balance is unknown.
        assert agent_eth["balance"] is None
        assert agent_eth["deficit"] is None

        # The other sub-call in the same multicall succeeded and is reported.
        assert safe_eth["balance"] == "2500000000000000000"
        assert safe_eth["deficit"] == "0"

    def test_decimals_failure_marks_token_unknown(self) -> None:
        """A failed ERC20 decimals call must not silently default to 18."""
        behaviour = self.behaviour
        # Two-account ERC20-only-token-on-optimism setup gives us 4 balance
        # calls + 1 decimals call. Make the decimals call fail.
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            {"optimism": data_for_tests.OPTIMUS_INITIAL_FUND_REQUIREMENTS["optimism"]}
        )
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=[[0, 2500000000000000, 0, 0, None]]
        )

        funds = behaviour.get_funds_status()
        response = funds.get_response_body()

        agent_usdc = response["optimism"][data_for_tests.MOCK_AGENT_ADDRESS][
            "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"
        ]
        # ERC20 decimals failed: token marked unknown, no balance/deficit asserted.
        assert agent_usdc["balance"] is None
        assert agent_usdc["deficit"] is None
        assert agent_usdc["decimals"] is None

        # Native ETH on the same chain still resolves: it does not need decimals.
        agent_eth = response["optimism"][data_for_tests.MOCK_AGENT_ADDRESS][
            "0x0000000000000000000000000000000000000000"
        ]
        assert agent_eth["balance"] == "0"
        assert agent_eth["decimals"] == 18
