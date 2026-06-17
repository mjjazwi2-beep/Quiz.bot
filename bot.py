# ════════════════════════════════════════
#           Quiz Bot — bot.py
# ════════════════════════════════════════

import os, re, asyncio
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.error import RetryAfter, TelegramError

# ──────────────────────────────────────────
#  الإعدادات  (تُقرأ من متغيرات البيئة)
# ──────────────────────────────────────────
TOKEN      = os.environ["BOT_TOKEN"]
ADMIN_ID   = int(os.environ["ADMIN_ID"])   # ID حسابك الشخصي
CHANNEL_LABEL = "𝐏𝐬𝐞𝐮𝐝𝐨𝐬𝐜𝐢𝐞𝐧𝐜𝐞"

DELAY       = 4      # ثواني بين كل سؤال
MAX_Q       = 300    # حد السؤال
MAX_OPT     = 100    # حد الخيار

# قائمة القنوات/المجموعات التي تريد الإرسال إليها
# أضف أو احذف كما تشاء
DESTINATIONS = {
    "قناتي الرئيسية"  : "@mj515678",
    "مجموعة الاختبار" : "@test_group_here",
    "نفس المحادثة"    : "SAME_CHAT",       # يرسل في نفس الشات
}

# ──────────────────────────────────────────
#  استخراج الأسئلة
# ──────────────────────────────────────────
def extract_questions(text: str) -> list[dict]:
    questions   = []
    cur_q       = []
    cur_opts    = []
    cur_ans     = None
    cur_opt_idx = None

    Q_PAT   = re.compile(r'^(?:Q(?:uestion)?\s*)?\d+\s*[.):-]', re.I)
    OPT_PAT = re.compile(r'^([A-F])\s*[.):-]', re.I)
    ANS_PAT = re.compile(
        r'^(?:Answer|Correct\s*Answer|Ans)\s*[:=\-]\s*([A-F])\b', re.I
    )

    def flush():
        if (cur_q and cur_ans is not None
                and 2 <= len(cur_opts) <= 6
                and cur_ans < len(cur_opts)):
            questions.append({
                "question": " ".join(cur_q).strip(),
                "options" : [o.strip() for o in cur_opts],
                "correct" : cur_ans,
            })

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

        elif m := OPT_PAT.match(line):
            cur_opts.append(OPT_PAT.sub("", line).strip())
            cur_opt_idx = len(cur_opts) - 1

        elif m := ANS_PAT.match(line):
            cur_ans = ord(m.group(1).upper()) - ord('A')

        elif cur_opt_idx is not None:
            cur_opts[cur_opt_idx] += " " + line

        elif cur_q:
            cur_q.append(line)

    flush()
    return questions

# ──────────────────────────────────────────
#  إرسال الاستفتاءات
# ──────────────────────────────────────────
async def send_polls(bot, chat_id: str, questions: list[dict]):
    failed  = []
    success = 0

    for i, q in enumerate(questions, 1):
        opts = [o[:MAX_OPT] for o in q["options"]] + [CHANNEL_LABEL]
        while True:
            try:
                await bot.send_poll(
                    chat_id            = chat_id,
                    question           = q["question"][:MAX_Q],
                    options            = opts,
                    type               = "quiz",
                    correct_option_ids = [q["correct"]],
                    is_anonymous       = True,
                )
                success += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError:
                failed.append(i)
                break
        await asyncio.sleep(DELAY)

    return success, failed

# ──────────────────────────────────────────
#  Handlers
# ──────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "👋 أهلاً!\n\n"
        "أرسل لي الأسئلة مباشرةً *نصاً* أو كـ *ملف .txt*\n\n"
        "الصيغة المطلوبة:\n"
        "```\n"
        "Q1. نص السؤال\n"
        "A. الخيار الأول\n"
        "B. الخيار الثاني\n"
        "C. الخيار الثالث\n"
        "Answer: B\n"
        "```",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text
    await _process_questions(update, ctx, text)


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    doc  = update.message.document
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="ignore")
    await _process_questions(update, ctx, text)


async def _process_questions(update: Update, ctx, text: str):
    questions = extract_questions(text)

    if not questions:
        await update.message.reply_text(
            "⚠️ لم أتمكن من استخراج أي سؤال.\n"
            "تأكد من الصيغة وأرسل مجدداً."
        )
        return

    # احفظ الأسئلة مؤقتاً
    ctx.user_data["questions"] = questions

    # اعرض أزرار الوجهات
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"dest:{target}")]
        for name, target in DESTINATIONS.items()
    ]
    await update.message.reply_text(
        f"✅ تم استخراج *{len(questions)}* سؤال.\n\n"
        "📤 أين تريد إرسال الاستفتاءات؟",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_destination(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_ID:
        return

    target    = query.data.split(":", 1)[1]
    questions = ctx.user_data.get("questions", [])

    if not questions:
        await query.edit_message_text("⚠️ انتهت الجلسة، أرسل الأسئلة مجدداً.")
        return

    # تحديد الوجهة
    chat_id = query.message.chat_id if target == "SAME_CHAT" else target

    await query.edit_message_text(
        f"🚀 جاري الإرسال ({len(questions)} سؤال)..."
    )

    success, failed = await send_polls(ctx.bot, chat_id, questions)

    summary = (
        f"✅ تم الإرسال: {success}\n"
        f"❌ فشل الإرسال: {len(failed)}\n"
    )
    if failed:
        summary += f"الأسئلة الفاشلة: {failed}"

    await query.message.reply_text(summary)
    ctx.user_data.clear()

# ──────────────────────────────────────────
#  تشغيل البوت
# ──────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.TEXT, handle_file))
    app.add_handler(CallbackQueryHandler(handle_destination, pattern=r"^dest:"))

    print("✅ البوت يعمل...")
    app.run_polling()

if __name__ == "__main__":
    main()
