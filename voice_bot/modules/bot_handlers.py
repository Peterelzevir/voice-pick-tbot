from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    User,
    Message,
    Bot
)
from telegram.error import TelegramError
from telegram.ext import CallbackContext, Application, ContextTypes
from telegram.constants import ChatAction, ParseMode
import time
import os
from modules.bot_utils import (
    validate_text,
    convert_to_voice,
    clear_dir,
    user_restricted,
    log_cmd,
    get_user_voice_dir,
    answer_query,
    remove_temp_file,
    logger,
    MAX_CHARS_NUM,
    RESULTS_PATH
)
from modules.tortoise_api import tts_audio_from_text
from modules.bot_db import db_handle
from modules.bot_settings import get_user_settings, UserSettings, TOGGLE_GEN_INLINE_KEY
from modules.bot_utils import SOURCE_WEB_LINK, QUERY_PATTERN_RETRY, get_text_locale, get_cis_locale_dict
from modules.whisper_api import transcribe_voice, WHISPER_SAMPLE_RATE
import asyncio
from concurrent.futures import Future
from threading import Thread
from asyncio.events import AbstractEventLoop
from librosa import load
from typing import Union
from numpy import ndarray


class TTSWorkThread(Thread):
    """
    Thread class with active event loop to process incoming synthesis requests sequentially
    on separate thread
    """
    def __init__(self):
        Thread.__init__(self, name="tts_worker", daemon=True)  # Doesn't matter if stops unexpectedly
        self.loop = asyncio.new_event_loop()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()


tts_work_thread = TTSWorkThread()


@user_restricted
async def start_cmd(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    db_handle.init_user(user.id)
    reply = get_text_locale(user, get_cis_locale_dict(f"Здравствуйте, {user.mention_html()}! Вызовите /help чтобы узнать как пользоваться ботом"),
                            f"Hi, {user.mention_html()}! Call /help to get info about bot usage")
    await update.message.reply_html(reply)


@user_restricted
async def gen_audio_cmd(update: Update, context: CallbackContext) -> None:
    """Send voice audio file generated by inference"""
    reply_id = update.message.message_id
    user = update.effective_user
    db_handle.init_user(user.id)
    if not context.args:
        reply = get_text_locale(user, get_cis_locale_dict("Ошибка: неверно вызвана команда, предоставьте текст вместе с командой (прим. /gen текст)"),
                                "Error: invalid arguments provided, provide text next to the command")
        await update.message.reply_text(reply, reply_to_message_id=reply_id)
        return

    text = ' '.join(context.args)
    val_res, val_err_msg = validate_text(user, text)
    if not val_res:
        reply = get_text_locale(user, get_cis_locale_dict(f"Ошибка: обнаружен неприемлемый символ в тексте. {val_err_msg}"), f"Error: Invalid text detected. {val_err_msg}")
        await update.message.reply_text(reply, reply_to_message_id=reply_id)
        return

    prog_msg: Message = await create_progress_msg(update, context)
    context.application.create_task(start_gen_task(update, context, text, prog_msg), update=update)


@user_restricted
async def gen_audio_inline(update: Update, context: CallbackContext) -> None:
    """reply to gen audio msg request"""
    inline_toggle = context.user_data.get(TOGGLE_GEN_INLINE_KEY, None)
    reply_id = update.message.message_id
    user = update.effective_user
    db_handle.init_user(user.id)
    if not inline_toggle:
        reply = get_text_locale(user, get_cis_locale_dict("Подсказка: если вы пытаетесь вызвать синтез, пожалуйста включите данный режим командой /toggle_inline"),
                                "Hint: if you trying to start audio synthesis, please enable the inline mode via /toggle_inline")
        await update.message.reply_html(reply, reply_to_message_id=reply_id)
        return

    text = update.message.text
    val_res, val_err_msg = validate_text(user, text)
    if not val_res:
        reply = get_text_locale(user, get_cis_locale_dict(f"Ошибка: обнаружен неприемлемый символ в тексте. {val_err_msg}"), f"Error: Invalid text detected. {val_err_msg}")
        await update.message.reply_text(reply, reply_to_message_id=reply_id)
        return

    prog_msg: Message = await create_progress_msg(update, context)
    context.application.create_task(start_gen_task(update, context, text, prog_msg), update=update)


@user_restricted
async def toggle_inline_cmd(update: Update, context: CallbackContext) -> None:
    """reply to gen audio msg request"""
    reply_id = update.message.message_id
    inline_toggle = context.user_data.get(TOGGLE_GEN_INLINE_KEY, None)
    if inline_toggle is None:
        inline_toggle = context.user_data[TOGGLE_GEN_INLINE_KEY] = True
    else:
        inline_toggle = context.user_data[TOGGLE_GEN_INLINE_KEY] = not inline_toggle

    reply = get_text_locale(update.effective_user, get_cis_locale_dict(f"Режим синтеза через текстовые сообщения {'Включен' if inline_toggle else 'Выключен'}"),
                            f"Inline audio generation mode is {'On' if inline_toggle else 'Off'}")
    await update.message.reply_text(reply, reply_to_message_id=reply_id)


@user_restricted
async def retry_button(update: Update, context: CallbackContext) -> None:
    """launches tts task on a already completed one from the message keyboard"""
    query = update.callback_query
    user = update.effective_user
    db_handle.init_user(user.id)
    context.application.create_task(answer_query(query), update=update)

    prog_msg: Message = await create_progress_msg(update, context)
    # TODO get actual message text instead of caption
    context.application.create_task(start_gen_task(update, context, query.message.caption, prog_msg), update=update)


async def help_cmd(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    log_cmd(user, "help_cmd")
    help_msg = ("Bot usage: select from the menu or type commands to interact with the bot. List of commands:\n\n"
                "<u>/gen</u> - provide text with this command and eventually receive a voice reply with your query,"
                "it may takes some time, depending on text length (from couple of seconds for a short sentence to "
                "couple of minutes for essay-length)\n\n"
                "<u>/add_voice</u> - to start a guided process of adding user voice for cloning, by providing the name and audio samples "
                "via files or voice recording(though voice quality will likely be subpar with bad mic recording)\n\n"
                "<u>/settings</u> - change user specific settings for voice synthesis\n\n"
                "<u>/toggle_inline</u> - toggle audio generation straight from text message\n\n"
                f"Take a look at source code for additional info at <a href='{SOURCE_WEB_LINK}'>GitHub</a>")
    help_msg_ru = ("Использование бота: выбирайте команды из меню или вводите вручную. Доступный список команд:\n\n"
                   "<u>/gen</u> - сопроводите вызов данной команды текстом через пробел, по её завершению будет выслан аудио файл,"
                   " что может занять некоторое время, в зависимости от длины текста (от нескольких секунд до нескольких минут)\n\n"
                   "<u>/add_voice</u> - начинает пошаговый процесс добавления нового голоса для клонирования, после указания имени и аудиоданных "
                   "в виде файлов или голосовой записи (однако качество голоса при последнем варианте может пострадать)\n\n"
                   "<u>/settings</u> - открыть меню настроек аудио-синтеза\n\n"
                   "<u>/toggle_inline</u> - переключить режим синтеза аудио простой отправкой текстого сообщения\n\n"
                   f"Для дополнительной информации обратите внимание на страницу проекта на <a href='{SOURCE_WEB_LINK}'>GitHub</a>")
    reply = get_text_locale(user, get_cis_locale_dict(help_msg_ru), help_msg)
    await update.message.reply_html(reply, disable_web_page_preview=True)


async def gen_audio_from_voice(update: Update, context: CallbackContext) -> None:
    """reply to voice msg"""
    user = update.effective_user
    db_handle.init_user(user.id)

    if update.message.voice:  # validate voice file
        filetype = "ogg"
        file_path = os.path.abspath(os.path.join(RESULTS_PATH, f'{user.id}_{int(time.time())}.{filetype}'))
        try:
            with open(file_path, mode='w'):
                pass
            file = await context.bot.get_file(update.message.voice)
            await file.download_to_drive(file_path)
            audio, _ = load(file_path, sr=WHISPER_SAMPLE_RATE)
        except Exception as e:
            remove_temp_file(file_path)
            raise TelegramError("Audio from voice Error: download failure") from e
        finally:
            remove_temp_file(file_path)
    else:
        raise TelegramError("Audio from voice Error: no voice")

    prog_msg: Message = await create_progress_msg(update, context)
    context.application.create_task(start_gen_task(update, context, audio, prog_msg), update=update)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if update and update.effective_message and update.effective_user:
        reply = get_text_locale(update.effective_user, get_cis_locale_dict(f"{update.effective_user.mention_html()}, к сожалению произошла внутренняя ошибка сервера во время обработки команды, пожалуйста попробуйте еще раз"),
                                f"Sorry {update.effective_user.mention_html()}, there has been a Server Internal Error when processing your command, please try again")
        try:
            await update.effective_message.reply_html(reply, reply_to_message_id=update.effective_message.message_id)
        except TelegramError:  # if reply message was deleted
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply, parse_mode=ParseMode.HTML)


""" ------------------------------TTS related callbacks------------------------------ """


async def start_gen_task(update: Update, context: CallbackContext, data: Union[str, ndarray], progress_msg: Message) -> None:
    """data - represents text, in case of text message handle and audio data, in case of voice mesage handle"""
    user = update.effective_user
    filename_result = os.path.abspath(os.path.join(RESULTS_PATH, '{}_{}.wav'.format(user.id, int(time.time()))))
    settings = get_user_settings(user.id)
    loop = asyncio.get_running_loop()
    future = asyncio.run_coroutine_threadsafe(run_gen_audio(update, context.application, progress_msg, filename_result, settings, data, get_user_voice_dir(user.id), loop), tts_work_thread.loop)
    future.add_done_callback(eval_gen_task)


async def run_gen_audio(update: Update, app: Application, progerss_msg: Message, filename_result: str, settings: UserSettings, data: Union[str, ndarray], user_voices_dir: str, app_loop: AbstractEventLoop) -> None:
    """Running on tts_worker thread"""
    try:
        if isinstance(data, ndarray):  # transcibe voice data
            data = transcribe_voice(data)
            val_res, val_err_msg = validate_text(update.effective_user, data)
            if not val_res:
                raise Exception(f"text validation error: {val_err_msg}")

        tts_audio_from_text(filename_result, data, settings.voice, user_voices_dir, settings.emotion, settings.samples_num)
    except Exception as e:
        async def handle_post_eval_gen_report_error(update: Update, app: Application, progress_msg: Message, exc: Exception) -> None:
            app.create_task(post_eval_gen_report_error(update, progress_msg, exc), update=update)

        asyncio.run_coroutine_threadsafe(handle_post_eval_gen_report_error(update, app, progerss_msg, e), app_loop)

    return update, app, progerss_msg, filename_result, data, settings.samples_num


def eval_gen_task(future: Future) -> None:
    try:
        update, app, progress_msg, filename_result, text, samples_num = future.result()
    except Exception as e:
        logger.error(msg="Exception while handling eval_gen_task:", exc_info=e)
    else:
        app.create_task(post_eval_gen_task(update, app, filename_result, text, samples_num, update.effective_message, progress_msg), update=update)


async def post_eval_gen_report_error(update: Update, progress_msg: Message, exc) -> None:
    """handles errors from tts_worker thread in a main thread"""
    clear_dir(RESULTS_PATH)
    logger.error(msg="Exception while handling run_gen_audio:", exc_info=exc)
    await delete_progress_msg(progress_msg)
    if update and update.effective_message and update.effective_user:
        reply = get_text_locale(update.effective_user, get_cis_locale_dict(f"{update.effective_user.mention_html()}, к сожалению синтез аудио завершился ошибкой, пожалуйста попробуйте еще раз"),
                                f"Sorry {update.effective_user.mention_html()}, your audio generation failed, please try again")
        await update.effective_message.reply_html(reply, reply_to_message_id=update.effective_message.message_id)


async def post_eval_gen_task(update: Update, app: Application, filename_result: str, text: str, samples_num: int, message: Message, progress_msg: Message) -> None:

    try:
        user: User = update.effective_user
        for sample_ind in range(samples_num):
            sample_file = filename_result.replace(".wav", f"_{sample_ind}.wav")
            voice_file = convert_to_voice(sample_file)
            with open(voice_file, 'rb') as audio:
                keyboard = [[InlineKeyboardButton(get_text_locale(user, get_cis_locale_dict("Генерировать снова"), "Regenerate"), callback_data=QUERY_PATTERN_RETRY)]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                if len(text) > MAX_CHARS_NUM:  # elide to prevent hitting max caption size
                    text = f"{text[:MAX_CHARS_NUM]}..."
                await message.reply_voice(voice=audio, caption=text, reply_to_message_id=message.message_id, reply_markup=reply_markup)

            logger.info(f"Audio generation DONE: called by {user.full_name}, for sample №{sample_ind}, with query: {text}")
    except Exception as e:
        app.create_task(delete_progress_msg(progress_msg), update=update)
        clear_dir(RESULTS_PATH)
        raise TelegramError("Audio generation Error") from e
    else:
        app.create_task(delete_progress_msg(progress_msg), update=update)
        clear_dir(RESULTS_PATH)


async def create_progress_msg(update: Update, context: CallbackContext):
    """send chat action and a progress message and return the Message"""
    bot: Bot = context.bot
    chat_id: int = update.effective_chat.id
    wait_emoji_ucode: str = "\U000023F3"
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
    return await bot.send_message(chat_id=chat_id, text=get_text_locale(update.effective_user, get_cis_locale_dict(f"{wait_emoji_ucode}Синтез в процессе...{wait_emoji_ucode}"),
                                  f"{wait_emoji_ucode}Synthesis is in progress...{wait_emoji_ucode}"))


async def delete_progress_msg(msg: Message) -> None:
    try:
        await msg.delete()
    except TelegramError:
        logger.error(msg="Failed to delete progress message")
