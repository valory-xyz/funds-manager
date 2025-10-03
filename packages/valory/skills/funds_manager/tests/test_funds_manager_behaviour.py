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

"""This module contains the tests for valory/decision_maker_abci's base behaviour."""

from pathlib import Path
from typing import Any, Dict, List, cast
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
from packages.valory.skills.funds_manager.tests.data_for_tests import (
    MOCK_AGENT_ADDRESS,
    MOCK_RPC_URLS,
    MOCK_SAFE_ADDRESS,
    OPTIMUS_FUNDS_RESPONSE,
    OPTIMUS_INITIAL_FUND_REQUIREMENTS,
    OPTIMUS_MULTICALL_RETURN_VALUES,
    TRADER_FUNDS_RESPONSE,
    TRADER_INITIAL_FUND_REQUIREMENTS,
    TRADER_MULTICALL_RETURN_VALUES,
)


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
                        "fund_requirements": TRADER_INITIAL_FUND_REQUIREMENTS,
                        "setup": {
                            "safe_contract_address": MOCK_SAFE_ADDRESS,
                        },
                        "rpc_urls": MOCK_RPC_URLS,
                    },
                }
            }
        }

        with mock.patch.object(PackageConfiguration, "check_overrides_valid"):
            super().setup_class(**kwargs)
        super().setup_class(**kwargs)

    def setup(self, **kwargs: Any) -> None:
        """Setup."""
        self.behaviour.setup()
        super().setup(**kwargs)

    def test_safe_address(self) -> None:
        """Test the `safe_address` property."""
        assert self.behaviour.safe_address == MOCK_SAFE_ADDRESS

    def test_get_funds_status_exists_in_shared_state(self) -> None:
        """Test the `get_funds_status` method is correctly set in the shared state."""

        assert GET_FUNDS_STATUS_METHOD_NAME in self.behaviour.context.shared_state
        assert callable(
            self.behaviour.context.shared_state[GET_FUNDS_STATUS_METHOD_NAME]
        )

    @pytest.mark.parametrize(
        "fund_requirements, funds_response, mock_multicall_response",
        [
            (
                TRADER_INITIAL_FUND_REQUIREMENTS,
                TRADER_FUNDS_RESPONSE,
                TRADER_MULTICALL_RETURN_VALUES,
            ),
            (
                OPTIMUS_INITIAL_FUND_REQUIREMENTS,
                OPTIMUS_FUNDS_RESPONSE,
                OPTIMUS_MULTICALL_RETURN_VALUES,
            ),
        ],
        ids=["trader", "optimus"],
    )
    def test_get_funds_status(
        self,
        fund_requirements: Dict[str, Any],
        funds_response: Dict[str, Any],
        mock_multicall_response: List[List[Any]],
    ) -> None:
        """Test the `get_funds_status` method."""
        behaviour = self.behaviour
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            fund_requirements
        )

        # patch the instance method
        behaviour._perform_w3_multicall = mock.Mock(side_effect=mock_multicall_response)  # type: ignore

        with mock.patch.object(
            type(behaviour.context),
            "agent_address",
            new_callable=mock.PropertyMock,
            return_value=MOCK_AGENT_ADDRESS,
        ):

            funds = cast(FundRequirements, behaviour.get_funds_status())

        assert behaviour._perform_w3_multicall.call_count == len(
            mock_multicall_response
        )

        assert funds.get_response_body() == funds_response
