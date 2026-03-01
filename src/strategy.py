"""
Polymarket Arbitrage Strategy Bot

策略：当总成本 < $1.00 时买入两边（UP 和 DOWN）
以保证无论结果如何都能获利。
"""

import asyncio
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Optional, Tuple, List

import httpx

from config import load_settings
from market_lookup import fetch_market_from_slug
from trading_client import get_client, place_order, get_positions, place_orders_fast
from py_clob_client.clob_types import BookParams

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 禁用来自 httpx 的 HTTP 日志
logging.getLogger("httpx").setLevel(logging.WARNING)


def find_current_15min_market(symbol: str) -> str:
    """
    在 Polymarket 上查找当前活跃的 15分钟市场。

    搜索匹配模式 'updown-15m-<timestamp>' 的市场
    并返回最近/活跃市场的 slug。
    """
    logger.info(f"正在搜索当前活跃的 {symbol} 15分钟市场...")

    try:
        # 在 Polymarket 的加密货币 15分钟页面上搜索
        page_url = "https://polymarket.com/crypto/15M"
        resp = httpx.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()

        # 在 HTML 中查找 市场 slug
        pattern = rf'{symbol}-updown-15m-(\d+)'
        matches = re.findall(pattern, resp.text)

        if not matches:
            raise RuntimeError(f"No active {symbol} 15min market found")

        now = datetime.now(timezone.utc)

        # Round the minutes down to the nearest 15
        minute = (now.minute // 15) * 15
        aligned_dt = now.replace(minute=minute, second=0, microsecond=0)

        # Convert back to timestamp
        aligned_ts = int(aligned_dt.timestamp())

        # 获取最近的时间戳（最当前的市场）
        slug = f"{symbol}-updown-15m-{aligned_ts}"

        logger.info(f"✅ 找到市场: {slug}")
        return slug

    except Exception as e:
        logger.error(f"搜索 {symbol} 15分钟市场时出错: {e}")
        # 退回方案：尝试使用最后一个已知的
        logger.warning("使用配置中的默认市场...")
        raise


class SimpleArbitrageBot:
    """实现 Jeremy Whittaker 策略的简单机器人。"""

    def __init__(self, settings, symbol):
        self.settings = settings
        self.client = get_client(settings)

        # 尝试自动查找当前的 15分钟市场
        try:
            market_slug = find_current_15min_market(symbol)
        except Exception as e:
            # 退回方案：使用 .env 中配置的 slug
            if settings.market_slug:
                logger.info(f"使用配置的市场: {settings.market_slug}")
                market_slug = settings.market_slug
            else:
                raise RuntimeError(f"Could not find {symbol} 15min market and no slug configured in .env")

        # 从市场获取代币 ID
        logger.info(f"正在获取市场信息: {market_slug}")
        market_info = fetch_market_from_slug(market_slug)

        self.market_id = market_info["market_id"]
        self.yes_token_id = market_info["yes_token_id"]
        self.no_token_id = market_info["no_token_id"]

        logger.info(f"市场 ID: {self.market_id}")
        logger.info(f"Up代币 (YES): {self.yes_token_id}")
        logger.info(f"Down代币 (NO): {self.no_token_id}")

        # 提取市场时间戳以计算剩余时间
        # slug 中的时间戳是市场开放时间，而不是关闭时间
        # 15分钟市场在15分钟（900秒）后关闭
        import re
        match = re.search(rf'{symbol}-updown-15m-(\d+)', market_slug)
        market_start = int(match.group(1)) if match else None
        self.market_end_timestamp = market_start + 900 if market_start else None  # +15 分钟
        self.market_slug = market_slug

        self.last_check = None
        self.opportunities_found = 0
        self.trades_executed = 0

        # 投资跟踪
        self.total_invested = 0.0
        self.total_shares_bought = 0
        self.positions = []  # 未平仓持仓列表

        # 缓存余额（每次交易后更新）
        self.cached_balance = None

        # 当前市场的交易次数（每个新市场重置为0）
        self.current_market_trades = 0

    def get_time_remaining(self) -> str:
        """获取市场关闭前的剩余时间。"""
        if not self.market_end_timestamp:
            return "Unknown"

        from datetime import datetime
        now = int(datetime.now().timestamp())
        remaining = self.market_end_timestamp - now

        if remaining <= 0:
            return "CLOSED"

        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        return f"{minutes}m {seconds}s"

    def get_balance(self) -> float:
        """获取当前 USDC 余额。"""
        from trading_client import get_balance
        return get_balance(self.settings)

    def get_current_prices(self) -> tuple[float, float, int, int, float, float] | tuple[None, None, None, None,None,None]:
        """
        使用最后交易价格获取当前价格（像原始版本一样）。
        同时获取订单簿流动性以验证是否有足够的股份。
        返回:
            (up_price, down_price, up_size, down_size) - 价格和可用数量
        """
        try:
            # 批量获取两个方向的最后交易价格

            params = [
                BookParams(token_id=self.yes_token_id),
                BookParams(token_id=self.no_token_id)
            ]
            prices_response = self.client.get_last_trades_prices(params=params)

            # prices_response 应为列表或字典，需根据SDK实际返回结构调整
            price_up = price_down = 0
            for item in prices_response:
                if item.get("token_id") == self.yes_token_id:
                    price_up = float(item.get("price", 0))
                elif item.get("token_id") == self.no_token_id:
                    price_down = float(item.get("price", 0))
            # 获取订单簿以检查可用流动性
                    # 获取订单簿数据（一次请求拿到 UP/DOWN）
            books = self._fetch_orderbooks([self.yes_token_id, self.no_token_id])
            orderbook_up = books.get(self.yes_token_id, {})
            orderbook_down = books.get(self.no_token_id, {})
            size_up = orderbook_up.get("ask_size", 0)
            size_down = orderbook_down.get("ask_size", 0)
            best_up = orderbook_up.get("best_ask", 0)
            best_down = orderbook_down.get("best_ask", 0)
            print(price_up, best_up)
            print(price_down, best_down)
            return price_up, price_down, size_up, size_down, best_up, best_down
        except Exception as e:
            logger.error(f"获取价格时出错: {e}")
            return None, None, None, None, None, None

    def _fetch_orderbooks(self, token_ids: List[str]) -> dict:
        """批量获取多个 token 的订单簿（一次请求），按 asset_id 映射。"""
        try:
            params = [BookParams(token_id=t) for t in token_ids]
            orderbooks = self.client.get_order_books(params=params)

            result = {}
            for ob in orderbooks:
                asset_id = getattr(ob, "asset_id", None) or getattr(ob, "token_id", None)
                if not asset_id:
                    continue
                bids = ob.bids if hasattr(ob, 'bids') and ob.bids else []
                asks = ob.asks if hasattr(ob, 'asks') and ob.asks else []
                best_bid = float(bids[-1].price) if bids else None
                best_ask = float(asks[-1].price) if asks else None
                spread = (best_ask - best_bid) if (best_bid and best_ask) else None
                result[asset_id] = {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": spread,
                    "bid_size": float(bids[-1].size) if bids else 0,
                    "ask_size": float(asks[-1].size) if asks else 0
                }
            return result
        except Exception as e:
            logger.error(f"? 获取订单簿时出错: {e}")
            return {}
    
    def check_arbitrage(self) -> Optional[dict]:
        """
        检查是否存在套利机会。
        使用订单簿（最佳卖价）获取我们可以买入的真实价格。
        同时验证这些价格下有足够的流动性。
        如果机会存在则返回包含信息的字典，否则返回 None。
        """
        price_up, price_down, size_up, size_down, best_up, best_down = self.get_current_prices()
        # 存储最近一次价格参数，供run_once复用
        self.last_price_info = (price_up, price_down, size_up, size_down, best_up, best_down)
        
        if price_up is None or price_down is None or price_up >= 0.75 or price_down >= 0.75:
            return None
        
        # 检查价格差异
        up_diff = abs(price_up - best_up)
        down_diff = abs(price_down - best_down)
        
        if (up_diff > 0.03 and down_diff > 0.03) or (up_diff + down_diff > 0.05):
            logger.warning(f"差价过高: UP差价={up_diff:.4f}, DOWN差价={down_diff:.4f}, 总差价={up_diff + down_diff:.4f}")
            return None
        
        # 计算总成本
        total_cost = price_up + price_down
        
        # 检查是否存在套利（总成本 < 1.0）
        if total_cost < self.settings.target_pair_cost:
            # 验证这些价格下有足够的流动性（留出5股的安全边际）
            safety_margin = 5
            available_up = size_up - safety_margin
            available_down = size_down - safety_margin
            
            # 两边都要满足：可用数量 >= 订单数量
            if available_up < self.settings.order_size or available_down < self.settings.order_size:
                logger.debug(
                    f"流动性不足: 上涨={size_up:.2f}(可用{available_up:.2f}), "
                    f"下跌={size_down:.2f}(可用{available_down:.2f}), "
                    f"需要={self.settings.order_size}"
                )
                return None
            
            profit = 1.0 - total_cost
            profit_pct = (profit / total_cost) * 100

            # 使用订单数量计算
            investment = total_cost * self.settings.order_size
            expected_payout = 1.0 * self.settings.order_size
            expected_profit = expected_payout - investment
            
            return {
                "price_up": price_up,
                "price_down": price_down,
                "total_cost": total_cost,
                "profit_per_share": profit,
                "profit_pct": profit_pct,
                "order_size": self.settings.order_size,
                "total_investment": investment,
                "expected_payout": expected_payout,
                "expected_profit": expected_profit,
                "size_up": size_up,
                "size_down": size_down,
                "timestamp": datetime.now().isoformat()
            }
        
        return None
    
    def execute_arbitrage(self, opportunity: dict):
        """通过买入两边执行套利。"""
        
        # 计算发现的机会（无论是否执行）
        self.opportunities_found += 1
        
        logger.info("="  * 70)
        logger.info("🎯 检测到套利机会")
        logger.info("=" * 70)
        logger.info(f"Up价格:           ${opportunity['price_up']:.4f}")
        logger.info(f"Down价格:           ${opportunity['price_down']:.4f}")
        logger.info(f"总成本:             ${opportunity['total_cost']:.4f}")
        logger.info(f"每股利润:           ${opportunity['profit_per_share']:.4f}")
        logger.info(f"利润百分比:         {opportunity['profit_pct']:.2f}%")
        logger.info("-" * 70)
        logger.info(f"订单数量:           {opportunity['order_size']} 股（每边）")
        logger.info(f"总投资:             ${opportunity['total_investment']:.2f}")
        logger.info(f"预期支付:           ${opportunity['expected_payout']:.2f}")
        logger.info(f"预期利润:           ${opportunity['expected_profit']:.2f}")
        logger.info("=" * 70)
        
        # 检查剩余时间是否足够
        if self.settings.min_time_remaining_minutes > 0:
            if self.market_end_timestamp:
                from datetime import datetime
                now = int(datetime.now().timestamp())
                remaining_seconds = self.market_end_timestamp - now
                remaining_minutes = remaining_seconds / 60
                
                if remaining_minutes < self.settings.min_time_remaining_minutes:
                    logger.info("=" * 70)
                    logger.info(f"⚠️ 市场剩余时间不足: {remaining_minutes:.1f} 分钟")
                    logger.info(f"最小要求: {self.settings.min_time_remaining_minutes} 分钟")
                    logger.info("为避免风险，跳过本次交易")
                    logger.info("=" * 70)
                    return
        
        # 检查是否达到当前市场的交易次数限制
        if self.settings.max_trades_per_market > 0:
            if self.current_market_trades >= self.settings.max_trades_per_market:
                logger.info("=" * 70)
                logger.info(f"⚠️ 当前场次已完成 {self.current_market_trades} 次套利交易")
                logger.info(f"已达到设定的最大交易次数限制: {self.settings.max_trades_per_market}")
                logger.info("将等待下一个市场开始...")
                logger.info("=" * 70)
                return
            else:
                logger.info(f"当前市场交易进度: {self.current_market_trades}/{self.settings.max_trades_per_market}")
        
        if self.settings.dry_run:
            logger.info("=" * 70)
            # 跟踪模拟投资
            self.total_invested += opportunity['total_investment']
            self.total_shares_bought += opportunity['order_size'] * 2  # UP + DOWN
            self.positions.append(opportunity)
            # 模拟模式下也增加计数器
            self.current_market_trades += 1
            return
        
        try:
            # 执行订单
            logger.info("\n📤 正在并行执行订单...")
            
            # 使用套利机会中的精确价格
            up_price = opportunity['price_up']
            down_price = opportunity['price_down']
            
            # 准备两个订单
            orders = [
                {
                    "side": "BUY",
                    "token_id": self.yes_token_id,
                    "price": up_price,
                    "size": self.settings.order_size
                },
                {
                    "side": "BUY",
                    "token_id": self.no_token_id,
                    "price": down_price,
                    "size": self.settings.order_size
                }
            ]
            
            logger.info(f"   上涨:   {self.settings.order_size} 股 @ ${up_price:.4f}")
            logger.info(f"   下跌: {self.settings.order_size} 股 @ ${down_price:.4f}")
            
            # 尽可能快地执行两个订单
            results = place_orders_fast(self.settings, orders)
            
            # 检查结果
            errors = [r for r in results if isinstance(r, dict) and "error" in r]
            if errors:
                for err in errors:
                    error_msg = f"❌ 订单错误: {err['error']}"
                    logger.error(error_msg)
                    # Log error to file
                    with open("error.txt", "a") as f:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        f.write(f"[{timestamp}] {error_msg}\n")
                        f.write(f"Full error details: {err}\n\n")
                raise RuntimeError(f"Some orders failed: {errors}")
            
            logger.info(f"✅ 上涨订单已执行")
            logger.info(f"✅ 下跌订单已执行")
            
            # 验证持仓是否平衡
            import time
            time.sleep(1)  # 等待订单结算
            
            positions = get_positions(self.settings, [self.yes_token_id, self.no_token_id])
            up_shares = positions.get(self.yes_token_id, {}).get("size", 0)
            down_shares = positions.get(self.no_token_id, {}).get("size", 0)
            
            if abs(up_shares - down_shares) > 0.1:
                logger.warning(f"⚠️ 检测到位置不平衡！")
                logger.warning(f"   上涨股份: {up_shares:.2f}")
                logger.warning(f"   下跌股份: {down_shares:.2f}")
                logger.warning(f"   差异: {abs(up_shares - down_shares):.2f}")
                logger.warning("   ⚠️ 可能需要人工干预以平衡位置")
            
            logger.info("\n" + "=" * 70)
            logger.info("✅ 套利执行成功")
            logger.info("=" * 70)
            
            self.trades_executed += 1
            self.current_market_trades += 1  # 增加当前市场的交易计数
            
            # 跟踪真实投资
            self.total_invested += opportunity['total_investment']
            self.total_shares_bought += opportunity['order_size'] * 2  # UP + DOWN
            self.positions.append(opportunity)
            
            # 交易后更新缓存余额
            new_balance = self.get_balance()
            self.cached_balance = new_balance
            logger.info(f"💰 更新后余额: ${new_balance:.2f}")
            
            # 获取并显示当前持仓
            self.show_current_positions()
            
        except Exception as e:
            logger.error(f"\n❌ 执行套利时出错: {e}")
            logger.error("❌ 订单未执行 - 未更新跟踪信息")
    
    def show_current_positions(self):
        """显示 UP 和 DOWN 代币的当前股份持仓。"""
        try:
            positions = get_positions(self.settings, [self.yes_token_id, self.no_token_id])
            
            up_shares = positions.get(self.yes_token_id, {}).get("size", 0)
            down_shares = positions.get(self.no_token_id, {}).get("size", 0)
            
            logger.info("-" * 70)
            logger.info("📊 当前持仓:")
            logger.info(f"   上涨股份:   {up_shares:.2f}")
            logger.info(f"   下跌股份: {down_shares:.2f}")
            logger.info("-" * 70)
            
        except Exception as e:
            logger.warning(f"无法获取持仓: {e}")
    
    def get_market_result(self) -> Optional[str]:
        """获取哪个选项赢得了市场。"""
        try:
            # 获取最终价格
            price_up, price_down, _, _, _, _ = self.get_current_prices()
            
            if price_up is None or price_down is None or price_up >= 0.8 or price_down >= 0.8:
                return None
            
            # 在关闭的市场中，赢家价格为 1.0，输家为 0.0
            if price_up >= 0.99:
                return "UP (goes up) 📈"
            elif price_down >= 0.99:
                return "DOWN (goes down) 📉"
            else:
                # 市场尚未解决，查看哪个概率更高
                if price_up > price_down:
                    return f"UP leading ({price_up:.2%})"
                else:
                    return f"DOWN leading ({price_down:.2%})"
        except Exception as e:
            logger.error(f"获取结果时出错: {e}")
            return None
    
    def show_final_summary(self):
        """市场关闭时显示最终总结。"""
        logger.info("\n" + "=" * 70)
        logger.info("🏁 市场已关闭 - 最终总结")
        logger.info("=" * 70)
        logger.info(f"市场: {self.market_slug}")
        
        # Get market result
        result = self.get_market_result()
        if result:
            logger.info(f"结果: {result}")
        
        logger.info(f"模式: {'🔸 模拟' if self.settings.dry_run else '🔴 真实交易'}")
        logger.info("-" * 70)
        logger.info(f"检测到的机会总数:        {self.opportunities_found}")
        logger.info(f"执行的交易总数:        {self.trades_executed if not self.settings.dry_run else self.opportunities_found}")
        logger.info(f"购买的股份总数:        {self.total_shares_bought}")
        logger.info("-" * 70)
        logger.info(f"总投资:                  ${self.total_invested:.2f}")
        
        # 计算预期利润
        expected_payout = (self.total_shares_bought / 2) * 1.0  # 每对支付 $1.00
        expected_profit = expected_payout - self.total_invested
        profit_pct = (expected_profit / self.total_invested * 100) if self.total_invested > 0 else 0
        
        logger.info(f"关闭时预期支付:        ${expected_payout:.2f}")
        logger.info(f"预期利润:              ${expected_profit:.2f} ({profit_pct:.2f}%)")
        logger.info("=" * 70)
    
    def run_once(self) -> bool:
        """扫描一次寻找机会。"""
        # 检查市场是否关闭
        time_remaining = self.get_time_remaining()
        if time_remaining == "CLOSED":
            return False  # 发出停止机器人的信号
        
        opportunity = self.check_arbitrage()
        if opportunity:
            self.execute_arbitrage(opportunity)
            return True
        else:
            # 直接复用check_arbitrage中存储的价格参数，避免重复请求
            if hasattr(self, 'last_price_info') and self.last_price_info:
                price_up, price_down, size_up, size_down, best_up, best_down = self.last_price_info
            else:
                return False
            if price_up and price_down:
                total = price_up + price_down
                needed = self.settings.target_pair_cost - total
                logger.info(
                    f"无套利机会: 上涨=${price_up:.4f} ({size_up:.0f}) + 下跌=${price_down:.4f} ({size_down:.0f}) "
                    f"= ${total:.4f} (需要 <= ${self.settings.target_pair_cost:.2f}) "
                    f"[剩余时间: {time_remaining}]"
                )
            return False
    
    async def monitor(self, symbol: str, interval_seconds: int = 10):
        """持续监控套利机会。"""
        logger.info("=" * 70)
        logger.info(f"🚀 {symbol} 15分钟套利机器人已启动")
        logger.info("=" * 70)
        current_balance = self.get_balance()
        logger.info(f"💰 钱包余额: ${current_balance:.2f}")
        logger.info(f"市场: {self.market_slug}")
        logger.info(f"剩余时间: {self.get_time_remaining()}")
        logger.info(f"模式: {'🔸 模拟' if self.settings.dry_run else '🔴 真实交易'}")
        logger.info(f"成本阈值: ${self.settings.target_pair_cost:.2f}")
        logger.info(f"订单数量: {self.settings.order_size} 股")
        logger.info(f"扫描间隔: {interval_seconds}秒")
        if self.settings.max_trades_per_market > 0:
            logger.info(f"每场次最大交易次数: {self.settings.max_trades_per_market}")
        else:
            logger.info(f"每场次最大交易次数: 无限制")
        if self.settings.min_time_remaining_minutes > 0:
            logger.info(f"最小剩余时间要求: {self.settings.min_time_remaining_minutes} 分钟")
        logger.info("=" * 70)
        logger.info("")
        
        scan_count = 0
        
        try:
            while True:
                scan_count += 1
                logger.info(f"\n[Scan #{scan_count} {symbol.upper()}] {datetime.now().strftime('%H:%M:%S')}")
                
                # 检查市场是否关闭
                if self.get_time_remaining() == "CLOSED":
                    logger.info("🚨 市场已关闭！")
                    self.show_final_summary()
                    
                    # 搜索下一个市场
                    logger.info(f"🔄 正在搜索下一个 {symbol} 15分钟市场...")
                    try:
                        new_market_slug = find_current_15min_market(symbol)
                        if new_market_slug != self.market_slug:
                            logger.info(f"✅ 找到新市场: {new_market_slug}")
                            logger.info("正在使用新市场重启机器人...")
                            # 使用新市场重启机器人（会重置 current_market_trades 为 0）
                            self.__init__(self.settings)
                            scan_count = 0
                            continue
                        else:
                            logger.info("⏳ 等待新市场... (30秒)")
                            await asyncio.sleep(30)
                            continue
                    except Exception as e:
                        logger.error(f"搜索新市场时出错: {e}")
                        logger.info("将在30秒后重试...")
                        await asyncio.sleep(30)
                        continue
                
                self.run_once()
                
                logger.info(f"发现的机会: {self.opportunities_found}/{scan_count}")
                if not self.settings.dry_run:
                    logger.info(f"执行的交易: {self.trades_executed}")
                
                logger.info(f"等待 {interval_seconds}秒...\n")
                await asyncio.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("\n" + "=" * 70)
            logger.info("🛑 机器人已被用户停止")
            logger.info(f"总扫描次数: {scan_count}")
            logger.info(f"发现的机会: {self.opportunities_found}")
            if not self.settings.dry_run:
                logger.info(f"执行的交易: {self.trades_executed}")
            logger.info("=" * 70)


async def main(symbol: str):
    """主入口点。"""
    
    # 加载配置
    settings = load_settings()
    
    # 验证配置
    if not settings.private_key:
        logger.error("❌ 错误: .env 中未配置 POLYMARKET_PRIVATE_KEY")
        return
    
    # 创建并运行机器人
    try:
        bot = SimpleArbitrageBot(settings, symbol)
        await bot.monitor(symbol, interval_seconds=0)
    except Exception as e:
        logger.error(f"❌ 致命错误: {e}", exc_info=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python strategy.py <symbol>")
        print("Example: python strategy.py btc")
        sys.exit(1)
    
    symbol = sys.argv[1].lower()
    asyncio.run(main(symbol))
