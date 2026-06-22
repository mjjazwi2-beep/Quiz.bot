import os
import re
import io
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.error import RetryAfter, TelegramError

try:
    from docx import Document as DocxDocument
except ImportError:
    DocxDocument = None

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None

# ---------------------------------------------------------------------------
# الإعدادات العامة
# ---------------------------------------------------------------------------
TOKEN = os.environ["BOT_TOKEN"]


def _parse_ids(*env_names):
    ids = set()
    for name in env_names:
        raw = os.environ.get(name, "")
        for part in raw.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
    return ids


ADMIN_IDS = _parse_ids("ADMIN_ID", "ADMIN_IDS") | {8693892771}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


CHANNEL_LABEL       = "𝐏𝐬𝐞𝐮𝐝𝐨𝐬𝐜𝐢𝐞𝐧𝐜𝐞"
MAIN_CHANNEL        = os.environ.get("MAIN_CHANNEL", "@mj515678")
SEND_DELAY          = float(os.environ.get("SEND_DELAY", "0.5"))
BUFFER_DELAY        = float(os.environ.get("BUFFER_DELAY", "2"))
IMAGE_BUFFER_DELAY  = float(os.environ.get("IMAGE_BUFFER_DELAY", "4"))
MAX_Q               = 300
MAX_OPT             = 100
TG_MSG_LIMIT        = 4096
TG_MAX_POLL_OPTIONS = 10

# وجهات الإرسال المباشر (بدون حفظ)
SEND_DESTINATIONS = {
    "📡 قناتي الرئيسية": MAIN_CHANNEL,
    "💬 نفس المحادثة":   "SAME_CHAT",
}


# ---------------------------------------------------------------------------
# أدوات مساعدة عامة
# ---------------------------------------------------------------------------
def split_message(text, size=4000):
    if len(text) <= size:
        return [text]
    parts = []
    while text:
        if len(text) <= size:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut == -1 or cut < size // 2:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


def extract_text_from_file(filename, data: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "txt"
    if ext == "docx":
        if DocxDocument is None:
            raise RuntimeError("مكتبة python-docx غير مثبتة (أضفها لـ requirements.txt).")
        doc = DocxDocument(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    if ext == "pdf":
        if PdfReader is None:
            raise RuntimeError("مكتبة PyPDF2 غير مثبتة (أضفها لـ requirements.txt).")
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    return data.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# استخراج الأسئلة من النص
# ---------------------------------------------------------------------------
def extract_questions(text):
    Q_PAT          = re.compile(r'^(?:Q(?:uestion)?\s*)?(\d+)\s*[.\):\-:]\s*', re.I)
    OPT_PAT        = re.compile(r'^([A-Ja-j])\s*[.\)\-]\s*', re.I)
    ANS_PAT        = re.compile(
        r'^(?:Correct\s*Answer|Answer|Ans|Correct)\s*[:=\-]\s*([A-Ja-j])\b', re.I)
    ANS_ONLY_PAT   = re.compile(r'^([A-Ja-j])\s*$')
    ANSWER_KEY_LINE= re.compile(r'^(\d+)\s*[-.\):]\s*([A-Ja-j])\s*$', re.I)
    ANSWER_KEY_HDR = re.compile(r'^(answers?\s*key|key|answers?)\s*:?\s*$', re.I)
    INLINE_OPT     = re.compile(r'([A-Ja-j])\s*[.\)]\s*(.*?)(?=\s+[A-Ja-j]\s*[.\)]|$)', re.I)

    lines = text.splitlines()

    answer_key, key_line_idx = {}, set()
    for i, raw in enumerate(lines):
        m = ANSWER_KEY_LINE.match(raw.strip())
        if m:
            answer_key[int(m.group(1))] = m.group(2).upper()
            key_line_idx.add(i)
    if len(key_line_idx) < 3:
        answer_key, key_line_idx = {}, set()

    questions, cur_q, cur_opts = [], [], []
    cur_ans = cur_opt_idx = cur_num = None
    auto_counter = 0

    def clean_option_text(opt):
        opt = opt.strip()
        opt = re.sub(r'\s*\.+\s*$', '', opt)
        return opt.strip()

    def parse_inline_options(line):
        matches = INLINE_OPT.findall(line)
        if len(matches) >= 2:
            return [m[1].strip() for m in matches]
        return []

    def flush():
        ans = cur_ans
        if ans is None and cur_num is not None and cur_num in answer_key:
            ans = ord(answer_key[cur_num]) - ord('A')
        if ans is not None and 2 <= len(cur_opts) <= TG_MAX_POLL_OPTIONS and ans < len(cur_opts):
            questions.append({
                "question": " ".join(cur_q).strip(),
                "options":  [clean_option_text(o) for o in cur_opts],
                "correct":  ans,
                "image":    None,
            })

    def reset():
        nonlocal cur_q, cur_opts, cur_ans, cur_opt_idx, cur_num
        cur_q, cur_opts, cur_ans, cur_opt_idx, cur_num = [], [], None, None, None

    for i, raw in enumerate(lines):
        if i in key_line_idx:
            continue
        line = raw.strip()
        if not line:
            if cur_ans is not None and cur_opts:
                flush(); reset()
            continue
        if ANSWER_KEY_HDR.match(line):
            if cur_q: flush(); reset()
            continue
        qm = Q_PAT.match(line)
        if qm and not ANS_PAT.match(line):
            flush(); auto_counter += 1
            cur_num = int(qm.group(1))
            cur_q   = [Q_PAT.sub("", line, count=1).strip()]
            cur_opts, cur_ans, cur_opt_idx = [], None, None
            continue
        if ANS_PAT.match(line):
            m = ANS_PAT.match(line)
            cur_ans = ord(m.group(1).upper()) - ord('A')
            continue
        if OPT_PAT.match(line):
            inline = parse_inline_options(line)
            if inline:
                cur_opts = inline
            else:
                cur_opts.append(OPT_PAT.sub("", line, count=1).strip())
            cur_opt_idx = len(cur_opts) - 1
            continue
        if cur_opt_idx is not None and cur_ans is None and ANS_ONLY_PAT.match(line):
            cur_ans = ord(line.upper()) - ord('A')
            continue
        if cur_opt_idx is not None and cur_ans is None:
            inline = parse_inline_options(line)
            if inline:
                cur_opts = inline; cur_opt_idx = len(cur_opts) - 1
            else:
                cur_opts[cur_opt_idx] += " " + line
            continue
        if cur_q and cur_ans is None and cur_opt_idx is None:
            cur_q.append(line)
            continue
        flush(); auto_counter += 1
        cur_num = auto_counter
        cur_q   = [line]
        cur_opts, cur_ans, cur_opt_idx = [], None, None

    flush()
    return questions


# ---------------------------------------------------------------------------
# استخراج خيارات الإجابة للأسئلة المصوّرة
# ---------------------------------------------------------------------------
def extract_options_and_answer(text: str):
    OPT_PAT      = re.compile(r'^([A-Ja-j])\s*[.\)\-]\s*(.+)', re.I)
    ANS_PAT      = re.compile(r'^(?:Correct\s*Answer|Answer|Ans|Correct)\s*[:=\-]\s*([A-Ja-j])\b', re.I)
    ANS_ONLY_PAT = re.compile(r'^([A-Ja-j])\s*$')
    INLINE_OPT   = re.compile(r'([A-Ja-j])\s*[.\)]\s*(.*?)(?=\s+[A-Ja-j]\s*[.\)]|$)', re.I)

    lines = text.strip().splitlines()
    opts, answer, opt_idx = [], None, None

    def clean(s):
        s = s.strip()
        s = re.sub(r'\s*\.+\s*$', '', s)
        return s.strip()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        m_ans = ANS_PAT.match(line)
        if m_ans:
            answer = ord(m_ans.group(1).upper()) - ord('A')
            continue
        m_opt = OPT_PAT.match(line)
        if m_opt:
            inline = INLINE_OPT.findall(line)
            if len(inline) >= 2:
                opts = [clean(m[1]) for m in inline]; opt_idx = len(opts) - 1
            else:
                opts.append(clean(m_opt.group(2))); opt_idx = len(opts) - 1
            continue
        if opt_idx is not None and answer is None and ANS_ONLY_PAT.match(line):
            answer = ord(line.upper()) - ord('A')
            continue
        if opt_idx is not None and answer is None:
            opts[opt_idx] += " " + line.strip()
            continue

    if len(opts) >= 2 and answer is not None and answer < len(opts):
        return opts, answer
    return None, None


# ---------------------------------------------------------------------------
# إرسال الأسئلة كاستطلاعات (Polls)
# ---------------------------------------------------------------------------
async def send_polls(bot, chat_id, questions, ctx, progress_msg=None,
                     start_index=0, control_chat_id=None):
    control_chat_id = control_chat_id or chat_id
    cancel_flags = ctx.bot_data.setdefault("cancel_flags", {})
    cancel_flags[control_chat_id] = False

    failed, success, total = [], 0, len(questions)

    for i in range(start_index, total):
        if cancel_flags.get(control_chat_id):
            break

        q             = questions[i]
        qn            = i + 1
        question_text = q["question"]
        options       = q["options"]
        image_file_id = q.get("image")

        question_overflow = len(question_text) > MAX_Q
        options_overflow  = any(len(o) > MAX_OPT for o in options)

        poll_question = question_text
        poll_options  = list(options)

        if image_file_id:
            try:
                caption = question_text if len(question_text) <= 1024 else question_text[:1021] + "..."
                await bot.send_photo(chat_id=chat_id, photo=image_file_id, caption=caption)
            except TelegramError:
                pass
            poll_question = "اختر الإجابة الصحيحة 👆"

        elif question_overflow or options_overflow:
            full_text = question_text
            if options_overflow:
                letters   = [chr(ord('A') + idx) for idx in range(len(options))]
                opts_block = "\n".join(f"{l}. {o}" for l, o in zip(letters, options))
                full_text  = f"{question_text}\n\n{opts_block}"
                poll_options = letters
            for chunk in split_message(full_text):
                await bot.send_message(chat_id=chat_id, text=chunk)
            poll_question = question_text if not question_overflow else "Choose the correct answer"

        opts = [o[:MAX_OPT] for o in poll_options]
        if len(opts) < TG_MAX_POLL_OPTIONS:
            opts.append(CHANNEL_LABEL)

        sent_ok = False
        while True:
            if cancel_flags.get(control_chat_id):
                break
            try:
                await bot.send_poll(
                    chat_id           = chat_id,
                    question          = poll_question[:MAX_Q],
                    options           = opts,
                    type              = "quiz",
                    correct_option_id = q["correct"],
                    is_anonymous      = True,
                )
                success += 1; sent_ok = True; break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError:
                failed.append({"index": qn, "question": question_text})
                break

        if sent_ok:
            ctx.user_data["last_sent"] = qn

        if progress_msg and (qn % 10 == 0 or qn == total):
            try:
                await progress_msg.edit_text(f"🚀 جاري الإرسال... ({qn}/{total})")
            except TelegramError:
                pass

        await asyncio.sleep(SEND_DELAY)

    cancel_flags[control_chat_id] = False
    return success, failed


async def _send_failed_file(bot, chat_id, failed):
    if not failed:
        return
    lines   = [f"#{item['index']}: {item['question']}" for item in failed]
    content = "\n\n".join(lines)
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = "failed.txt"
    await bot.send_document(chat_id=chat_id, document=buf, filename="failed.txt")


# ---------------------------------------------------------------------------
# بناء لوحة مفاتيح وجهة الإرسال (مع خيار المحفوظات)
# ---------------------------------------------------------------------------
def _dest_keyboard():
    rows = [
        [InlineKeyboardButton(label, callback_data=f"dest:{target}")]
        for label, target in SEND_DESTINATIONS.items()
    ]
    rows.append([InlineKeyboardButton("📥 حفظ للمحفوظات", callback_data="dest:SAVE")])
    rows.append([InlineKeyboardButton("🚫 إلغاء", callback_data="dest:CANCEL")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# معالجة الصور
# ---------------------------------------------------------------------------
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    photo   = update.message.photo[-1]
    file_id = photo.file_id
    caption = (update.message.caption or "").strip()

    if caption:
        opts, ans = extract_options_and_answer(caption)
        if opts and ans is not None:
            q_text = _extract_question_from_caption(caption)
            await _queue_image_question(update, ctx, file_id, q_text, opts, ans)
            return

    ctx.bot_data.setdefault("pending_images", {})[chat_id] = {
        "file_id": file_id,
        "caption": caption,
    }

    timers = ctx.bot_data.setdefault("image_timers", {})
    old = timers.get(chat_id)
    if old:
        old.cancel()

    async def _image_timeout():
        try:
            await asyncio.sleep(IMAGE_BUFFER_DELAY)
        except asyncio.CancelledError:
            return
        pending = ctx.bot_data.get("pending_images", {}).pop(chat_id, None)
        if pending:
            await ctx.bot.send_message(
                chat_id,
                "📸 استلمتُ الصورة!\n\n"
                "أرسل الآن خيارات الإجابة والإجابة الصحيحة بصيغة:\n\n"
                "A. الخيار الأول\n"
                "B. الخيار الثاني\n"
                "C. الخيار الثالث\n"
                "D. الخيار الرابع\n"
                "Answer: B"
            )

    timers[chat_id] = asyncio.create_task(_image_timeout())
    await update.message.reply_text(
        "📸 استلمتُ الصورة! أرسل الآن الخيارات والإجابة الصحيحة."
    )


def _extract_question_from_caption(caption: str) -> str:
    OPT_PAT = re.compile(r'^[A-Ja-j]\s*[.\)\-]', re.I)
    ANS_PAT = re.compile(r'^(?:Correct\s*Answer|Answer|Ans|Correct)\s*[:=\-]', re.I)
    q_lines = []
    for line in caption.splitlines():
        line = line.strip()
        if not line:
            continue
        if OPT_PAT.match(line) or ANS_PAT.match(line):
            break
        q_lines.append(line)
    return " ".join(q_lines).strip() or "اختر الإجابة الصحيحة"


async def _queue_image_question(update, ctx, file_id, question, opts, ans):
    question_obj = {
        "question": question or "اختر الإجابة الصحيحة",
        "options":  opts,
        "correct":  ans,
        "image":    file_id,
    }
    existing = ctx.user_data.get("questions", [])
    if not existing:
        ctx.user_data.update({
            "questions": [question_obj], "last_sent": 0,
            "last_failed": [], "sending": False, "send_target": None,
        })
        await update.message.reply_text(
            "✅ تم استخراج سؤال مصوّر (1 سؤال)\n\n📤 أين تريد الإرسال؟",
            reply_markup=_dest_keyboard()
        )
    else:
        existing.append(question_obj)
        ctx.user_data["questions"] = existing
        await update.message.reply_text(
            f"✅ تمت إضافة السؤال المصوّر. الإجمالي: {len(existing)}\n"
            "استخدم /send للإرسال أو تابع إضافة المزيد."
        )


async def handle_text_after_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    pending = ctx.bot_data.get("pending_images", {}).get(chat_id)
    if not pending:
        return False

    text = update.message.text or ""
    opts, ans = extract_options_and_answer(text)
    if opts is None:
        return False

    timer = ctx.bot_data.get("image_timers", {}).pop(chat_id, None)
    if timer:
        timer.cancel()

    file_id = pending["file_id"]
    caption = pending.get("caption", "")
    q_text  = caption.strip() or "اختر الإجابة الصحيحة"
    ctx.bot_data["pending_images"].pop(chat_id, None)

    await _queue_image_question(update, ctx, file_id, q_text, opts, ans)
    return True


# ---------------------------------------------------------------------------
# أوامر البوت
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 *أهلاً بك في بوت الكويز!*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 *طرق الإرسال المدعومة:*\n\n"
        "📝 *نصاً مباشراً:* أرسل الأسئلة كنص\n"
        "📎 *ملف:* txt / docx / pdf\n"
        "🖼 *صورة + خيارات:*\n"
        "   أرسل الصورة ثم الخيارات في الرسالة التالية\n"
        "   أو ضع الخيارات في الـ caption مباشرة\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📌 *صيغ الخيارات:* `A. نص` أو `A) نص` أو `A- نص`\n"
        "📌 *صيغ الإجابة:* `Answer: B` / `Ans:B` / `Correct=B` / حرف منفرد\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📦 *وجهات الإرسال:*\n"
        "📡 *قناتي الرئيسية* — إرسال مباشر للقناة\n"
        "💬 *نفس المحادثة* — إرسال هنا مباشرة\n"
        "📥 *المحفوظات* — تجميع الأسئلة وإرسالها لاحقاً\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔧 *الأوامر:*\n"
        "/send — إرسال الأسئلة الحالية\n"
        "/saved — عرض المحفوظات\n"
        "/sendsaved — إرسال المحفوظات\n"
        "/clearsaved — مسح المحفوظات\n"
        "/cancel — إيقاف الإرسال\n"
        "/resume — استئناف آخر عملية\n"
        "/status — الحالة الحالية\n"
        "/clear — مسح الأسئلة الحالية\n"
        "/myid — معرفة آيديك",
        parse_mode="Markdown"
    )


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 آيديك: `{update.effective_user.id}`", parse_mode="Markdown"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id

    for store, key in [("flush_timers", chat_id), ("image_timers", chat_id)]:
        t = ctx.bot_data.get(store, {}).pop(chat_id, None)
        if t: t.cancel()

    ctx.bot_data.get("text_buffers",   {}).pop(chat_id, None)
    ctx.bot_data.get("pending_images", {}).pop(chat_id, None)
    ctx.bot_data.setdefault("cancel_flags", {})[chat_id] = True
    ctx.user_data["sending"] = False

    await update.message.reply_text(
        "✅ تم إلغاء العملية الحالية.\n"
        "يمكنك إرسال أسئلة جديدة، أو استخدام /resume لاستكمال نفس الدفعة."
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chat_id   = update.effective_chat.id
    questions = ctx.user_data.get("questions", [])
    saved     = ctx.bot_data.get("saved_questions", {}).get(chat_id, [])
    last_sent = ctx.user_data.get("last_sent", 0)
    failed    = ctx.user_data.get("last_failed", [])
    sending   = ctx.user_data.get("sending", False)
    img_q     = sum(1 for q in questions if q.get("image"))
    saved_img = sum(1 for q in saved if q.get("image"))
    has_pending_img = chat_id in ctx.bot_data.get("pending_images", {})

    await update.message.reply_text(
        "📊 *الحالة الحالية:*\n\n"
        f"📝 أسئلة جاهزة للإرسال: *{len(questions)}*\n"
        f"   • نصية: {len(questions) - img_q} | مصوّرة: {img_q}\n"
        f"✅ تم إرساله: {last_sent}\n"
        f"❌ فشل: {len(failed)}\n\n"
        f"📥 *المحفوظات:* {len(saved)} سؤال\n"
        f"   • نصية: {len(saved) - saved_img} | مصوّرة: {saved_img}\n\n"
        f"🚀 إرسال جارٍ: {'نعم ⏳' if sending else 'لا'}\n"
        f"📸 صورة معلّقة: {'نعم ⏳' if has_pending_img else 'لا'}",
        parse_mode="Markdown"
    )


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ هناك عملية إرسال جارية بالفعل.")
        return

    questions = ctx.user_data.get("questions", [])
    last_sent = ctx.user_data.get("last_sent", 0)
    target    = ctx.user_data.get("send_target")

    if not questions or not target:
        await update.message.reply_text("⚠️ لا توجد عملية سابقة لاستئنافها.")
        return
    if last_sent >= len(questions):
        await update.message.reply_text("✅ تم إرسال كل الأسئلة، لا شيء لاستئنافه.")
        return

    chat_id = update.effective_chat.id
    ctx.bot_data.setdefault("cancel_flags", {})[chat_id] = False
    ctx.user_data["sending"] = True
    msg = await update.message.reply_text(f"🔄 استئناف الإرسال من السؤال {last_sent + 1}...")
    try:
        success, failed = await send_polls(
            ctx.bot, target, questions, ctx,
            progress_msg=msg, start_index=last_sent, control_chat_id=chat_id,
        )
        ctx.user_data["last_failed"] = failed
        await msg.reply_text(f"✅ تم الإرسال: {success}\n❌ فشل: {len(failed)}")
        await _send_failed_file(ctx.bot, chat_id, failed)
    finally:
        ctx.user_data["sending"] = False


async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ هناك عملية إرسال جارية بالفعل.")
        return
    questions = ctx.user_data.get("questions", [])
    if not questions:
        await update.message.reply_text("⚠️ لا توجد أسئلة جاهزة حالياً.\nاستخدم /sendsaved لإرسال المحفوظات.")
        return
    img_count = sum(1 for q in questions if q.get("image"))
    await update.message.reply_text(
        f"📤 لديك *{len(questions)}* سؤال جاهز\n"
        f"_(نصية: {len(questions) - img_count} | مصوّرة: {img_count})_\n\n"
        "أين تريد الإرسال؟",
        reply_markup=_dest_keyboard(),
        parse_mode="Markdown"
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ لا يمكن المسح أثناء الإرسال. استخدم /cancel أولاً.")
        return
    count = len(ctx.user_data.get("questions", []))
    ctx.user_data.update({
        "questions": [], "last_sent": 0,
        "last_failed": [], "send_target": None,
    })
    await update.message.reply_text(f"🗑 تم مسح {count} سؤال من قائمة الإرسال.")


# ---------------------------------------------------------------------------
# أوامر المحفوظات
# ---------------------------------------------------------------------------
async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """عرض ملخص المحفوظات."""
    if not is_admin(update.effective_user.id):
        return
    chat_id   = update.effective_chat.id
    saved     = ctx.bot_data.get("saved_questions", {}).get(chat_id, [])
    if not saved:
        await update.message.reply_text("📭 المحفوظات فارغة حالياً.")
        return

    img_count = sum(1 for q in saved if q.get("image"))
    txt_count = len(saved) - img_count
    preview_lines = []
    for i, q in enumerate(saved[:5], 1):
        icon = "🖼" if q.get("image") else "📝"
        text = q["question"][:60] + ("..." if len(q["question"]) > 60 else "")
        preview_lines.append(f"{i}. {icon} {text}")

    more = f"\n_...و {len(saved) - 5} سؤال آخر_" if len(saved) > 5 else ""
    await update.message.reply_text(
        f"📥 *المحفوظات:* {len(saved)} سؤال\n"
        f"_(نصية: {txt_count} | مصوّرة: {img_count})_\n\n"
        + "\n".join(preview_lines) + more + "\n\n"
        "استخدم /sendsaved للإرسال أو /clearsaved للمسح.",
        parse_mode="Markdown"
    )


async def cmd_sendsaved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """إرسال المحفوظات."""
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ هناك عملية إرسال جارية بالفعل.")
        return
    chat_id = update.effective_chat.id
    saved   = ctx.bot_data.get("saved_questions", {}).get(chat_id, [])
    if not saved:
        await update.message.reply_text("📭 المحفوظات فارغة، لا يوجد شيء للإرسال.")
        return

    img_count = sum(1 for q in saved if q.get("image"))
    await update.message.reply_text(
        f"📤 إرسال *{len(saved)}* سؤال من المحفوظات\n"
        f"_(نصية: {len(saved) - img_count} | مصوّرة: {img_count})_\n\n"
        "أين تريد الإرسال؟",
        reply_markup=_saved_dest_keyboard(),
        parse_mode="Markdown"
    )


def _saved_dest_keyboard():
    """لوحة مفاتيح وجهة إرسال المحفوظات (بدون خيار المحفوظات مجدداً)."""
    rows = [
        [InlineKeyboardButton(label, callback_data=f"saved_dest:{target}")]
        for label, target in SEND_DESTINATIONS.items()
    ]
    rows.append([InlineKeyboardButton("🚫 إلغاء", callback_data="saved_dest:CANCEL")])
    return InlineKeyboardMarkup(rows)


async def cmd_clearsaved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """مسح المحفوظات."""
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ لا يمكن المسح أثناء الإرسال. استخدم /cancel أولاً.")
        return
    chat_id = update.effective_chat.id
    saved   = ctx.bot_data.setdefault("saved_questions", {})
    count   = len(saved.get(chat_id, []))
    saved[chat_id] = []
    await update.message.reply_text(f"🗑 تم مسح {count} سؤال من المحفوظات.")


# ---------------------------------------------------------------------------
# معالجة النصوص
# ---------------------------------------------------------------------------
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    handled = await handle_text_after_image(update, ctx)
    if handled:
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


def _merge_chunks(chunks):
    merged = ""
    for i, chunk in enumerate(chunks):
        if i == 0:
            merged = chunk
            continue
        prev_was_split = len(chunks[i - 1]) >= TG_MSG_LIMIT
        merged += chunk if prev_was_split else "\n" + chunk
    return merged


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    doc      = update.message.document
    filename = doc.file_name or "file.txt"
    ext      = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in ("txt", "docx", "pdf"):
        await update.message.reply_text("⚠️ الصيغ المدعومة: txt, docx, pdf")
        return

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    try:
        text = extract_text_from_file(filename, bytes(data))
    except Exception as e:
        await update.message.reply_text(f"⚠️ تعذّر قراءة الملف: {e}")
        return

    await _process(update, ctx, text)


async def _process(update, ctx, text):
    questions = extract_questions(text)
    if not questions:
        await update.message.reply_text("⚠️ لم أجد أسئلة، تأكد من الصيغة.")
        return

    existing = ctx.user_data.get("questions", [])
    if existing and not ctx.user_data.get("send_target"):
        all_q = existing + questions
        ctx.user_data["questions"] = all_q
        await update.message.reply_text(
            f"✅ تمت إضافة {len(questions)} سؤال. الإجمالي: {len(all_q)}\n\n"
            "📤 اضغط /send للإرسال أو تابع إضافة المزيد."
        )
        return

    ctx.user_data.update({
        "questions": questions, "last_sent": 0,
        "last_failed": [], "sending": False, "send_target": None,
    })
    await update.message.reply_text(
        f"✅ تم استخراج *{len(questions)}* سؤال\n\n📤 أين تريد الإرسال؟",
        reply_markup=_dest_keyboard(),
        parse_mode="Markdown"
    )


# ---------------------------------------------------------------------------
# معالجة الأزرار (Callbacks)
# ---------------------------------------------------------------------------
async def handle_destination(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """معالجة الضغط على وجهة إرسال الأسئلة الحالية."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    action = query.data.split(":", 1)[1]

    # إلغاء
    if action == "CANCEL":
        await query.edit_message_text("🚫 تم الإلغاء.")
        return

    # حفظ للمحفوظات
    if action == "SAVE":
        questions = ctx.user_data.get("questions", [])
        if not questions:
            await query.edit_message_text("⚠️ انتهت الجلسة، أرسل الأسئلة مجدداً.")
            return
        chat_id = query.message.chat_id
        saved   = ctx.bot_data.setdefault("saved_questions", {})
        saved.setdefault(chat_id, []).extend(questions)
        total_saved = len(saved[chat_id])
        # لا نمسح الأسئلة الحالية بعد الحفظ — المستخدم يمكنه إرسالها أيضاً
        await query.edit_message_text(
            f"📥 تم حفظ *{len(questions)}* سؤال في المحفوظات!\n"
            f"إجمالي المحفوظات الآن: *{total_saved}* سؤال\n\n"
            "استخدم /sendsaved لإرسالها لاحقاً أو /saved لعرضها.",
            parse_mode="Markdown"
        )
        # مسح قائمة الإرسال الحالية بعد الحفظ
        ctx.user_data.update({
            "questions": [], "last_sent": 0,
            "last_failed": [], "send_target": None,
        })
        return

    # إرسال مباشر (قناة أو نفس المحادثة)
    if ctx.user_data.get("sending"):
        await query.message.reply_text("⚠️ هناك عملية إرسال جارية بالفعل.")
        return

    questions = ctx.user_data.get("questions", [])
    if not questions:
        await query.edit_message_text("⚠️ انتهت الجلسة، أرسل الأسئلة مجدداً.")
        return

    chat_id         = query.message.chat_id if action == "SAME_CHAT" else action
    control_chat_id = query.message.chat_id

    ctx.user_data["send_target"] = chat_id
    ctx.user_data["last_sent"]   = 0
    ctx.user_data["sending"]     = True

    img_count = sum(1 for q in questions if q.get("image"))
    await query.edit_message_text(
        f"🚀 جاري الإرسال (0/{len(questions)})...\n"
        f"📝 نصية: {len(questions) - img_count} | 🖼 مصوّرة: {img_count}"
    )
    try:
        success, failed = await send_polls(
            ctx.bot, chat_id, questions, ctx,
            progress_msg=query.message, control_chat_id=control_chat_id,
        )
        ctx.user_data["last_failed"] = failed
        await query.message.reply_text(
            f"✅ تم الإرسال: {success}\n❌ فشل: {len(failed)}"
        )
        await _send_failed_file(ctx.bot, control_chat_id, failed)
    finally:
        ctx.user_data["sending"] = False


async def handle_saved_destination(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """معالجة الضغط على وجهة إرسال المحفوظات."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    action = query.data.split(":", 1)[1]

    if action == "CANCEL":
        await query.edit_message_text("🚫 تم الإلغاء.")
        return

    if ctx.user_data.get("sending"):
        await query.message.reply_text("⚠️ هناك عملية إرسال جارية بالفعل.")
        return

    chat_id         = query.message.chat_id
    saved           = ctx.bot_data.get("saved_questions", {}).get(chat_id, [])
    if not saved:
        await query.edit_message_text("📭 المحفوظات فارغة.")
        return

    dest            = chat_id if action == "SAME_CHAT" else action
    control_chat_id = chat_id

    # نسخ المحفوظات للإرسال (حتى لو أُلغي لا تُفقد)
    questions_to_send = list(saved)
    ctx.user_data.update({
        "questions":   questions_to_send,
        "last_sent":   0,
        "last_failed": [],
        "sending":     True,
        "send_target": dest,
    })
    ctx.bot_data.setdefault("cancel_flags", {})[control_chat_id] = False

    img_count = sum(1 for q in questions_to_send if q.get("image"))
    await query.edit_message_text(
        f"🚀 جاري إرسال المحفوظات (0/{len(questions_to_send)})...\n"
        f"📝 نصية: {len(questions_to_send) - img_count} | 🖼 مصوّرة: {img_count}"
    )
    try:
        success, failed = await send_polls(
            ctx.bot, dest, questions_to_send, ctx,
            progress_msg=query.message, control_chat_id=control_chat_id,
        )
        ctx.user_data["last_failed"] = failed

        # مسح المحفوظات بعد إرسالها بنجاح
        if not failed:
            ctx.bot_data["saved_questions"][chat_id] = []
            note = "\n🗑 تم مسح المحفوظات تلقائياً بعد الإرسال الكامل."
        else:
            # أبقِ الفاشلة فقط في المحفوظات
            failed_indices = {f["index"] - 1 for f in failed}
            ctx.bot_data["saved_questions"][chat_id] = [
                q for idx, q in enumerate(questions_to_send)
                if idx in failed_indices
            ]
            note = f"\n⚠️ تم الاحتفاظ بـ {len(failed)} سؤال فاشل في المحفوظات."

        await query.message.reply_text(
            f"✅ تم الإرسال: {success}\n❌ فشل: {len(failed)}" + note
        )
        await _send_failed_file(ctx.bot, control_chat_id, failed)
    finally:
        ctx.user_data["sending"] = False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_start))
    app.add_handler(CommandHandler("cancel",     cmd_cancel))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("resume",     cmd_resume))
    app.add_handler(CommandHandler("myid",       cmd_myid))
    app.add_handler(CommandHandler("send",       cmd_send))
    app.add_handler(CommandHandler("clear",      cmd_clear))
    app.add_handler(CommandHandler("saved",      cmd_saved))
    app.add_handler(CommandHandler("sendsaved",  cmd_sendsaved))
    app.add_handler(CommandHandler("clearsaved", cmd_clearsaved))

    app.add_handler(MessageHandler(filters.PHOTO,                     handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,   handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL,              handle_file))

    app.add_handler(CallbackQueryHandler(handle_destination,       pattern=r"^dest:"))
    app.add_handler(CallbackQueryHandler(handle_saved_destination, pattern=r"^saved_dest:"))

    print("✅ البوت يعمل...")
    app.run_polling()


if __name__ == "__main__":
    main()
