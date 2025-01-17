import web3
import itertools

from typing import List, Optional, Tuple, Union
from degenbot.exceptions import (
    LiquidityPoolError,
    EVMRevertError,
    ManagerError,
    TransactionError,
)
from degenbot.transaction.base import Transaction
from degenbot.uniswap.manager import (
    UniswapV2LiquidityPoolManager,
    UniswapV3LiquidityPoolManager,
)
from degenbot.uniswap.v2 import LiquidityPool
from degenbot.uniswap.v3.abi import (
    UNISWAP_V3_ROUTER_ABI,
    UNISWAP_V3_ROUTER2_ABI,
)
from degenbot.uniswap.v3 import V3LiquidityPool
from degenbot.manager import Erc20TokenHelperManager


# Internal dict of known router contracts, pre-populated with mainnet addresses
# Stored at the class level so routers can be added via class method `add_router`
_routers = {
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F": {
        "name": "Sushiswap: Router",
        "uniswap_version": 2,
        "factory_address": {2: "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac"},
    },
    "0xf164fC0Ec4E93095b804a4795bBe1e041497b92a": {
        "name": "UniswapV2: Router",
        "uniswap_version": 2,
        "factory_address": {2: "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"},
    },
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D": {
        "name": "UniswapV2: Router 2",
        "uniswap_version": 2,
        "factory_address": {2: "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"},
    },
    "0xE592427A0AEce92De3Edee1F18E0157C05861564": {
        "name": "UniswapV3: Router",
        "uniswap_version": 3,
        "factory_address": {3: "0x1F98431c8aD98523631AE4a59f267346ea31F984"},
    },
    "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45": {
        "name": "UniswapV3: Router 2",
        "uniswap_version": 3,
        "factory_address": {
            2: "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
            3: "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        },
    },
}


class UniswapTransaction(Transaction):
    def __init__(
        self,
        tx_hash: str,
        tx_nonce: int,
        tx_value: int,
        func_name: str,
        func_params: dict,
        router_address: str,
    ):

        self.routers = _routers

        if router_address not in self.routers.keys():
            raise ValueError(f"Router address {router_address} unknown!")

        try:
            self.v2_pool_manager = UniswapV2LiquidityPoolManager(
                factory_address=self.routers[router_address][
                    "factory_address"
                ][2]
            )
        except:
            pass

        try:
            self.v3_pool_manager = UniswapV3LiquidityPoolManager(
                factory_address=self.routers[router_address][
                    "factory_address"
                ][3]
            )
        except:
            pass

        self.hash = tx_hash
        self.nonce = tx_nonce
        self.value = tx_value
        self.func_name = func_name
        self.func_params = func_params
        self.func_deadline = func_params.get("deadline")
        self.func_previous_block_hash = (
            hash.hex()
            if (hash := self.func_params.get("previousBlockhash"))
            else None
        )

    @classmethod
    def add_router(cls, router_address: str, router_dict: dict):

        router_address = web3.Web3.toChecksumAddress(router_address)
        if router_address in _routers.keys():
            raise ValueError("Router address already known!")

        _routers[router_address] = router_dict

    def simulate(
        self,
        func_name: Optional[str] = None,
        func_params: Optional[dict] = None,
        silent: bool = False,
    ) -> List[Tuple[Union[LiquidityPool, V3LiquidityPool], dict]]:
        """
        Take a Uniswap V2 / V3 transaction (specified by name and a dictionary of arguments
        to that function) and return a list of pools and state dictionaries for all hops
        associated with the transaction
        """

        def v2_swap_exact_in(
            params: dict,
            unwrapped_input: Optional[bool] = False,
            silent: bool = False,
        ) -> List[Tuple[LiquidityPool, dict]]:

            v2_pool_objects = []
            for token_addresses in itertools.pairwise(params.get("path")):
                try:
                    pool_helper: LiquidityPool = self.v2_pool_manager.get_pool(
                        token_addresses=token_addresses,
                        silent=silent,
                    )
                except LiquidityPoolError:
                    raise TransactionError(
                        f"LiquidityPool could not be build for token pair {token_addresses[0]} - {token_addresses[1]}"
                    )
                else:
                    v2_pool_objects.append(pool_helper)

            # the pool manager created Erc20Token objects in the code block above,
            # so calls to `get_erc20token` will return the previously-created helper
            token_in = Erc20TokenHelperManager().get_erc20token(
                address=params["path"][0],
                silent=silent,
                min_abi=True,
                unload_brownie_contract_after_init=True,
            )
            token_out = Erc20TokenHelperManager().get_erc20token(
                address=params["path"][-1],
                silent=silent,
                min_abi=True,
                unload_brownie_contract_after_init=True,
            )

            if unwrapped_input:
                swap_in_quantity = self.value
            else:
                swap_in_quantity = params.get("amountIn")

            # predict future pool states assuming the swap executes in isolation
            future_pool_states = []
            for i, v2_pool in enumerate(v2_pool_objects):
                token_in_quantity = (
                    swap_in_quantity if i == 0 else token_out_quantity
                )

                # i == 0 for first pool in path, take from 'path' in func_params
                # otherwise, set token_in equal to token_out from previous iteration
                # and token_out equal to the other token held by the pool
                token_in = token_in if i == 0 else token_out
                token_out = (
                    v2_pool.token0
                    if token_in is v2_pool.token1
                    else v2_pool.token1
                )

                current_state = v2_pool.state
                swap_info, future_state = v2_pool.simulate_swap(
                    token_in=token_in,
                    token_in_quantity=token_in_quantity,
                )

                if (
                    future_state["reserves_token0"]
                    < current_state["reserves_token0"]
                ):
                    token_out_quantity = (
                        current_state["reserves_token0"]
                        - future_state["reserves_token0"]
                    )
                elif (
                    future_state["reserves_token1"]
                    < current_state["reserves_token1"]
                ):
                    token_out_quantity = (
                        current_state["reserves_token1"]
                        - future_state["reserves_token1"]
                    )
                else:
                    raise ValueError("Swap direction could not be identified")

                future_pool_states.append(
                    (
                        v2_pool,
                        future_state,
                    )
                )

                if not silent:
                    print(f"Simulating swap through pool: {v2_pool}")
                    print(
                        f"\t{token_in_quantity} {token_in} -> {token_out_quantity} {token_out}"
                    )
                    print("\t(CURRENT)")
                    print(
                        f"\t{v2_pool.token0}: {current_state['reserves_token0']}"
                    )
                    print(
                        f"\t{v2_pool.token1}: {current_state['reserves_token1']}"
                    )
                    print(f"\t(FUTURE)")
                    print(
                        f"\t{v2_pool.token0}: {future_state['reserves_token0']}"
                    )
                    print(
                        f"\t{v2_pool.token1}: {future_state['reserves_token1']}"
                    )

            return future_pool_states

        def v2_swap_exact_out(
            params: dict,
            unwrapped_input: Optional[bool] = False,
            silent: bool = False,
        ) -> List[Tuple[LiquidityPool, dict]]:

            pool_objects: List[LiquidityPool] = []
            for token_addresses in itertools.pairwise(params.get("path")):
                try:
                    pool_helper: LiquidityPool = self.v2_pool_manager.get_pool(
                        token_addresses=token_addresses,
                        silent=silent,
                    )
                except LiquidityPoolError:
                    raise TransactionError(
                        f"Liquidity pool could not be build for token pair {token_addresses[0]} - {token_addresses[1]}"
                    )
                else:
                    pool_objects.append(pool_helper)

            # the pool manager creates Erc20Token objects as it works,
            # so calls to `get_erc20token` will return the previously-created helper
            token_in = Erc20TokenHelperManager().get_erc20token(
                address=params["path"][0],
                silent=silent,
                min_abi=True,
                unload_brownie_contract_after_init=True,
            )
            token_out = Erc20TokenHelperManager().get_erc20token(
                address=params["path"][-1],
                silent=silent,
                min_abi=True,
                unload_brownie_contract_after_init=True,
            )

            swap_out_quantity = params.get("amountOut")

            if unwrapped_input:
                swap_in_quantity = self.value

            # predict future pool states assuming the swap executes in isolation
            # work through the pools backwards, since the swap will execute at a defined output, with input floating
            future_pool_states = []
            for i, pool in enumerate(pool_objects[::-1]):
                token_out_quantity = (
                    swap_out_quantity if i == 0 else token_out_quantity
                )

                # i == 0 for last pool in path, take from 'path' in func_params
                # otherwise, set token_out equal to token_in from previous iteration
                # and token_in equal to the other token held by the pool
                token_out = token_out if i == 0 else token_in
                token_in = (
                    pool.token0 if token_out is pool.token1 else pool.token1
                )

                current_state = pool.state
                swap_info, future_state = pool.simulate_swap(
                    token_out=token_out,
                    token_out_quantity=token_out_quantity,
                )

                # print(f"{i}: {token_in} -> {token_out}")
                # print(f"{current_state=}")
                # print(f"{future_state=}")

                if (
                    future_state["reserves_token0"]
                    > current_state["reserves_token0"]
                ):
                    token_in_quantity = (
                        future_state["reserves_token0"]
                        - current_state["reserves_token0"]
                    )
                elif (
                    future_state["reserves_token1"]
                    > current_state["reserves_token1"]
                ):
                    token_in_quantity = (
                        future_state["reserves_token1"]
                        - current_state["reserves_token1"]
                    )
                else:
                    raise ValueError("Swap direction could not be identified")

                future_pool_states.append(
                    (
                        pool,
                        future_state,
                    )
                )

                if not silent:
                    print(f"Simulating swap through pool: {pool}")
                    print(
                        f"\t{token_in_quantity} {token_in} -> {token_out_quantity} {token_out}"
                    )
                    print("\t(CURRENT)")
                    print(
                        f"\t{pool.token0}: {current_state['reserves_token0']}"
                    )
                    print(
                        f"\t{pool.token1}: {current_state['reserves_token1']}"
                    )
                    print(f"\t(FUTURE)")
                    print(
                        f"\t{pool.token0}: {future_state['reserves_token0']}"
                    )
                    print(
                        f"\t{pool.token1}: {future_state['reserves_token1']}"
                    )

            # if swap_in_quantity < token_in_quantity:
            #     raise TransactionError("msg.value too low for swap")

            return future_pool_states

        def v3_swap_exact_in(
            params: dict,
            silent: bool = False,
        ) -> List[Tuple[V3LiquidityPool, dict]]:

            # decode with Router ABI
            # https://github.com/Uniswap/v3-periphery/blob/main/contracts/interfaces/ISwapRouter.sol
            try:
                (
                    tokenIn,
                    tokenOut,
                    fee,
                    recipient,
                    deadline,
                    amountIn,
                    amountOutMinimum,
                    sqrtPriceLimitX96,
                ) = params.get("params")
            except:
                pass

            # decode with Router2 ABI
            # https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/interfaces/IV3SwapRouter.sol
            try:
                (
                    tokenIn,
                    tokenOut,
                    fee,
                    recipient,
                    amountIn,
                    amountOutMinimum,
                    sqrtPriceLimitX96,
                ) = params.get("params")
            except:
                pass

            # decode values from exactInput (hand-crafted)
            try:
                (
                    tokenIn,
                    tokenOut,
                    fee,
                    amountIn,
                ) = params.get("params")
            except:
                pass

            try:
                # get the V3 pool involved in the swap
                v3_pool = self.v3_pool_manager.get_pool(
                    token_addresses=(tokenIn, tokenOut),
                    pool_fee=fee,
                    silent=silent,
                )
            except (ManagerError, LiquidityPoolError) as e:
                raise TransactionError(f"Could not get pool (via tokens): {e}")
            except:
                raise

            if not silent:
                print(f"Predicting output of swap through pool: {v3_pool}")

            try:
                token_in_object = Erc20TokenHelperManager().get_erc20token(
                    address=tokenIn,
                    silent=silent,
                    min_abi=True,
                    unload_brownie_contract_after_init=True,
                )
            except Exception as e:
                print(e)
                print(type(e))
                raise

            try:
                swap_info, final_state = v3_pool.simulate_swap(
                    token_in=token_in_object,
                    token_in_quantity=amountIn,
                )
            except EVMRevertError as e:
                raise TransactionError(
                    f"V3 operation could not be simulated: {e}"
                )

            return [
                (
                    v3_pool,
                    final_state,
                )
            ]

        def v3_swap_exact_out(
            params: dict,
            silent: bool = False,
        ) -> List[Tuple[V3LiquidityPool, dict]]:

            sqrtPriceLimitX96 = None
            amountInMaximum = None

            # decode with Router ABI
            # https://github.com/Uniswap/v3-periphery/blob/main/contracts/interfaces/ISwapRouter.sol
            try:
                (
                    tokenIn,
                    tokenOut,
                    fee,
                    recipient,
                    deadline,
                    amountOut,
                    amountInMaximum,
                    sqrtPriceLimitX96,
                ) = params.get("params")
            except:
                pass

            # decode with Router2 ABI
            # https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/interfaces/IV3SwapRouter.sol
            try:
                (
                    tokenIn,
                    tokenOut,
                    fee,
                    recipient,
                    amountOut,
                    amountInMaximum,
                    sqrtPriceLimitX96,
                ) = params.get("params")
            except:
                pass

            # decode values from exactOutput (hand-crafted)
            try:
                (
                    tokenIn,
                    tokenOut,
                    fee,
                    amountOut,
                ) = params.get("params")
            except:
                pass

            try:
                # get the V3 pool involved in the swap
                v3_pool = self.v3_pool_manager.get_pool(
                    token_addresses=(tokenIn, tokenOut),
                    pool_fee=fee,
                    silent=silent,
                )
            except (ManagerError, LiquidityPoolError) as e:
                raise TransactionError(f"Could not get pool (via tokens): {e}")
            except:
                raise

            if not silent:
                print(f"Predicting output of swap through pool: {v3_pool}")

            try:
                token_out_object = Erc20TokenHelperManager().get_erc20token(
                    address=tokenOut,
                    silent=silent,
                    min_abi=True,
                    unload_brownie_contract_after_init=True,
                )
            except Exception as e:
                print(e)
                print(type(e))
                raise

            try:
                swap_info, final_state = v3_pool.simulate_swap(
                    token_out=token_out_object,
                    token_out_quantity=amountOut,
                    sqrt_price_limit=sqrtPriceLimitX96,
                )
            except EVMRevertError as e:
                raise TransactionError(
                    f"V3 operation could not be simulated: {e}"
                )

            # swap input is positive from the POV of the pool
            amountIn = max(
                swap_info["amount0_delta"],
                swap_info["amount1_delta"],
            )

            if amountInMaximum and amountIn < amountInMaximum:
                raise TransactionError(
                    f"amountIn ({amountIn}) < amountOutMin ({amountInMaximum})"
                )

            return [
                (
                    v3_pool,
                    final_state,
                )
            ]

        if func_name is None:
            func_name = self.func_name

        if func_params is None:
            func_params = self.func_params

        future_state = []

        try:

            # -----------------------------------------------------
            # UniswapV2 functions
            # -----------------------------------------------------

            if func_name in (
                "swapExactTokensForETH",
                "swapExactTokensForETHSupportingFeeOnTransferTokens",
            ):
                if not silent:
                    print(func_name)
                future_state.extend(
                    v2_swap_exact_in(func_params, silent=silent)
                )

            elif func_name in (
                "swapExactETHForTokens",
                "swapExactETHForTokensSupportingFeeOnTransferTokens",
            ):
                if not silent:
                    print(func_name)
                future_state.extend(
                    v2_swap_exact_in(
                        func_params, unwrapped_input=True, silent=silent
                    )
                )

            elif func_name in [
                "swapExactTokensForTokens",
                "swapExactTokensForTokensSupportingFeeOnTransferTokens",
            ]:
                if not silent:
                    print(func_name)
                future_state.extend(
                    v2_swap_exact_in(func_params, silent=silent)
                )

            elif func_name in ("swapTokensForExactETH"):
                if not silent:
                    print(func_name)
                future_state.extend(
                    v2_swap_exact_out(params=func_params, silent=silent)
                )

            elif func_name in ("swapTokensForExactTokens"):
                if not silent:
                    print(func_name)
                future_state.extend(
                    v2_swap_exact_out(params=func_params, silent=silent)
                )

            elif func_name in ("swapETHForExactTokens"):
                if not silent:
                    print(func_name)
                future_state.extend(
                    v2_swap_exact_out(
                        params=func_params, unwrapped_input=True, silent=silent
                    )
                )

            # -----------------------------------------------------
            # UniswapV3 functions
            # -----------------------------------------------------
            elif func_name == "multicall":
                if not silent:
                    print(func_name)
                future_state = self.simulate_multicall(silent=silent)
            elif func_name == "exactInputSingle":
                if not silent:
                    print(func_name)
                # v3_pool, swap_info, pool_state = v3_swap_exact_in(
                #     params=func_params
                # )
                # future_state.append([v3_pool, pool_state])
                future_state.extend(
                    v3_swap_exact_in(params=func_params, silent=silent)
                )
            elif func_name == "exactInput":
                if not silent:
                    print(func_name)

                try:
                    (
                        exactInputParams_path,
                        exactInputParams_recipient,
                        exactInputParams_deadline,
                        exactInputParams_amountIn,
                        exactInputParams_amountOutMinimum,
                    ) = func_params.get("params")
                except:
                    pass

                try:
                    (
                        exactInputParams_path,
                        exactInputParams_recipient,
                        exactInputParams_amountIn,
                        exactInputParams_amountOutMinimum,
                    ) = func_params.get("params")
                except:
                    pass

                # decode the path
                path_pos = 0
                exactInputParams_path_decoded = []
                # read alternating 20 and 3 byte chunks from the encoded path,
                # store each address (hex) and fee (int)
                for byte_length in itertools.cycle((20, 3)):
                    # stop at the end
                    if path_pos == len(exactInputParams_path):
                        break
                    elif (
                        byte_length == 20
                        and len(exactInputParams_path)
                        >= path_pos + byte_length
                    ):
                        address = exactInputParams_path[
                            path_pos : path_pos + byte_length
                        ].hex()
                        exactInputParams_path_decoded.append(address)
                    elif (
                        byte_length == 3
                        and len(exactInputParams_path)
                        >= path_pos + byte_length
                    ):
                        fee = int(
                            exactInputParams_path[
                                path_pos : path_pos + byte_length
                            ].hex(),
                            16,
                        )
                        exactInputParams_path_decoded.append(fee)
                    path_pos += byte_length

                if not silent:
                    print(f" • path = {exactInputParams_path_decoded}")
                    print(f" • recipient = {exactInputParams_recipient}")
                    if exactInputParams_deadline:
                        print(f" • deadline = {exactInputParams_deadline}")
                    print(f" • amountIn = {exactInputParams_amountIn}")
                    print(
                        f" • amountOutMinimum = {exactInputParams_amountOutMinimum}"
                    )

                # decode the path - tokenIn is the first position, tokenOut is the second position
                # e.g. tokenIn, fee, tokenOut
                for token_pos in range(
                    0,
                    len(exactInputParams_path_decoded) - 2,
                    2,
                ):
                    tokenIn = exactInputParams_path_decoded[token_pos]
                    fee = exactInputParams_path_decoded[token_pos + 1]
                    tokenOut = exactInputParams_path_decoded[token_pos + 2]

                    v3_pool, swap_info, pool_state = v3_swap_exact_in(
                        params={
                            "params": (
                                tokenIn,
                                tokenOut,
                                fee,
                                # use amountIn for the first swap, otherwise take the output
                                # amount of the last swap (always negative so we can check
                                # for the min without knowing the token positions)
                                exactInputParams_amountIn
                                if token_pos == 0
                                else min(swap_info.values()),
                            )
                        },
                        silent=silent,
                    )
                    future_state.append([v3_pool, pool_state])
            elif func_name == "exactOutputSingle":
                if not silent:
                    print(func_name)
                # v3_pool, swap_info, pool_state = v3_swap_exact_out(
                #     params=func_params
                # )
                future_state.extend(
                    v3_swap_exact_out(params=func_params, silent=silent)
                )
            elif func_name == "exactOutput":
                if not silent:
                    print(func_name)

                # Router ABI
                try:
                    (
                        exactOutputParams_path,
                        exactOutputParams_recipient,
                        exactOutputParams_deadline,
                        exactOutputParams_amountOut,
                        exactOutputParams_amountInMaximum,
                    ) = func_params.get("params")
                except Exception as e:
                    pass

                # Router2 ABI
                try:
                    (
                        exactOutputParams_path,
                        exactOutputParams_recipient,
                        exactOutputParams_amountOut,
                        exactOutputParams_amountInMaximum,
                    ) = func_params.get("params")
                except Exception as e:
                    pass

                # decode the path
                path_pos = 0
                exactOutputParams_path_decoded = []
                # read alternating 20 and 3 byte chunks from the encoded path,
                # store each address (hex) and fee (int)
                for byte_length in itertools.cycle((20, 3)):
                    # stop at the end
                    if path_pos == len(exactOutputParams_path):
                        break
                    elif (
                        byte_length == 20
                        and len(exactOutputParams_path)
                        >= path_pos + byte_length
                    ):
                        address = exactOutputParams_path[
                            path_pos : path_pos + byte_length
                        ].hex()
                        exactOutputParams_path_decoded.append(address)
                    elif (
                        byte_length == 3
                        and len(exactOutputParams_path)
                        >= path_pos + byte_length
                    ):
                        fee = int(
                            exactOutputParams_path[
                                path_pos : path_pos + byte_length
                            ].hex(),
                            16,
                        )
                        exactOutputParams_path_decoded.append(fee)
                    path_pos += byte_length

                if not silent:
                    print(f" • path = {exactOutputParams_path_decoded}")
                    print(f" • recipient = {exactOutputParams_recipient}")
                    if exactOutputParams_deadline:
                        print(f" • deadline = {exactOutputParams_deadline}")
                    print(f" • amountOut = {exactOutputParams_amountOut}")
                    print(
                        f" • amountInMaximum = {exactOutputParams_amountInMaximum}"
                    )

                # the path is encoded in REVERSE order, so we decode from start to finish
                # tokenOut is the first position, tokenIn is the second position
                # e.g. tokenOut, fee, tokenIn
                for token_pos in range(
                    0,
                    len(exactOutputParams_path_decoded) - 2,
                    2,
                ):
                    tokenOut = exactOutputParams_path_decoded[token_pos]
                    fee = exactOutputParams_path_decoded[token_pos + 1]
                    tokenIn = exactOutputParams_path_decoded[token_pos + 2]

                    v3_pool, swap_info, pool_state = v3_swap_exact_out(
                        params={
                            "params": (
                                tokenIn,
                                tokenOut,
                                fee,
                                # use amountOut for the last swap (token_pos == 0),
                                # otherwise take the input amount of the previous swap
                                # (always positive so we can check for the max without
                                # knowing the token positions)
                                exactOutputParams_amountOut
                                if token_pos == 0
                                else max(swap_info.values()),
                            )
                        },
                        silent=silent,
                    )

                    future_state.append([v3_pool, pool_state])
            elif func_name in (
                "addLiquidity",
                "addLiquidityETH",
                "removeLiquidity",
                "removeLiquidityETH",
                "removeLiquidityETHWithPermit",
                "removeLiquidityETHSupportingFeeOnTransferTokens",
                "removeLiquidityETHWithPermitSupportingFeeOnTransferTokens",
                "removeLiquidityWithPermit",
                "swapExactTokensForTokensSupportingFeeOnTransferTokens",
                "swapExactETHForTokensSupportingFeeOnTransferTokens",
                "swapExactTokensForETHSupportingFeeOnTransferTokens",
            ):
                # TODO: add prediction for these functions
                if not silent:
                    print(f"TODO: {func_name}")
            elif func_name in (
                "refundETH",
                "selfPermit",
                "selfPermitAllowed",
                "unwrapWETH9",
            ):
                # ignore, these functions do not affect future pool states
                pass
            else:
                print(f"\tUNHANDLED function: {func_name}")

        # WIP: catch ValueError to avoid bad inputs to the swap bubbling out of the TX helper
        except (LiquidityPoolError, ValueError) as e:
            raise TransactionError(f"Transaction could not be calculated: {e}")
        else:
            return future_state

    def simulate_multicall(self, silent: bool = False):

        future_state = []

        for payload in self.func_params.get("data"):
            try:
                # decode with Router ABI
                payload_func, payload_args = (
                    web3.Web3()
                    .eth.contract(abi=UNISWAP_V3_ROUTER_ABI)
                    .decode_function_input(payload)
                )
            except:
                pass

            try:
                # decode with Router2 ABI
                payload_func, payload_args = (
                    web3.Web3()
                    .eth.contract(abi=UNISWAP_V3_ROUTER2_ABI)
                    .decode_function_input(payload)
                )
            except:
                pass

            try:
                # simulate each payload individually and append the future_state dict of that payload
                # payload_pool, payload_state = self.simulate(
                #     func_name=payload_func.fn_name,
                #     func_params=payload_args,
                # )
                future_state.extend(
                    self.simulate(
                        func_name=payload_func.fn_name,
                        func_params=payload_args,
                        silent=silent,
                    )
                )
            except Exception as e:
                raise TransactionError(f"Could not decode multicall: {e}")

        return future_state
