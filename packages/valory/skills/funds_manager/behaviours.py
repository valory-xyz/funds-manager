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
"""This module contains the behaviours for the 'funds_manager' skill."""
import copy
from typing import List, cast

from aea.skills.behaviours import SimpleBehaviour
from w3multicall.multicall import W3Multicall  # type: ignore[import]
from web3 import Web3

from packages.valory.skills.funds_manager.models import (
    AGENT_ACCOUNT_NAME,
    FundRequirements,
    Params,
    TokenRequirement,
)


CHAIN_NAME_TO_ID = {
    "ethereum": 1,
    "gnosis": 100,
}


ERC20_DECIMALS_ABI = "decimals()(uint8)"
NATIVE_BALANCE_ABI = "getEthBalance(address)(uint256)"
ERC20_BALANCE_ABI = "balanceOf(address)(uint256)"
NATIVE_DECIMALS = 18

MULTICALL_ADDR = "0xcA11bde05977b3631167028862bE2a173976CA11"

FIVE_MINUTES_IN_SECONDS = 300


GET_FUNDS_STATUS_METHOD_NAME = "get_funds_status"


class FundsManagerBehaviour(SimpleBehaviour):
    """FundsManagerBehaviour"""

    def act(self) -> None:
        """Do the action."""

    def setup(self) -> None:
        """Set up the behaviour."""
        super().setup()
        self.context.shared_state[GET_FUNDS_STATUS_METHOD_NAME] = self.get_funds_status

    def perform_w3_multicall(self, rpc_url: str, calls: List) -> List:
        """Do a multicall using w3_multicall."""
        w3 = Web3(Web3.HTTPProvider(rpc_url))

        w3_multicall = W3Multicall(w3)
        for call in calls:
            w3_multicall.add(call)

        return w3_multicall.call()

    @property
    def params(self) -> Params:
        """Return the params."""
        return cast(Params, self.context.params)

    @property
    def fund_requirements(self) -> FundRequirements:
        """Return the fund requirements."""
        return cast(FundRequirements, self.params.fund_requirements)

    @property
    def safe_address(self) -> str:
        """Return the safe address."""
        return self.params.safe_address

    def _get_account_address(self, account_name: str) -> str:
        """Get the address for the given account name."""
        if account_name == AGENT_ACCOUNT_NAME:
            return self.context.agent_address
        return self.safe_address

    def get_funds_status(self) -> FundRequirements:
        """Get the current funds status."""

        funds = copy.deepcopy(self.fund_requirements)

        for (
            chain_name,
            chain_requirements,
        ) in self.fund_requirements.items():

            for (
                account_name,
                account_requirements,
            ) in chain_requirements.accounts.items():

                account_address = self._get_account_address(account_name)

                calls = []
                decimals_calls = {}
                decimals_map = {}

                for (
                    token_address,
                    token_requirements,
                ) in account_requirements.tokens.items():
                    if token_requirements.is_native:
                        # Native tokens: prepare multicall for balance
                        balance_call = W3Multicall.Call(
                            MULTICALL_ADDR,
                            NATIVE_BALANCE_ABI,
                            [account_address],
                        )

                        decimals_map[token_address] = NATIVE_DECIMALS

                    else:
                        # ERC20: prepare multicall for balance
                        balance_call = W3Multicall.Call(
                            token_address,
                            ERC20_BALANCE_ABI,
                            [account_address],
                        )
                        # ERC20: prepare multicall for decimals
                        decimals_call = W3Multicall.Call(
                            token_address, ERC20_DECIMALS_ABI
                        )

                        decimals_calls[token_address] = decimals_call
                    calls.append((token_address, token_requirements, balance_call))

                # Execute multicall for balances
                balances = self.perform_w3_multicall(
                    self.params.rpc_urls[chain_name], [call for _, _, call in calls]
                )

                # Execute multicall for decimals (only ERC20)
                decimals = self.perform_w3_multicall(
                    self.params.rpc_urls[chain_name], list(decimals_calls.values())
                )

                # Map decimals back to token addresses
                decimals_map.update(
                    {
                        token_address: value
                        for token_address, value in zip(decimals_calls.keys(), decimals)
                    }
                )

                funds[chain_name].accounts[account_address] = funds[
                    chain_name
                ].accounts.pop(account_name)

                for (
                    token_address,
                    token_requirements,
                    _,
                ), balance in zip(calls, balances):
                    balance = int(balance or 0)
                    deficit = (
                        max(token_requirements.topup - balance, 0)
                        if balance < token_requirements.threshold
                        else 0
                    )
                    token_requirement: TokenRequirement = (
                        funds[chain_name]
                        .accounts[account_address]
                        .tokens[token_address]
                    )
                    token_requirement.balance = balance
                    token_requirement.deficit = deficit
                    token_requirement.decimals = decimals_map[token_address]

        return funds
