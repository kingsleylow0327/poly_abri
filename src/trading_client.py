import logging

from datetime import datetime
from dto.order_dto import OrderDto
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY, SELL

from src.config import Settings

logger = logging.getLogger(__name__)


_cached_client = None

def get_client(settings: Settings) -> ClobClient:
    global _cached_client
    
    if _cached_client is not None:
        return _cached_client
    
    if not settings.private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is required for trading")
    
    host = "https://clob.polymarket.com"
    
    # 为 Magic/Email 账户创建 signature_type=1 的客户端
    _cached_client = ClobClient(
        host, 
        key=settings.private_key.strip(), 
        chain_id=137, 
        signature_type=settings.signature_type, 
        funder=settings.funder.strip() if settings.funder else None
    )
    
    # 派生 API 凭证 - 简单有效的方法
    logger.info("正在从私钥派生用户 API 凭证...")
    derived_creds = _cached_client.create_or_derive_api_creds()
    _cached_client.set_api_creds(derived_creds)
    
    logger.info("✅ API 凭证配置成功")
    logger.info(f"   API Key: {derived_creds.api_key}")
    logger.info(f"   钱包地址: {_cached_client.get_address()}")
    logger.info(f"   资金方: {settings.funder}")
    
    return _cached_client


def get_balance(settings: Settings) -> float:
    """从 Polymarket 账户获取 USDC 余额。"""
    try:
        client = get_client(settings)
        # 获取 USDC (COLLATERAL) 余额
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=settings.signature_type
        )
        result = client.get_balance_allowance(params)
        
        if isinstance(result, dict):
            balance_raw = result.get("balance", "0")
            balance_wei = float(balance_raw)
            # USDC 有18位小数
            balance_usdc = balance_wei / 1_000_000
            return balance_usdc
        
        logger.warning(f"获取余额时收到意外响应: {result}")
        return 0.0
    except Exception as e:
        logger.error(f"获取余额时出错: {e}")
        return 0.0


def place_order(settings: Settings, *, side: str, token_id: str, price: float, size: float, tif: str = "GTC") -> dict:
    if price <= 0:
        raise ValueError("price must be > 0")
    if size <= 0:
        raise ValueError("size must be > 0")
    if not token_id:
        raise ValueError("token_id is required")

    side_up = side.upper()
    if side_up not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")

    client = get_client(settings)
    
    try:
        # 创建订单参数
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY if side_up == "BUY" else SELL
        )
        
        # 不要使用 PartialCreateOrderOptions(neg_risk=True) - 会导致 "invalid signature"
        # 客户端会从 token_id 自动检测 neg_risk
        signed_order = client.create_order(order_args)
        
        # 以 GTC (Good-Til-Cancelled) 方式提交订单 - 保留在订单簿中直到成交
        return client.post_order(signed_order, OrderType.GTC)
    except Exception as exc:  # pragma: no cover - 从客户端传递
        raise RuntimeError(f"place_order failed: {exc}") from exc


def place_orders_fast(settings: Settings, orders: list[dict]) -> list[dict]:
    """
    尽可能快地提交多个订单。
    策略：先预签所有订单，然后快速连续提交。
    这样可以最小化订单提交之间的时间间隔。
    参数:
        settings: 机器人设置
        orders: 订单字典列表，包含键: side, token_id, price, size
    返回:
        订单结果列表
    """
    client = get_client(settings)

    post_args: list[PostOrdersArgs] = []
    for order_params in orders:
        side_up = order_params["side"].upper()
        order_args = OrderArgs(
            token_id=order_params["token_id"],
            price=order_params["price"],
            size=order_params["size"],
            side=BUY if side_up == "BUY" else SELL
        )
        signed_order = client.create_order(order_args)
        post_args.append(PostOrdersArgs(order=signed_order, orderType=OrderType.GTC))

    try:
        # 批量提交签好的订单，减少出单间隔
        return client.post_orders(post_args)
    except Exception as exc:
        return [{"error": str(exc)}]

def place_orders_market(settings: Settings, orders: dict) -> list[dict]:
    client = get_client(settings)

    post_args: list[PostOrdersArgs] = []
    side_up = orders["side"].upper()
    order_args = OrderArgs(
        token_id=orders["token_id"],
        price=orders["price"],
        size=orders["size"],
        side=BUY if side_up == "BUY" else SELL
    )
    signed_order = client.create_order(order_args)
    post_args.append(PostOrdersArgs(order=signed_order, orderType=OrderType.FAK))

    try:
        return client.post_orders(post_args)
    except Exception as exc:
        return [{"error": str(exc)}]

def execute_market_buy(settings: Settings, order_dto: OrderDto) -> list[dict]:
    order = order_dto.to_dict()
    order["side"] = "BUY"
    return place_orders_market(settings, order)
    

def execute_market_sell(settings: Settings, order_dto: OrderDto) -> list[dict]:
    order = order_dto.to_dict()
    order["side"] = "SELL"
    return place_orders_market(settings, order)

def is_tp_sl_success(settings: Settings, order_dto: OrderDto) -> bool:
    results = execute_market_sell(settings, order_dto)
    errors = [r for r in results if isinstance(r, dict) and r.get("errorMsg")]
    if errors:
        for err in errors:
            error_msg = f"❌ 订单错误: {err.get('errorMsg', err)}"
            logger.error(error_msg)
            with open("error.txt", "a") as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{timestamp}] {error_msg}\n")
                f.write(f"Full error details: {err}\n\n")
        return False
    return True


def get_positions(settings: Settings, token_ids: list[str] = None) -> dict:
    try:
        import httpx
        
        # 打印传入的 token_ids 参数
        logger.info(f"get_positions 被调用，token_ids: {token_ids}")
        # 或者使用 print
        print(f"📋 查询持仓，筛选条件: {token_ids}")

        # 使用 FUNDER 地址作为用户地址查询持仓
        user_address = settings.funder
        if not user_address:
            logger.error("未配置 POLYMARKET_FUNDER 地址")
            return {}
        
        # 通过 REST API 获取持仓
        api_url = f"https://data-api.polymarket.com/positions?user={user_address}"
        response = httpx.get(api_url, timeout=10)
        response.raise_for_status()
        
        positions = response.json()
        print(f"📦 获取到 {len(positions)} 个持仓记录")
        # 如果提供了 token_ids，则进行过滤
        result = {}
        for pos in positions:
            # 从响应中提取 token_id，可能在不同的字段中
            token_id = pos.get("asset")
            print(f"🔍 链上检查，token_id: {token_id}")
            if token_id:
                if token_ids is None or token_id in token_ids:
                    size = float(pos.get("size", 0))
                    avg_price = float(pos.get("avg_price", 0)) if pos.get("avg_price") else 0
                    result[token_id] = {
                        "size": size,
                        "avg_price": avg_price,
                        "raw": pos
                    }
        
        return result
    except Exception as e:
        logger.error(f"获取持仓时出错: {e}")
        return {}
