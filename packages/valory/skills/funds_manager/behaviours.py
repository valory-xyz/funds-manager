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
from typing import Dict, List, Tuple, cast

from aea.skills.behaviours import SimpleBehaviour
from w3multicall.multicall import W3Multicall  # type: ignore[import]
from web3 import Web3

from packages.valory.skills.funds_manager.models import (
    AGENT_ACCOUNT_NAME,
    AccountRequirements,
    FundRequirements,
    Params,
    TokenRequirement,
)


ERC20_DECIMALS_ABI = "decimals()(uint8)"
NATIVE_BALANCE_ABI = "getEthBalance(address)(uint256)"
ERC20_BALANCE_ABI = "balanceOf(address)(uint256)"
NATIVE_DECIMALS = 18

MULTICALL_ADDR = "0xcA11bde05977b3631167028862bE2a173976CA11"

GET_FUNDS_STATUS_METHOD_NAME = "get_funds_status"


class FundsManagerBehaviour(SimpleBehaviour):
    """FundsManagerBehaviour"""

    def act(self) -> None:
        """Do the action."""

    def setup(self) -> None:
        """Set up the behaviour."""
        super().setup()
        self.context.shared_state[GET_FUNDS_STATUS_METHOD_NAME] = self.get_funds_status

    def _perform_w3_multicall(self, rpc_url: str, calls: List) -> List:
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

    def _switch_out_account_names_for_addresses(
        self, funds: FundRequirements
    ) -> FundRequirements:
        """Switch out account names for addresses in the given FundRequirements object."""
        funds_with_addresses = copy.deepcopy(funds)

        for _, chain_requirements in funds_with_addresses.items():
            for account_name in list(chain_requirements.accounts.keys()):
                account_address = self._get_account_address(account_name)
                chain_requirements.accounts[account_address] = (
                    chain_requirements.accounts.pop(account_name)
                )

        return funds_with_addresses

    def _get_native_balance_call_tuple(
        self, account_address: str, token_address: str
    ) -> tuple:
        """Get the balance call tuple for the given account and token."""
        return (
            account_address,
            token_address,
            W3Multicall.Call(
                MULTICALL_ADDR,
                NATIVE_BALANCE_ABI,
                [account_address],
            ),
        )

    def _get_erc20_balance_call_tuple(
        self, account_address: str, token_address: str
    ) -> tuple:
        """Get the balance call tuple for the given account and token."""
        return (
            account_address,
            token_address,
            W3Multicall.Call(token_address, ERC20_BALANCE_ABI, [account_address]),
        )

    def _construct_calls(
        self,
        account_address: str,
        account_requirements: AccountRequirements,
    ) -> Tuple:
        """Construct balance calls, decimals calls, and token mapping for the given account."""
        balance_calls = []
        decimals_calls = {}
        token_mapping = {}  # (account_address, token_address) -> TokenRequirement
        for (
            token_address,
            token_requirements,
        ) in account_requirements.tokens.items():
            token_mapping[(account_address, token_address)] = token_requirements

            if token_requirements.is_native:
                # Native: balance call
                balance_calls.append(
                    self._get_native_balance_call_tuple(account_address, token_address)
                )
            else:
                # ERC20: balance call
                balance_calls.append(
                    self._get_erc20_balance_call_tuple(account_address, token_address)
                )
                # ERC20: decimals call
                decimals_calls[token_address] = W3Multicall.Call(
                    token_address, ERC20_DECIMALS_ABI
                )

        return balance_calls, decimals_calls, token_mapping

    def get_funds_status(self) -> FundRequirements:
        """Get the current funds status, using chain-level multicalls."""

        funds = self._switch_out_account_names_for_addresses(self.fund_requirements)

        for chain_name, chain_requirements in funds.items():
            # Collect calls per chain
            balance_calls = []
            decimals_calls = {}
            token_mapping: Dict[Tuple, TokenRequirement] = {}
            for (
                account_address,
                account_requirements,
            ) in chain_requirements.accounts.items():
                (
                    balance_calls_account,
                    decimals_calls_account,
                    token_mapping_account,
                ) = self._construct_calls(account_address, account_requirements)
                balance_calls.extend(balance_calls_account)
                decimals_calls.update(decimals_calls_account)
                token_mapping.update(token_mapping_account)

            # Perform ONE multicall per chain
            all_calls = [call for _, _, call in balance_calls] + list(
                decimals_calls.values()
            )
            results = self._perform_w3_multicall(
                self.params.rpc_urls[chain_name], all_calls
            )

            # Split results
            balance_calls_end_index = len(balance_calls)
            balance_results = results[:balance_calls_end_index]
            decimal_results = results[balance_calls_end_index:]

            decimals_map = {
                token_addr: res
                for token_addr, res in zip(decimals_calls.keys(), decimal_results)
            }

            # Fill in balances, deficits, and decimals
            for (account_address, token_address, _), balance in zip(
                balance_calls, balance_results
            ):
                balance = int(balance or 0)
                token_requirement = token_mapping[(account_address, token_address)]
                deficit = (
                    max(token_requirement.topup - balance, 0)
                    if balance < token_requirement.threshold
                    else 0
                )

                token_requirement.balance = balance
                token_requirement.deficit = deficit
                token_requirement.decimals = (
                    NATIVE_DECIMALS
                    if token_requirement.is_native
                    else decimals_map[token_address]
                )

        return funds
