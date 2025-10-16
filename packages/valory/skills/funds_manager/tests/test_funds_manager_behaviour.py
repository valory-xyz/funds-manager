# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2025 Valory AG
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

    def setup(self, **kwargs: Any) -> None:
        """Setup."""
        self.behaviour.setup()
        super().setup(**kwargs)

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
        behaviour._perform_w3_multicall = mock.Mock(side_effect=mock_multicall_response)  # type: ignore

        funds = cast(FundRequirements, behaviour.get_funds_status())

        assert behaviour._perform_w3_multicall.call_count == len(
            mock_multicall_response
        )

        assert funds.get_response_body() == funds_response
