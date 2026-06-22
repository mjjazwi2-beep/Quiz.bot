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

# مكتبات اختيارية لقراءة docx / pdf — أضفها لملف requirements.txt على Railway:
#   python-docx
#   PyPDF2
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
    """يقرأ آيديات أدمن من متغيرات بيئة (يمكن وضع أكثر من آيدي مفصولين بفاصلة)."""
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


CHANNEL_LABEL = "𝐏𝐬𝐞𝐮𝐝𝐨𝐬𝐜𝐢𝐞𝐧𝐜𝐞"
MAIN_CHANNEL  = os.environ.get("MAIN_CHANNEL", "@mj515678")
SEND_DELAY    = float(os.environ.get("SEND_DELAY", "0.5"))
BUFFER_DELAY  = float(os.environ.get("BUFFER_DELAY", "2"))
IMAGE_BUFFER_DELAY = float(os.environ.get("IMAGE_BUFFER_DELAY", "4"))  # وقت انتظار الخيارات بعد الصورة
MAX_Q         = 300
MAX_OPT       = 100
TG_MSG_LIMIT  = 4096
TG_MAX_POLL_OPTIONS = 10

DESTINATIONS = {
    "قناتي الرئيسية" : MAIN_CHANNEL,
    "نفس المحادثة"   : "SAME_CHAT",
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
            raise RuntimeError("مكتبة python-docx غير مثبتة على السيرفر (أضفها لـ requirements.txt).")
        doc = DocxDocument(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)

    if ext == "pdf":
        if PdfReader is None:
            raise RuntimeError("مكتبة PyPDF2 غير مثبتة على السيرفر (أضفها لـ requirements.txt).")
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)

    return data.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# استخراج الأسئلة من النص
# ---------------------------------------------------------------------------
def extract_questions(text):
    Q_PAT = re.compile(r'^(?:Q(?:uestion)?\s*)?(\d+)\s*[.\):\-:]\s*', re.I)
    OPT_PAT = re.compile(r'^([A-Ja-j])\s*[.\)\-]\s*', re.I)
    ANS_PAT = re.compile(
        r'^(?:Correct\s*Answer|Answer|Ans|Correct)\s*[:=\-]\s*([A-Ja-j])\b', re.I)
    ANS_ONLY_PAT = re.compile(r'^([A-Ja-j])\s*$')
    ANSWER_KEY_LINE = re.compile(r'^(\d+)\s*[-.\):]\s*([A-Ja-j])\s*$', re.I)
    ANSWER_KEY_HEADER = re.compile(r'^(answers?\s*key|key|answers?)\s*:?\s*$', re.I)
    INLINE_OPT = re.compile(
        r'([A-Ja-j])\s*[.\)]\s*(.*?)(?=\s+[A-Ja-j]\s*[.\)]|$)', re.I)

    lines = text.splitlines()

    answer_key   = {}
    key_line_idx = set()
    for i, raw in enumerate(lines):
        m = ANSWER_KEY_LINE.match(raw.strip())
        if m:
            answer_key[int(m.group(1))] = m.group(2).upper()
            key_line_idx.add(i)
    if len(key_line_idx) < 3:
        answer_key, key_line_idx = {}, set()

    questions    = []
    cur_q        = []
    cur_opts     = []
    cur_ans      = None
    cur_opt_idx  = None
    cur_num      = None
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
        if not cur_q:
            return
        ans = cur_ans
        if ans is None and cur_num is not None and cur_num in answer_key:
            ans = ord(answer_key[cur_num]) - ord('A')
        if ans is not None and 2 <= len(cur_opts) <= TG_MAX_POLL_OPTIONS and ans < len(cur_opts):
            questions.append({
                "question": " ".join(cur_q).strip(),
                "options" : [clean_option_text(o) for o in cur_opts],
                "correct" : ans,
                "image"   : None,   # لا توجد صورة في الأسئلة النصية
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
                flush()
                reset()
            continue

        if ANSWER_KEY_HEADER.match(line):
            if cur_q:
                flush()
                reset()
            continue

        qm = Q_PAT.match(line)
        if qm and not ANS_PAT.match(line):
            flush()
            auto_counter += 1
            cur_num     = int(qm.group(1))
            cur_q       = [Q_PAT.sub("", line, count=1).strip()]
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
                cur_opts    = inline
                cur_opt_idx = len(cur_opts) - 1
            else:
                cur_opts[cur_opt_idx] += " " + line
            continue

        if cur_q and cur_ans is None and cur_opt_idx is None:
            cur_q.append(line)
            continue

        flush()
        auto_counter += 1
        cur_num = auto_counter
        cur_q   = [line]
        cur_opts, cur_ans, cur_opt_idx = [], None, None

    flush()
    return questions


# ---------------------------------------------------------------------------
# استخراج خيارات الإجابة من نص (للأسئلة المصوّرة)
# ---------------------------------------------------------------------------
def extract_options_and_answer(text: str):
    """
    يحلّل النص المرسل بعد صورة لاستخراج خيارات الإجابة والإجابة الصحيحة.
    يدعم الصيغ:
      A) نص   A. نص   A- نص   a) نص   a. نص
    والإجابة الصحيحة بصيغة: Answer: B / Ans=B / Correct: B / أو حرف منفرد
    """
    OPT_PAT = re.compile(r'^([A-Ja-j])\s*[.\)\-]\s*(.+)', re.I)
    ANS_PAT = re.compile(
        r'^(?:Correct\s*Answer|Answer|Ans|Correct)\s*[:=\-]\s*([A-Ja-j])\b', re.I)
    ANS_ONLY_PAT = re.compile(r'^([A-Ja-j])\s*$')
    INLINE_OPT = re.compile(
        r'([A-Ja-j])\s*[.\)]\s*(.*?)(?=\s+[A-Ja-j]\s*[.\)]|$)', re.I)

    lines  = text.strip().splitlines()
    opts   = []
    answer = None
    opt_idx = None

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
            # جرب inline أولاً
            inline = INLINE_OPT.findall(line)
            if len(inline) >= 2:
                opts = [clean(m[1]) for m in inline]
                opt_idx = len(opts) - 1
            else:
                opts.append(clean(m_opt.group(2)))
                opt_idx = len(opts) - 1
            continue

        if opt_idx is not None and answer is None and ANS_ONLY_PAT.match(line):
            answer = ord(line.upper()) - ord('A')
            continue

        if opt_idx is not None and answer is None:
            # تكملة لآخر خيار
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

    failed  = []
    success = 0
    total   = len(questions)

    for i in range(start_index, total):
        if cancel_flags.get(control_chat_id):
            break

        q  = questions[i]
        qn = i + 1
        question_text = q["question"]
        options       = q["options"]
        image_file_id = q.get("image")   # file_id الصورة إن وُجدت

        question_overflow = len(question_text) > MAX_Q
        options_overflow  = any(len(o) > MAX_OPT for o in options)

        poll_question = question_text
        poll_options  = list(options)

        # --- أرسل الصورة أولاً إن وُجدت ---
        if image_file_id:
            try:
                # إرسال الصورة مع نص السؤال كـ caption
                caption = question_text if len(question_text) <= 1024 else question_text[:1021] + "..."
                await bot.send_photo(
                    chat_id   = chat_id,
                    photo     = image_file_id,
                    caption   = caption,
                )
            except TelegramError:
                pass  # إن فشل إرسال الصورة، نكمل بالاستطلاع عادياً

            # عند وجود صورة: استطلاع نصه "اختر الإجابة الصحيحة" لأن السؤال ظهر في الصورة+caption
            poll_question = "اختر الإجابة الصحيحة 👆"

        elif question_overflow or options_overflow:
            full_text = question_text
            if options_overflow:
                letters = [chr(ord('A') + idx) for idx in range(len(options))]
                opts_block = "\n".join(f"{l}. {o}" for l, o in zip(letters, options))
                full_text = f"{question_text}\n\n{opts_block}"
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
                success += 1
                sent_ok  = True
                break
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
    lines = [f"#{item['index']}: {item['question']}" for item in failed]
    content = "\n\n".join(lines)
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = "failed.txt"
    await bot.send_document(chat_id=chat_id, document=buf, filename="failed.txt")


# ---------------------------------------------------------------------------
# معالجة رسائل الصور مع الخيارات
# ---------------------------------------------------------------------------
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    يعالج الصور المُرسلة للبوت:
    - إذا كانت الصورة بها caption يحتوي على خيارات وإجابة → تُعامَل كسؤال مصوّر فوري.
    - إذا لم يكن بها caption → يحفظ البوت الصورة وينتظر الرسالة النصية التالية
      (خلال IMAGE_BUFFER_DELAY ثوانٍ) التي تحتوي على الخيارات والإجابة.
    """
    if not is_admin(update.effective_user.id):
        return

    chat_id  = update.effective_chat.id
    photo    = update.message.photo[-1]  # أعلى جودة
    file_id  = photo.file_id
    caption  = (update.message.caption or "").strip()

    if caption:
        # جرّب استخراج الخيارات من الـ caption مباشرة
        opts, ans = extract_options_and_answer(caption)
        if opts and ans is not None:
            # استخرج نص السؤال (الجزء قبل الخيارات)
            q_text = _extract_question_from_caption(caption)
            await _queue_image_question(update, ctx, file_id, q_text, opts, ans)
            return

    # لا caption أو caption بدون خيارات كاملة → انتظر النص التالي
    ctx.bot_data.setdefault("pending_images", {})[chat_id] = {
        "file_id" : file_id,
        "caption" : caption,
    }

    # مؤقت: إذا لم يأتِ نص خلال IMAGE_BUFFER_DELAY → أبلغ المستخدم
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
    """يستخرج نص السؤال من الـ caption (قبل ظهور أول خيار)."""
    OPT_PAT = re.compile(r'^[A-Ja-j]\s*[.\)\-]', re.I)
    ANS_PAT = re.compile(r'^(?:Correct\s*Answer|Answer|Ans|Correct)\s*[:=\-]', re.I)
    lines = caption.splitlines()
    q_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if OPT_PAT.match(line) or ANS_PAT.match(line):
            break
        q_lines.append(line)
    return " ".join(q_lines).strip() or "اختر الإجابة الصحيحة"


async def _queue_image_question(update, ctx, file_id, question, opts, ans):
    """يُضيف سؤالاً مصوّراً لقائمة الأسئلة ويطلب وجهة الإرسال."""
    question_obj = {
        "question": question or "اختر الإجابة الصحيحة",
        "options" : opts,
        "correct" : ans,
        "image"   : file_id,
    }

    existing = ctx.user_data.get("questions", [])
    # إذا لم يكن هناك أسئلة معلّقة → ابدأ دفعة جديدة بهذا السؤال
    if not existing:
        ctx.user_data["questions"]   = [question_obj]
        ctx.user_data["last_sent"]   = 0
        ctx.user_data["last_failed"] = []
        ctx.user_data["sending"]     = False
        ctx.user_data["send_target"] = None
        keyboard = [
            [InlineKeyboardButton(name, callback_data=f"dest:{target}")]
            for name, target in DESTINATIONS.items()
        ]
        await update.message.reply_text(
            "✅ تم استخراج سؤال مصوّر (1 سؤال)\n\n📤 أين تريد الإرسال؟",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # أضفه للقائمة الحالية
        existing.append(question_obj)
        ctx.user_data["questions"] = existing
        await update.message.reply_text(
            f"✅ تمت إضافة السؤال المصوّر. إجمالي الأسئلة الآن: {len(existing)}\n"
            "استخدم /send لإرسال الكل، أو أرسل المزيد."
        )


async def handle_text_after_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يتحقق هل هناك صورة معلّقة لهذا الشات.
    إذا كان كذلك، يحاول استخراج الخيارات من النص ثم يعالج السؤال المصوّر.
    يُعيد True إذا تمت المعالجة (ولا داعي لمعالجة النص كأسئلة عادية).
    """
    chat_id = update.effective_chat.id
    pending = ctx.bot_data.get("pending_images", {}).get(chat_id)
    if not pending:
        return False

    text = update.message.text or ""
    opts, ans = extract_options_and_answer(text)
    if opts is None:
        return False  # النص لا يبدو خيارات — عالجه كنص أسئلة عادي

    # إلغاء مؤقت الإشعار
    timer = ctx.bot_data.get("image_timers", {}).pop(chat_id, None)
    if timer:
        timer.cancel()

    file_id  = pending["file_id"]
    caption  = pending.get("caption", "")
    q_text   = caption.strip() or "اختر الإجابة الصحيحة"
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
        "👋 أهلاً!\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 *طرق الإرسال المدعومة:*\n\n"
        "📝 *نصاً مباشراً:* أرسل الأسئلة نصاً\n"
        "📎 *ملف:* txt / docx / pdf\n"
        "🖼 *صورة + خيارات:*\n"
        "   أرسل الصورة ثم أرسل الخيارات في الرسالة التالية\n"
        "   أو ضع الخيارات في الـ caption مباشرة\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📌 *صيغ الخيارات المدعومة:*\n"
        "`A. نص` أو `A) نص` أو `A- نص` أو `a. نص`\n\n"
        "📌 *صيغ الإجابة المدعومة:*\n"
        "`Answer: B` / `Ans:B` / `Correct=B` / أو حرف منفرد\n\n"
        "📌 *لو النص طويل وانقسم تلقائياً:*\n"
        "البوت يجمعه تلقائياً، انتظر ثانيتين بعد آخر رسالة.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔧 *الأوامر المتاحة:*\n"
        "/send — إرسال الأسئلة المحفوظة\n"
        "/cancel — إيقاف الإرسال فوراً\n"
        "/resume — استئناف آخر عملية متوقفة\n"
        "/status — عرض الحالة الحالية\n"
        "/clear — مسح الأسئلة المحفوظة\n"
        "/myid — معرفة آيديك على تيليجرام",
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

    timer = ctx.bot_data.get("flush_timers", {}).pop(chat_id, None)
    if timer:
        timer.cancel()
    ctx.bot_data.get("text_buffers", {}).pop(chat_id, None)

    img_timer = ctx.bot_data.get("image_timers", {}).pop(chat_id, None)
    if img_timer:
        img_timer.cancel()
    ctx.bot_data.get("pending_images", {}).pop(chat_id, None)

    ctx.bot_data.setdefault("cancel_flags", {})[chat_id] = True
    ctx.user_data["sending"] = False
    await update.message.reply_text(
        "✅ تم إلغاء العملية الحالية فوراً.\n"
        "يمكنك إرسال أسئلة جديدة، أو استخدام /resume لاستكمال نفس الدفعة."
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    questions = ctx.user_data.get("questions", [])
    last_sent = ctx.user_data.get("last_sent", 0)
    failed    = ctx.user_data.get("last_failed", [])
    sending   = ctx.user_data.get("sending", False)
    img_q     = sum(1 for q in questions if q.get("image"))
    txt_q     = len(questions) - img_q
    chat_id   = update.effective_chat.id
    has_pending_img = chat_id in ctx.bot_data.get("pending_images", {})
    await update.message.reply_text(
        "📊 *الحالة الحالية:*\n\n"
        f"📝 إجمالي الأسئلة المستخرجة: {len(questions)}\n"
        f"   • نصية: {txt_q} | مصوّرة: {img_q}\n"
        f"✅ تم إرسال: {last_sent}\n"
        f"❌ فشل: {len(failed)}\n"
        f"🚀 عملية جارية الآن: {'نعم' if sending else 'لا'}\n"
        f"📸 صورة معلّقة: {'نعم' if has_pending_img else 'لا'}",
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
        await update.message.reply_text("✅ تم إرسال كل الأسئلة سابقاً، لا شيء لاستئنافه.")
        return

    chat_id = update.effective_chat.id
    ctx.bot_data.setdefault("cancel_flags", {})[chat_id] = False
    ctx.user_data["sending"] = True
    msg = await update.message.reply_text(f"🚀 استئناف الإرسال من السؤال {last_sent + 1}...")
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
    """يُرسل الأسئلة المحفوظة يدوياً (بعد تجميع عدة صور مثلاً)."""
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ هناك عملية إرسال جارية بالفعل.")
        return
    questions = ctx.user_data.get("questions", [])
    if not questions:
        await update.message.reply_text("⚠️ لا توجد أسئلة محفوظة حالياً.")
        return
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"dest:{target}")]
        for name, target in DESTINATIONS.items()
    ]
    await update.message.reply_text(
        f"📤 لديك {len(questions)} سؤال محفوظ. أين تريد الإرسال؟",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """يمسح قائمة الأسئلة المحفوظة."""
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ لا يمكن المسح أثناء الإرسال. استخدم /cancel أولاً.")
        return
    count = len(ctx.user_data.get("questions", []))
    ctx.user_data["questions"]   = []
    ctx.user_data["last_sent"]   = 0
    ctx.user_data["last_failed"] = []
    ctx.user_data["send_target"] = None
    await update.message.reply_text(f"🗑 تم مسح {count} سؤال.")


# ---------------------------------------------------------------------------
# معالجة النصوص
# ---------------------------------------------------------------------------
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    # أولوية: هل هذا النص خيارات لصورة معلّقة؟
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
    doc = update.message.document
    filename = doc.file_name or "file.txt"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in ("txt", "docx", "pdf"):
        await update.message.reply_text("⚠️ الصيغ المدعومة فقط: txt, docx, pdf")
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

    # أضف الأسئلة الجديدة لأي أسئلة موجودة (مثل مزج الصور مع النص)
    existing = ctx.user_data.get("questions", [])
    # إن كانت existing كلها أسئلة مصوّرة → اجمعها مع الجديدة
    if existing and not ctx.user_data.get("send_target"):
        all_q = existing + questions
        ctx.user_data["questions"] = all_q
        await update.message.reply_text(
            f"✅ تمت إضافة {len(questions)} سؤال. الإجمالي: {len(all_q)} سؤال\n\n"
            "📤 اضغط /send للإرسال أو تابع إضافة المزيد."
        )
        return

    ctx.user_data["questions"]   = questions
    ctx.user_data["last_sent"]   = 0
    ctx.user_data["last_failed"] = []
    ctx.user_data["sending"]     = False
    ctx.user_data["send_target"] = None
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

    if ctx.user_data.get("sending"):
        await query.message.reply_text(
            "⚠️ هناك عملية إرسال جارية بالفعل، انتظر حتى تنتهي أو استخدم /cancel."
        )
        return

    target    = query.data.split(":", 1)[1]
    questions = ctx.user_data.get("questions", [])
    if not questions:
        await query.edit_message_text("⚠️ انتهت الجلسة، أرسل الأسئلة مجدداً.")
        return

    chat_id         = query.message.chat_id if target == "SAME_CHAT" else target
    control_chat_id = query.message.chat_id

    ctx.user_data["send_target"] = chat_id
    ctx.user_data["last_sent"]   = 0
    ctx.user_data["sending"]     = True

    img_count = sum(1 for q in questions if q.get("image"))
    txt_count = len(questions) - img_count
    await query.edit_message_text(
        f"🚀 جاري الإرسال (0/{len(questions)})...\n"
        f"📝 نصية: {txt_count} | 🖼 مصوّرة: {img_count}"
    )
    try:
        success, failed = await send_polls(
            ctx.bot, chat_id, questions, ctx,
            progress_msg=query.message, control_chat_id=control_chat_id,
        )
        ctx.user_data["last_failed"] = failed
        summary = f"✅ تم الإرسال: {success}\n❌ فشل: {len(failed)}"
        await query.message.reply_text(summary)
        await _send_failed_file(ctx.bot, control_chat_id, failed)
    finally:
        ctx.user_data["sending"] = False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("myid",   cmd_myid))
    app.add_handler(CommandHandler("send",   cmd_send))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(CallbackQueryHandler(handle_destination, pattern=r"^dest:"))
    print("✅ البوت يعمل...")
    app.run_polling()


if __name__ == "__main__":
    main()
