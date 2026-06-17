import os
import re
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.error import RetryAfter, TelegramError

# ---------------------------------------------------------------------------
# الإعدادات العامة
# ---------------------------------------------------------------------------
TOKEN = os.environ["BOT_TOKEN"]


def _parse_ids(*env_names):
    """يقرأ آيديات أدمن من متغيرات بيئة (يمكن وضع أكثر من آيدي مفصولين بفاصلة)."""
    ids = set()
    for name in env_names:
        raw = os.environ.get(name, "")
        for part in raw.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
    return ids


# دعم أكثر من أدمن:
#  - ADMIN_ID / ADMIN_IDS: متغيرات بيئة على Railway (يمكن وضع أكثر من آيدي
#    مفصولين بفاصلة في ADMIN_IDS لإضافة أشخاص جدد لاحقاً بدون تعديل الكود)
#  - 8693892771: تمت إضافته مباشرة حسب الطلب، يبقى مفعّلاً دائماً
ADMIN_IDS = _parse_ids("ADMIN_ID", "ADMIN_IDS") | {8693892771}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


CHANNEL_LABEL = "𝐏𝐬𝐞𝐮𝐝𝐨𝐬𝐜𝐢𝐞𝐧𝐜𝐞"
MAIN_CHANNEL  = os.environ.get("MAIN_CHANNEL", "@mj515678")
SEND_DELAY    = float(os.environ.get("SEND_DELAY", "4"))    # تأخير بين كل سؤال والتالي
BUFFER_DELAY  = float(os.environ.get("BUFFER_DELAY", "2"))  # مهلة تجميع الرسائل المقسومة تلقائياً
MAX_Q         = 300
MAX_OPT       = 100
TG_MSG_LIMIT  = 4096  # الحد الأقصى لطول رسالة تيليجرام الواحدة

DESTINATIONS = {
    "قناتي الرئيسية" : MAIN_CHANNEL,
    "نفس المحادثة"   : "SAME_CHAT",
}


# ---------------------------------------------------------------------------
# استخراج الأسئلة من النص (بدون أي تغيير في منطق الاستخراج نفسه)
# ---------------------------------------------------------------------------
def extract_questions(text):
    questions   = []
    cur_q       = []
    cur_opts    = []
    cur_ans     = None
    cur_opt_idx = None

    Q_PAT   = re.compile(r'^(?:Q(?:uestion)?\s*)?\d+\s*[.):-]', re.I)
    OPT_PAT = re.compile(r'^([A-F])\s*[.):-]', re.I)
    ANS_PAT = re.compile(r'^(?:Answer|Correct\s*Answer|Ans)\s*[:=\-]\s*([A-F])\b', re.I)
    # خيارات في نفس السطر: A. نص. B. نص. C. نص.
    INLINE_OPT = re.compile(r'([A-F])\.\s*(.*?)(?=\s+[A-F]\.|$)', re.I)

    def flush():
        if (cur_q and cur_ans is not None
                and 2 <= len(cur_opts) <= 6
                and cur_ans < len(cur_opts)):
            questions.append({
                "question": " ".join(cur_q).strip(),
                "options" : [o.strip() for o in cur_opts],
                "correct" : cur_ans,
            })

    def parse_inline_options(line):
        matches = INLINE_OPT.findall(line)
        if len(matches) >= 2:
            return [m[1].strip().rstrip('.') for m in matches]
        return []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if Q_PAT.match(line):
            flush()
            cur_q       = [Q_PAT.sub("", line).strip()]
            cur_opts    = []
            cur_ans     = None
            cur_opt_idx = None

        elif ANS_PAT.match(line):
            m = ANS_PAT.match(line)
            cur_ans = ord(m.group(1).upper()) - ord('A')

        elif OPT_PAT.match(line):
            inline = parse_inline_options(line)
            if inline:
                cur_opts    = inline
                cur_opt_idx = len(cur_opts) - 1
            else:
                cur_opts.append(OPT_PAT.sub("", line).strip())
                cur_opt_idx = len(cur_opts) - 1

        elif cur_opt_idx is not None and not ANS_PAT.match(line):
            inline = parse_inline_options(line)
            if inline:
                cur_opts    = inline
                cur_opt_idx = len(cur_opts) - 1
            else:
                cur_opts[cur_opt_idx] += " " + line

        elif cur_q:
            cur_q.append(line)

    flush()
    return questions


# ---------------------------------------------------------------------------
# تجميع الرسائل المقسومة تلقائياً من تيليجرام (حل مشكلة الرسائل الطويلة)
# ---------------------------------------------------------------------------
def _merge_chunks(chunks):
    """يلصق أجزاء النص المرسلة على دفعات بأمان:
    إذا كان طول جزء سابق يلامس الحد الأقصى لرسالة تيليجرام (4096 حرف)، فهذا
    يعني أن تيليجرام قسمها قسراً في منتصف الكلام، فنلصق التالي مباشرة بدون
    فاصل. غير ذلك تُعتبر الأجزاء أسطر/رسائل منفصلة فنضع بينها سطر جديد."""
    merged = ""
    for i, chunk in enumerate(chunks):
        if i == 0:
            merged = chunk
            continue
        prev_was_split = len(chunks[i - 1]) >= TG_MSG_LIMIT
        merged += chunk if prev_was_split else "\n" + chunk
    return merged


async def send_polls(bot, chat_id, questions, progress_msg=None):
    failed  = []
    success = 0
    total   = len(questions)
    for i, q in enumerate(questions, 1):
        opts = [o[:MAX_OPT] for o in q["options"]] + [CHANNEL_LABEL]
        while True:
            try:
                await bot.send_poll(
                    chat_id           = chat_id,
                    question          = q["question"][:MAX_Q],
                    options           = opts,
                    type              = "quiz",
                    correct_option_id = q["correct"],
                    is_anonymous      = True,
                )
                success += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError:
                failed.append(i)
                break

        if progress_msg and (i % 10 == 0 or i == total):
            try:
                await progress_msg.edit_text(f"🚀 جاري الإرسال... ({i}/{total})")
            except TelegramError:
                pass

        await asyncio.sleep(SEND_DELAY)
    return success, failed


# ---------------------------------------------------------------------------
# أوامر البوت
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 أهلاً!\n\nأرسل لي الأسئلة نصاً أو ملف .txt\n\n"
        "تدعم صيغتين:\n\n"
        "الصيغة الأولى:\n"
        "Q1. السؤال\nA. خيار\nB. خيار\nAnswer: A\n\n"
        "الصيغة الثانية:\n"
        "Q1. السؤال\nA. خيار. B. خيار. C. خيار.\nAnswer: B\n\n"
        "📌 لو النص طويل وانقسم تلقائياً لعدة رسائل، البوت يجمعها تلقائياً "
        "قبل المعالجة، فقط انتظر ثانيتين بعد آخر رسالة.\n\n"
        "أوامر مفيدة:\n"
        "/cancel — إلغاء العملية الحالية\n"
        "/myid — معرفة آيديك على تيليجرام"
    )


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 آيديك: `{update.effective_user.id}`", parse_mode="Markdown"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    timer = ctx.bot_data.get("flush_timers", {}).pop(chat_id, None)
    if timer:
        timer.cancel()
    ctx.bot_data.get("text_buffers", {}).pop(chat_id, None)
    ctx.user_data.clear()
    await update.message.reply_text("✅ تم إلغاء العملية الحالية، يمكنك البدء من جديد.")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    buffers = ctx.bot_data.setdefault("text_buffers", {})
    timers  = ctx.bot_data.setdefault("flush_timers", {})

    buffers.setdefault(chat_id, []).append(update.message.text)

    old_timer = timers.get(chat_id)
    if old_timer:
        old_timer.cancel()

    async def _flush():
        try:
            await asyncio.sleep(BUFFER_DELAY)
        except asyncio.CancelledError:
            return
        chunks = buffers.pop(chat_id, [])
        timers.pop(chat_id, None)
        if chunks:
            await _process(update, ctx, _merge_chunks(chunks))

    timers[chat_id] = asyncio.create_task(_flush())


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    file = await update.message.document.get_file()
    data = await file.download_as_bytearray()
    await _process(update, ctx, data.decode("utf-8", errors="ignore"))


async def _process(update, ctx, text):
    questions = extract_questions(text)
    if not questions:
        await update.message.reply_text("⚠️ لم أجد أسئلة، تأكد من الصيغة.")
        return
    ctx.user_data["questions"] = questions
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"dest:{target}")]
        for name, target in DESTINATIONS.items()
    ]
    await update.message.reply_text(
        f"✅ تم استخراج {len(questions)} سؤال\n\n📤 أين تريد الإرسال؟",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_destination(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    target    = query.data.split(":", 1)[1]
    questions = ctx.user_data.get("questions", [])
    if not questions:
        await query.edit_message_text("⚠️ انتهت الجلسة، أرسل الأسئلة مجدداً.")
        return
    chat_id = query.message.chat_id if target == "SAME_CHAT" else target
    await query.edit_message_text(f"🚀 جاري الإرسال (0/{len(questions)})...")
    success, failed = await send_polls(ctx.bot, chat_id, questions, progress_msg=query.message)
    summary = f"✅ تم الإرسال: {success}\n❌ فشل: {len(failed)}"
    if failed:
        summary += f"\nالفاشلة: {failed}"
    await query.message.reply_text(summary)
    ctx.user_data.clear()


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.TEXT, handle_file))
    app.add_handler(CallbackQueryHandler(handle_destination, pattern=r"^dest:"))
    print("✅ البوت يعمل...")
    app.run_polling()


if __name__ == "__main__":
    main()
