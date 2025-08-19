from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import json

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from cardinal import Cardinal
from FunPayAPI.common.enums import OrderStatuses

from threading import Thread

import requests
from FunPayAPI.types import MessageTypes
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent

from pip._internal.cli.main import main
try:
    from pytoniq_core import Cell
    import tonutils.client
    from tonutils.wallet import WalletV4R2
except ImportError:
    main(["install", "-U", "tonutils"])
    main(["install", "-U", "pytoniq"])
    from pytoniq_core import Cell
    import tonutils.client
    import tonutils.wallet

logger = logging.getLogger("FPC.AUTO-STARS")

NAME = "Auto Stars"
VERSION = "0.0.1"
DESCRIPTION = "Автовыдача звёзд.\nРаботает на кошеле типа V4R2.\nГлавное чтобы в описании лота было: <code>#count: 50</code> (50 - пример, сколько звезд надо выдать)"
CREDITS = "@cyka"
UUID = "27c70bd9-adf1-4d33-9eeb-57492e99fa52"
SETTINGS_PAGE = False

FRAGMENT_API_URL = "https://fragment.com/api?hash="
LOGGER_PREFIX = "[AUTO-STARS]"

@dataclass
class Order:
    user_id: int = 0
    chat_id: int = 0
    quantity: int = 0
    order_id: str = ""
    username: str = ""
    recipient: str = ""
    funpay_price: float = 0
    queue_position: int = 0
    confirm_order: bool = False
    

class AutoStarsPlugin:
    
    COOKIES = {
        "stel_ssid": "",
        "stel_dt": "-180",
        "stel_token": "",
        "stel_ton_token": ""
    }

    MNEMONIC = ""
    MNEMONIC = MNEMONIC.split()

    def __init__(self):
        self.orders: Dict[int, Order] = {}
        self.queue = asyncio.Queue()
        self.ton_client = tonutils.client.TonapiClient(api_key="")
        self.wallet = self._initialize_wallet()
        
    async def _process_queue(self, cardinal: Cardinal):
        """Обрабатывает заказы из очереди."""
        while True:
            try:
                # Получаем заказ из очереди с тайм-аутом
                try:
                    order = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue  # Если очереди пустая, просто продолжаем
                
                if not order:
                    continue  # Пропускаем пустые заказы
                
                await asyncio.sleep(20)
                
                print(f"Обработка заказа пользователя {order.username} (ID: {order.user_id})")
                stars = await self.process_star_purchase(order, cardinal)
                
                # Обновляем позиции после обработки заказа
                await self._update_queue_positions(cardinal)

                self.queue.task_done()  # Помечаем задачу как выполненную

            except asyncio.CancelledError:
                print("Обработка очереди остановлена.")
                break
            
            except Exception as e:
                #self._send_error_message(cardinal, order, order.chat_id, order.username, order.quantity)
                print(f"Ошибка обработки заказа: {e}")
                continue
                

    async def _update_queue_positions(self, cardinal: Cardinal) -> None:
        """Обновляет позиции заказов в очереди и уведомляет покупателей."""
        for index, order in enumerate(self.queue._queue):
            if order.queue_position != index + 1:
                order.queue_position = index + 1
                cardinal.send_message(
                    order.user_id,
                    f"📊 Ваш заказ на {order.quantity} звезд теперь на позиции {order.queue_position} в очереди."
                )

    def _initialize_wallet(self) -> tonutils.wallet.WalletV4R2:
        try:
            wallet, _, _, _ = tonutils.wallet.WalletV4R2.from_mnemonic(
                self.ton_client, 
                mnemonic=self.MNEMONIC
            )
            return wallet
        except Exception as e:
            logger.error(f"Wallet initialization failed: {e}")
            return None

    def log(self, message: str) -> None:
        logger.info(f"{LOGGER_PREFIX}: {message}")

    def tg_log(self, cardinal: Cardinal, message: str) -> None:
        try:
            for user in cardinal.telegram.authorized_users:
                cardinal.telegram.bot.send_message(user, message)
        except Exception as e:
            logger.error(f"TG log failed: {e}")

    def info_tg_log(self, cardinal: Cardinal, order: Order, amount: float) -> None:
        try:
            rub = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub")
            rub = rub.json()["the-open-network"]["rub"]

            ton_rub = amount * rub
            payback = order.funpay_price / ton_rub

            message = f"⭐ <b> Инфо </b>\n\n💸 Перевожу {amount} TON или-же {ton_rub} RUB ({order.quantity} звезд)\n\n❤️‍🩹 Окуп: {payback}X ({order.funpay_price - ton_rub} RUB)\n\nЗаказ: {order.order_id}\nUsername: {order.username}"

            self.tg_log(cardinal=cardinal, message=message)
        except Exception as e:
            logger.error(f"TG info log failed: {e}")

    @staticmethod
    def decode_payload(payload: str) -> str:
        try:
            data = payload + '=' * (-len(payload) % 4)
            cell = Cell.one_from_boc(base64.b64decode(data))
            return str(cell.begin_parse().skip_bits(32).load_snake_string())
        except Exception as e:
            logger.error(f"Error decoding payload: {e}")
            return ""

    def _make_api_request(self, data: Dict[str, Any]) -> Optional[Dict]:
        try:
            with requests.Session() as session:
                response = session.post(
                    FRAGMENT_API_URL,
                    data=data,
                    cookies=self.COOKIES,
                    timeout=10
                )
                response.raise_for_status()
                logger.info(f"make api req - {response.json()}")
                return response.json()
        except Exception as e:
            logger.error(f"API request failed: {e}")
            return None

    def get_user(self, username: str) -> Optional[str]:
        try:
            response = self._make_api_request({
                "query": username,
                "quantity": "",
                "method": "searchStarsRecipient"
            })
            logger.info(f"user - {response}")
            return response.get("found", {}).get("recipient") if response else None
        except Exception as e:
            logger.error(f"User search failed: {e}")
            return None

    def init_buy_stars(self, recipient: str, quantity: int) -> Optional[Dict]:
        try:
            return self._make_api_request({
                "recipient": recipient,
                "quantity": quantity,
                "method": "initBuyStarsRequest"
            })
        except Exception as e:
            logger.error(f"Init buy stars failed: {e}")
            return None

    def get_buy_stars_link(self, req_id: str, show_sender: int = 0) -> Optional[Dict]:
        try:
            return self._make_api_request({
                "transaction": 1,
                "id": req_id,
                "show_sender": show_sender, #1 - ваш акк тг показывается, 0 - нет.
                "method": "getBuyStarsLink"
            })
        except Exception as e:
            logger.error(f"Get buy stars link failed: {e}")
            return None

    async def _transfer_funds(self, address: str, amount: float, comment: str) -> Optional[str]:
        try:
            return await self.wallet.transfer(
                destination=address,
                amount=amount,
                body=comment
            )
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
            return None

    def _run_async_transfer(self, address: str, amount: float, comment: str) -> Optional[str]:
        loop = asyncio.new_event_loop()
        try:
            time.sleep(2)
            return loop.run_until_complete(self._transfer_funds(address, amount, comment))
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
        finally:
            loop.close()

    async def process_star_purchase(self, order: Order, cardinal: 'Cardinal') -> None:
        try:
            init_data = self.init_buy_stars(order.recipient, order.quantity)
            logger.info(f"init_data - {init_data}")
            
            try:
                if not init_data["req_id"]:
                    logger.error(f"req_id is None")
                    return self._send_error_message(cardinal, order, order.chat_id, order.username, order.quantity)
            except Exception as e:
                logger.error(f"req_id failed: {e}")
                return self._send_error_message(cardinal, order, order.chat_id, order.username, order.quantity)               

            link_data = self.get_buy_stars_link(init_data["req_id"])
            logger.info(f"link_data - {link_data}")
            try:
                if not link_data["transaction"]["messages"][0]:
                    logger.error(f"trans_msg failed")
                    return self._send_error_message(cardinal, order, order.chat_id, order.username, order.quantity)
            except Exception as e:
                logger.error(f"trans_msg failed: {e}")
                return self._send_error_message(cardinal, order, order.chat_id, order.username, order.quantity)                

            transaction = link_data["transaction"]["messages"][0]
            address = transaction["address"]
            amount = float(transaction["amount"]) / 10**9
            comment = self.decode_payload(transaction["payload"])

            self.info_tg_log(cardinal=cardinal, order=order, amount=amount)
            tx_hash = await self._transfer_funds(address, amount, comment)
            
            if tx_hash:
                cardinal.send_message(
                    order.chat_id,
                    f"✅ {order.quantity} звёзд успешно отправлены на аккаунт {order.username} и поступят в течении 2-3 Минут ! Не забудьте подтвердить заказ и оставить свой отзыв ✈️"
                )
                self.orders.pop(order.user_id, None)
            else:
                logger.error(f"not tx_hash")
                self._send_error_message(cardinal, order, order.chat_id, order.username, order.quantity)
        except Exception as e:
            logger.error(f"Star purchase failed: {e}")

    def _send_error_message(self, cardinal: 'Cardinal', order: Order, chat_id: int, username: str, quantity: int) -> None:
        cardinal.send_message(
            chat_id,
            f"Отправка {quantity} звезд {username} не удалась, попробуйте еще раз."
        )
        self.orders[order.user_id].confirm_order = False
        order.recipient = ""
        order.username = ""

    @staticmethod
    def find_count(description: str) -> Optional[int]:
        match = re.search(r"#count: (\d+)", description)
        return int(match.group(1)) if match else None

    def handle_message(self, cardinal: 'Cardinal', event: NewMessageEvent) -> None:
        if (event.message.author_id == cardinal.account.id or 
            event.message.type != MessageTypes.NON_SYSTEM):
            if event.message.type == MessageTypes.REFUND:
                self.orders.pop(event.message.author_id, None)
            return

        order = self.orders.get(event.message.author_id)
        if not order:
            return
        
        if self.orders[order.user_id].confirm_order:
            return

        if not order.username or not order.recipient:
            username = self._extract_username(event.message.text)
            if username:
                order.username = username
                recipient = self.get_user(username)
                if recipient:
                    order.recipient = recipient
                    
                    order = self.orders.get(event.message.author_id)
                    
                    cardinal.send_message(
                        event.message.chat_id,
                        f"⭐ Подтвердите, что хотите получить {order.quantity} Telegram Stars 🌟 на аккаунт {order.username}\nЕсли всё верно, отправьте в чат слово 💬 ⏩ \"да\" ⏪ или '+' без кавычек. Либо отправьте \"нет\" или '-' и пришлите новое имя пользователя Telegram."
                    )
                else:
                    get_order = cardinal.account.get_order(order.order_id)
                    logger.info(f"status - {get_order.status}")
                    if str(get_order.status) in ["OrderStatuses.CLOSED", "OrderStatuses.REFUNDED"]:
                        self.orders[order.user_id].confirm_order = True
                        return
                    
                    order.username = ""
                    cardinal.send_message(
                        event.message.chat_id,
                        f"❌ Указанный Вами аккаунт не найден. Попробуйте ещё раз (пример @funpay)"
                    )
                    
        elif event.message.text.lower() in ["да", "+"]:
            if self.orders[order.user_id].confirm_order:
                return
            
            order = self.orders.get(event.message.author_id)
            get_order = cardinal.account.get_order(order.order_id)

            logger.info(f"status - {get_order.status}")
            if get_order.status in [OrderStatuses.CLOSED, OrderStatuses.REFUNDED]:
                self.orders[order.user_id].confirm_order = True
                return
            
            self.orders[order.user_id].confirm_order = True
            
            self.queue.put_nowait(order)
            self.orders[order.user_id].queue_position = self.queue.qsize() + 1
            
            cardinal.send_message(
                order.chat_id,
                f"📊 Ваш заказ на {order.quantity} звезд добавлен в очередь. Ваша позиция: {order.queue_position}."
            )

        elif event.message.text.lower() in ["нет", "-"]:
            cardinal.send_message(
                event.message.chat_id,
                f"⭐ Отправка {order.quantity} Telegram Stars 🌟 на аккаунт {order.username} отменена, отправьте новый юзернейм (например @funpay):"
            )
            order.recipient = ""
            order.username = ""

    @staticmethod
    def _extract_username(text: str) -> Optional[str]:
        match = re.search(r"@[\w_]+", text)
        return match.group(0) if match else None

    def handle_order(self, cardinal: 'Cardinal', event: NewOrderEvent) -> None:
        order = cardinal.account.get_order(event.order.id)
        count = self.find_count(order.full_description)
        
        if not count:
            return

        self.orders[order.buyer_id] = Order(
            user_id=order.buyer_id,
            chat_id=order.chat_id,
            order_id=str(event.order.id),
            quantity=count * event.order.amount,
            funpay_price=event.order.price,
        )

        order_data = self.orders.get(order.buyer_id)
        order_data.username = order.telegram_username
        recipient = self.get_user(order.telegram_username)

        if recipient:
            order_data.recipient = recipient

            cardinal.send_message(
                order.chat_id,
                f"⭐ Подтвердите, что хотите получить {order_data.quantity} Telegram Stars 🌟 на аккаунт {order.telegram_username}\nЕсли всё верно, отправьте в чат слово 💬 ⏩ \"да\" ⏪ или '+' без кавычек. Либо отправьте \"нет\" или '-' и пришлите новое имя пользователя Telegram."
            )
        else:
            if str(order_data.status) in ["OrderStatuses.CLOSED", "OrderStatuses.REFUNDED"]:
                self.orders[order.buyer_id].confirm_order = True
                return
            
            order.username = ""
            cardinal.send_message(
                order.chat_id,
                f"❌ Указанный Вами аккаунт не найден. Попробуйте ещё раз (пример @funpay)"
            )

plugin = AutoStarsPlugin()

def run_async(target, *args):
    asyncio.run(target(*args))

def _init(cardinal: Cardinal) -> None:
    try:
        logging.basicConfig(filename='лог.txt', level=logging.INFO, format='%(asctime)s - %(message)s')
        Thread(target=run_async, args=(plugin._process_queue, cardinal), daemon=True).start()
        plugin.log("Плагин успешно запущен.")
        plugin.get_user("@cyka")
    except Exception as e:
        logger.error(f"Error initializing plugin: {e}")
        
def message_hook(cardinal: Cardinal, event: NewMessageEvent) -> None:
    plugin.handle_message(cardinal, event)

def order_hook(cardinal: Cardinal, event: NewOrderEvent) -> None:
    plugin.handle_order(cardinal, event)

BIND_TO_PRE_INIT = [_init]
BIND_TO_NEW_ORDER = [order_hook]
BIND_TO_NEW_MESSAGE = [message_hook]
BIND_TO_DELETE = None
