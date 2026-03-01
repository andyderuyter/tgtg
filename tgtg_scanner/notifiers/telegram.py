from __future__ import annotations

import asyncio
import datetime
import logging
import random
import warnings
from functools import wraps
from queue import Empty
from time import sleep

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    InvalidToken,
    NetworkError,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown
from telegram.warnings import PTBUserWarning

from tgtg_scanner.errors import MaskConfigurationError, TelegramConfigurationError
from tgtg_scanner.models import Config, Favorites, Item, Reservations
from tgtg_scanner.models.favorites import AddFavoriteRequest, RemoveFavoriteRequest
from tgtg_scanner.models.reservations import Order, Reservation
from tgtg_scanner.notifiers.base import Notifier

log = logging.getLogger("tgtg")


def _private(func):
    @wraps(func)
    async def wrapper(self: Telegram, update: Update, context: CallbackContext) -> None:
        if not self._is_my_chat(update):
            log.warning(
                f"Unauthorized access to {func.__name__} from chat id {update.message.chat.id} "
                f"and user id {update.message.from_user.id}"
            )
            return
        return await func(self, update, context)

    return wrapper


class Telegram(Notifier):
    """Notifier for Telegram."""

    MAX_RETRIES = 10
    MAX_BUTTON_TEXT_LENGTH = 50

    def __init__(self, config: Config, reservations: Reservations, favorites: Favorites):
        super().__init__(config, reservations, favorites)
        self.application: Application = None
        self.config = config
        self.enabled = config.telegram.enabled
        self.token = config.telegram.token
        self.body = config.telegram.body
        self.image = config.telegram.image
        self.chat_ids = config.telegram.chat_ids
        self.timeout = config.telegram.timeout
        self.disable_commands = config.telegram.disable_commands
        self.only_reservations = config.telegram.only_reservations
        self.cron = config.telegram.cron
        self.mute: datetime.datetime | None = None
        self.retries = 0
        if self.enabled:
            if not self.token or not self.body:
                raise TelegramConfigurationError()
            if self.image not in [
                None,
                "",
                "${{item_logo_bytes}}",
                "${{item_cover_bytes}}",
            ]:
                raise TelegramConfigurationError()
            # Suppress Telegram Warnings
            warnings.filterwarnings("ignore", category=PTBUserWarning, module="telegram")
            try:
                Item.check_mask(self.body)
            except MaskConfigurationError as err:
                raise TelegramConfigurationError(err.message) from err
            try:
                # Setting event loop explicitly for python 3.9 compatibility
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                application = ApplicationBuilder().token(self.token).arbitrary_callback_data(True).build()
                application.add_error_handler(self._error)
                asyncio.run(application.bot.get_me())
            except InvalidToken as err:
                raise TelegramConfigurationError("Invalid Telegram Bot Token") from err
            except TelegramError as err:
                raise TelegramConfigurationError(err.message) from err

    @property
    def _handlers(self):
        return [
            CommandHandler("mute", self._mute),
            CommandHandler("unmute", self._unmute),
            CommandHandler("reserve", self._reserve_item_menu),
            CommandHandler("reserveall", self._reserve_all_items),
            CommandHandler("reservations", self._cancel_reservations_menu),
            CommandHandler("orders", self._cancel_orders_menu),
            CommandHandler("cancelallreservations", self._cancel_all_reservations),
            CommandHandler("cancelallorders", self._cancel_all_orders),
            CommandHandler("cancelall", self._cancel_all),
            CommandHandler("listfavorites", self._list_favorites),
            CommandHandler("listfavoriteids", self._list_favorite_ids),
            CommandHandler("addfavorites", self._add_favorites),
            CommandHandler("removefavorites", self._remove_favorites),
            CommandHandler("offers", self._offers_overview),
            CommandHandler("getid", self._get_id),
            MessageHandler(
                filters.Regex(r"^https:\/\/share\.toogoodtogo\.com\/item\/(\d+)\/?"),
                self._url_handler,
            ),
            CallbackQueryHandler(self._callback_query_handler),
        ]

    async def _start_polling(self):
        log.debug("Telegram: Starting polling")
        for handler in self._handlers:
            self.application.add_handler(handler)
        await self.application.initialize()
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES, timeout=self.timeout, poll_interval=0.1)
        await self.application.bot.delete_my_commands()
        await self.application.bot.set_my_commands(
            [
                # BotCommand("mute", "Deactivate Telegram Notifications for 1 or X days"),
                # BotCommand("unmute", "Reactivate Telegram Notifications"),
                BotCommand("reserve", "Reserve the next available Magic Bag"),
                # BotCommand("reserveall", "Create Reservations for all Favorites"),
                BotCommand("reservations", "List and cancel active Reservations"),
                BotCommand("orders", "List and cancel active Orders"),
                BotCommand("cancelallreservations", "Cancel all active Reservations"),
                BotCommand("cancelallorders", "Cancel all active Orders"),
                BotCommand("cancelall", "Cancel all active Reservations and Orders"),
                # BotCommand("listfavorites", "List all Favorites"),
                # BotCommand("listfavoriteids", "List all Item IDs from Favorites"),
                # BotCommand("addfavorites", "Add Item IDs to Favorites"),
                # BotCommand("removefavorites", "Remove Item IDs from Favorites"),
                BotCommand("offers", "Show current favorite offers"),
                BotCommand("getid", "Get your Chat ID"),
            ]
        )
        await self.application.start()

    async def _stop_polling(self):
        log.debug("Telegram: stopping polling")
        await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()

    def start(self) -> None:
        if self.enabled and not self.chat_ids:
            asyncio.run(self._get_chat_id())
        super().start()

    def _run(self) -> None:
        async def _listen_for_items() -> None:
            # Setting event loop explicitly for python 3.9 compatibility
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.application = ApplicationBuilder().token(self.token).arbitrary_callback_data(True).build()
            self.application.add_error_handler(self._error)
            await self.application.bot.set_my_commands([])
            if not self.disable_commands:
                try:
                    await self._start_polling()
                except Exception as exc:
                    log.error("Telegram failed starting polling: %s", exc)
                    return
            while True:
                try:
                    item = self.queue.get(block=False)
                    if item is None:
                        break
                    log.debug("Sending %s Notification", self.name)
                    await self._send(item)
                except Empty:
                    pass
                except Exception as exc:
                    log.error("Failed sending %s: %s", self.name, exc)
                finally:
                    await asyncio.sleep(0.1)
            if not self.disable_commands:
                try:
                    await self._stop_polling()
                except Exception as exc:
                    log.warning("Telegram failed stopping polling: %s", exc)

        self.config.set_locale()
        asyncio.run(_listen_for_items())

    def _unmask(self, text: str, item: Item) -> str:
        for match in item._get_variables(text):
            if hasattr(item, match.group(1)):
                val = str(getattr(item, match.group(1)))
                val = escape_markdown(val, version=2)
                text = text.replace(match.group(0), val)
        return text

    def _unmask_image(self, text: str, item: Item) -> bytes | None:
        if text in ["${{item_logo_bytes}}", "${{item_cover_bytes}}"]:
            matches = item._get_variables(text)
            return bytes(getattr(item, matches[0].group(1)))
        return None

    async def _send(self, item: Item | Reservation) -> None:  # type: ignore[override]
        """Send item information as Telegram message.

        Reservation notifications are always send.
        Disable Item notification with mute or only_reservations config.
        """
        if self.mute and self.mute < datetime.datetime.now():
            log.info("Reactivated Telegram Notifications")
            self.mute = None
        image = None
        if isinstance(item, Item) and not self.only_reservations and not self.mute:
            message = self._unmask(self.body, item)
            if self.image:
                image = self._unmask_image(self.image, item)
        elif isinstance(item, Reservation):
            full_item = self.favorites.get_item_by_id(item.item_id)
            item_url = str(full_item.link) if full_item and full_item.link else "https://share.toogoodtogo.com/"

            message = escape_markdown(
                (
                    f"{item.display_name} ({item.amount} pakketten) zijn gereserveerd voor 5 minuten"
                    if item.amount > 1
                    else f"{item.display_name} (1 pakket) is gereserveerd voor 5 minuten"
                ),
                version=2,
            )
        else:
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👉 Open pakket in TGTG app", url=item_url)],
            [InlineKeyboardButton("📦 Open orders om te verwijderen", callback_data="cmd:orders")]
        ])
        await self._send_message(message, image, reply_markup=keyboard)
        log.info("Sent Telegram order notification for %s", item.display_name)

    async def _send_message(self, message: str, image: bytes | None = None, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        log.debug("%s message: %s", self.name, message)
        fmt = ParseMode.MARKDOWN_V2
        for chat_id in self.chat_ids:
            try:
                if image:
                    await self.application.bot.send_photo(
                        chat_id=chat_id, photo=image, caption=message,
                        parse_mode=fmt, reply_markup=reply_markup
                    )
                else:
                    await self.application.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode=fmt,
                        disable_web_page_preview=True,
                        reply_markup=reply_markup,
                    )
                self.retries = 0
            except BadRequest as err:
                err_message = err.message
                if err_message.startswith("Can't parse entities:"):
                    err_message += ". For details see https://github.com/Der-Henning/tgtg/wiki/Configuration#note-on-markdown-v2"
                log.error("Telegram Error: %s", err_message)
            except (NetworkError, TimedOut) as err:
                log.warning("Telegram Error: %s", err)
                self.retries += 1
                if self.retries > Telegram.MAX_RETRIES:
                    raise err
                await self._send_message(message)
            except TelegramError as err:
                log.error("Telegram Error: %s", err)

    def _is_my_chat(self, update: Update) -> bool:
        return str(update.message.chat.id) in self.chat_ids

    async def _get_id(self, update: Update, _) -> None:
        await update.message.reply_text(f"Current Chat ID: {update.message.chat.id}")

    @_private
    async def _mute(self, update: Update, context: CallbackContext) -> None:
        """Deactivates Telegram Notifications for x days."""
        days = int(context.args[0]) if context.args and context.args[0].isnumeric() else 1
        self.mute = datetime.datetime.now() + datetime.timedelta(days=days)
        log.info("Deactivated Telegram Notifications for %s days", days)
        log.info("Reactivation at %s", self.mute)
        await update.message.reply_text(
            f"Deactivated Telegram Notifications for {days} days.\nReactivating at {self.mute} or use /unmute"
        )

    @_private
    async def _unmute(self, update: Update, _) -> None:
        """Reactivate Telegram Notifications."""
        self.mute = None
        log.info("Reactivated Telegram Notifications")
        await update.message.reply_text("Reactivated Telegram Notifications")

    @_private
    async def _reserve_item_menu(self, update: Update, _) -> None:
        favorites = self.favorites.get_favorites()
        buttons = [
            [
                InlineKeyboardButton(
                    Telegram._shorten_with_ellipsis(f"{item.display_name}: {item.items_available}"), callback_data=item
                )
            ]
            for item in favorites
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text("Selecteer een pakket om te reserveren:", reply_markup=reply_markup)

    @_private
    async def _reserve_all_items(self, update: Update, _) -> None:
        favorites = self.favorites.get_favorites()
        for item in favorites:
            self.reservations.reserve(item.item_id, item.display_name)
        await update.message.reply_text("Created Reservations for all Favorites")

    @_private
    async def _cancel_reservations_menu(self, update: Update, _) -> None:
        buttons = [
            [
                InlineKeyboardButton(
                    Telegram._shorten_with_ellipsis(
                        f"{reservation.amount}x {reservation.display_name}"
                    ),
                    callback_data=reservation,
                )
            ]
            for reservation in self.reservations.reservation_query
        ]
        if len(buttons) == 0:
            await update.message.reply_text("Geen actieve reservaties.")
            return
        reply_markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text("Actieve reservaties. Klik 1x om te annuleren.", reply_markup=reply_markup)

    @_private
    async def _cancel_orders_menu(self, update: Update, _) -> None:
        self.reservations.update_active_orders()
        buttons = [
            [
                InlineKeyboardButton(
                    Telegram._shorten_with_ellipsis(
                        f"{order.amount}x {order.display_name}"
                    ),
                    callback_data=order,
                )
            ]
            for order in self.reservations.active_orders.values()
        ]
        if len(buttons) == 0:
            await update.message.reply_text("Geen actieve orders.")
            return
        reply_markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text("Actieve orders. Klik 1x om te annuleren.", reply_markup=reply_markup)

    @_private
    async def _cancel_all_reservations(self, update: Update, _) -> None:
        self.reservations.cancel_all_reservations()
        await update.message.reply_text("Cancelled all active Reservations")
        log.info("Cancelled all active Reservations")

    @_private
    async def _cancel_all_orders(self, update: Update, _) -> None:
        self.reservations.cancel_all_orders()
        await update.message.reply_text("Cancelled all active Orders")
        log.info("Cancelled all active Orders")

    @_private
    async def _cancel_all(self, update: Update, _) -> None:
        self.reservations.cancel_all_reservations()
        self.reservations.cancel_all_orders()
        await update.message.reply_text("Cancelled all active Reservations and Orders")
        log.info("Cancelled all active Reservations and Orders")

    @_private
    async def _list_favorites(self, update: Update, _) -> None:
        favorites = self.favorites.get_favorites()
        if not favorites:
            await update.message.reply_text("You currently don't have any Favorites")
        else:
            await update.message.reply_text("\n".join([f"• {item.item_id} - {item.display_name}" for item in favorites]))

    @_private
    async def _list_favorite_ids(self, update: Update, _) -> None:
        favorites = self.favorites.get_favorites()
        if not favorites:
            await update.message.reply_text("You currently don't have any Favorites")
        else:
            await update.message.reply_text(" ".join([item.item_id for item in favorites]))

    @_private
    async def _add_favorites(self, update: Update, context: CallbackContext) -> None:
        if not context.args:
            await update.message.reply_text(
                "Please supply Item IDs in one of the following ways: "
                "'/addfavorites 12345 23456 34567' or "
                "'/addfavorites 12345,23456,34567'"
            )
            return

        item_ids = list(
            filter(
                bool,
                map(
                    str.strip,
                    [split_args for arg in context.args for split_args in arg.split(",")],
                ),
            )
        )
        self.favorites.add_favorites(item_ids)
        await update.message.reply_text(f"Added the following Item IDs to Favorites: {' '.join(item_ids)}")
        log.debug('Added the following item ids to favorites: "%s"', item_ids)

    @_private
    async def _remove_favorites(self, update: Update, context: CallbackContext) -> None:
        if not context.args:
            await update.message.reply_text(
                "Please supply Item IDs in one of the following ways: "
                "'/removefavorites 12345 23456 34567' or "
                "'/removefavorites 12345,23456,34567'"
            )
            return

        item_ids = list(
            filter(
                bool,
                map(
                    str.strip,
                    [split_args for arg in context.args for split_args in arg.split(",")],
                ),
            )
        )
        self.favorites.remove_favorite(item_ids)
        await update.message.reply_text(f"Removed the following Item IDs from Favorites: {' '.join(item_ids)}")
        log.debug("Removed the following Item IDs from Favorites: '%s'", item_ids)

    @_private
    async def _offers_overview(self, update: Update, _) -> None:
        """Sends each available offer as an individual message with a button."""
        favorites = self.favorites.get_favorites()
        available_items = [item for item in favorites if item.items_available > 0]
        
        if not favorites:
            await update.message.reply_text("You don't have any favorites saved\.")
            return

        if not available_items:
            await update.message.reply_text("Er zijn momenteel geen beschikbare pakketten in je favorieten\.")
            return

        # Header message to start the overview
        await update.message.reply_text("*Beschikbare favoriete pakketten*:", parse_mode=ParseMode.MARKDOWN_V2)

        for item in available_items:
            # 1. Format and Escape
            name = escape_markdown(str(item.display_name), version=2)
            stock = item.items_available
            price = escape_markdown(f"{item.price} ({item.value})", version=2)
            pickup = escape_markdown(str(item.pickupdate) if item.pickupdate else "Not available", version=2)
            item_url = str(item.link)

            # 2. Build Message String
            message = (
                f"🛍️ *{name}*\n"
                f" ├ Aantal: {stock}\n"
                f" ├ Prijs: {price}\n"
                f" └ Ophalen: {pickup}"
            )

            # 3. Create the Keyboard
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(text="👉 Open pakket in TGTG app", url=item_url)]
            ])

            # 4. Send immediately inside the loop
            await update.message.reply_text(
                message, 
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard
            )

    @_private
    async def _url_handler(self, update: Update, context: CallbackContext) -> None:
        item_id = context.matches[0].group(1)
        item_favorite = self.favorites.is_item_favorite(item_id)
        item = self.favorites.get_item_by_id(item_id)
        if item.item_id is None:
            await update.message.reply_text("There is no Item with this link")
            return

        if item_favorite:
            await update.message.reply_text(
                f"{item.display_name} is in your Favorites. Do you want to remove it?",
                reply_markup=(
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Yes",
                                    callback_data=RemoveFavoriteRequest(item_id, item.display_name, True),
                                ),
                                InlineKeyboardButton(
                                    "No",
                                    callback_data=RemoveFavoriteRequest(item_id, item.display_name, False),
                                ),
                            ]
                        ]
                    )
                ),
            )
        else:
            await update.message.reply_text(
                f"{item.display_name} is not in your Favorites. Do you want to add it?",
                reply_markup=(
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Yes",
                                    callback_data=AddFavoriteRequest(item_id, item.display_name, True),
                                ),
                                InlineKeyboardButton(
                                    "No",
                                    callback_data=AddFavoriteRequest(item_id, item.display_name, False),
                                ),
                            ]
                        ]
                    )
                ),
            )

    async def _callback_query_handler(self, update: Update, _) -> None:
        data = update.callback_query.data
        # Handle command shortcut buttons
        if data == "cmd:orders":
            await update.callback_query.answer()
            self.reservations.update_active_orders()
            buttons = [
                [InlineKeyboardButton(
                    Telegram._shorten_with_ellipsis(
                        f"{order.amount}x {order.display_name}"
                    ),
                    callback_data=order,
                )]
                for order in self.reservations.active_orders.values()
            ]
            if not buttons:
                await update.callback_query.message.reply_text("Geen actieve orders.")
            else:
                await update.callback_query.message.reply_text(
                    "Actieve orders. Klik 1x om te annuleren.",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            return
        if isinstance(data, Item):
            self.reservations.reserve(data.item_id, data.display_name)
            await update.callback_query.answer(f"{data.display_name} toegevoegd aan reservatie wachtrij")
            log.info('Added "%s" to reservation queue', data.display_name)
        if isinstance(data, Reservation):
            self.reservations.reservation_query.remove(data)
            await update.callback_query.answer(f"{data.display_name} verwijderd uit reservatie wachtrij")
            log.info('Removed "%s" from reservation queue', data.display_name)
        if isinstance(data, Order):
            self.reservations.cancel_order(data.id)
            await update.callback_query.answer(f"Order geannuleerd voor {data.display_name}")
            log.info('Canceled order for "%s"', data.display_name)
        if isinstance(data, AddFavoriteRequest):
            if data.proceed:
                self.favorites.add_favorites([data.item_id])
                await update.callback_query.edit_message_text(f"Toegevoegd {data.item_display_name} aan favorieten")
                log.debug('Added "%s" to favorites', data.item_display_name)
                log.debug('Removed "%s" from favorites', data.item_display_name)
            else:
                await update.callback_query.delete_message()
        if isinstance(data, RemoveFavoriteRequest):
            if data.proceed:
                self.favorites.remove_favorite([data.item_id])
                await update.callback_query.edit_message_text(f"Removed {data.item_display_name} from Favorites")
                log.debug('Removed "%s" from favorites', data.item_display_name)
            else:
                await update.callback_query.delete_message()

    async def _error(self, update: Update, context: CallbackContext) -> None:
        """Log Errors caused by Updates."""
        log.warning('Update "%s" caused error "%s"', update, context.error)

    async def _get_chat_id(self) -> None:
        r"""Initializes an interaction with the user
        to obtain the telegram chat id. \n
        On using the config.ini configuration the
        chat id will be stored in the config.ini.
        """
        log.warning("You enabled the Telegram notifications without providing a chat id!")
        code = random.randint(1111, 9999)
        log.warning("Send %s to the bot in your desired chat.", code)
        log.warning("Waiting for code ...")
        application = ApplicationBuilder().token(self.token).arbitrary_callback_data(True).build()
        application.add_error_handler(self._error)
        while not self.chat_ids:
            updates = await application.bot.get_updates(timeout=self.timeout)
            for update in reversed(updates):
                if update.message and update.message.text:
                    if update.message.text.isdecimal() and int(update.message.text) == code:
                        log.warning(
                            "Received code from %s %s on chat id %s",
                            update.message.from_user.first_name,
                            update.message.from_user.last_name,
                            update.message.chat_id,
                        )
                        self.chat_ids = [str(update.message.chat_id)]
            sleep(1)
        if self.config.set("TELEGRAM", "ChatIDs", ",".join(self.chat_ids)):
            log.warning("Saved chat id in your config file")
        else:
            log.warning(
                "For persistence please set TELEGRAM_CHAT_IDS=%s",
                ",".join(self.chat_ids),
            )

    def __repr__(self) -> str:
        return f"Telegram: {self.chat_ids}"

    @staticmethod
    def _shorten_with_ellipsis(text: str, length: int = MAX_BUTTON_TEXT_LENGTH) -> str:
        """Shorten text to length and add ellipsis in the middle"""
        if len(text) <= length:
            return text
        else:
            slice_size = (length - 3) // 2
            return text[:slice_size] + "..." + text[-slice_size:]
