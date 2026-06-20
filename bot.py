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


# دعم أكثر من أدمن:
#  - ADMIN_ID / ADMIN_IDS: متغيرات بيئة على Railway (يمكن وضع أكثر من آيدي
#    مفصولين بفاصلة في ADMIN_IDS لإضافة أشخاص جدد لاحقاً بدون تعديل الكود)
#  - 8693892771: تمت إضافته مباشرة حسب الطلب، يبقى مفعّلاً دائماً
ADMIN_IDS = _parse_ids("ADMIN_ID", "ADMIN_IDS") | {8693892771}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


CHANNEL_LABEL = "𝐏𝐬𝐞𝐮𝐝𝐨𝐬𝐜𝐢𝐞𝐧𝐜𝐞"
MAIN_CHANNEL  = os.environ.get("MAIN_CHANNEL", "@mj515678")
SEND_DELAY    = float(os.environ.get("SEND_DELAY", "0.5"))   # تأخير بين كل سؤال والتالي (مخفّض حسب الطلب)
BUFFER_DELAY  = float(os.environ.get("BUFFER_DELAY", "2"))   # مهلة تجميع الرسائل المقسومة تلقائياً
MAX_Q         = 300   # الحد الأقصى لطول نص السؤال داخل الاستطلاع (حد تيليجرام)
MAX_OPT       = 100   # الحد الأقصى لطول الخيار الواحد داخل الاستطلاع (حد تيليجرام)
TG_MSG_LIMIT  = 4096  # الحد الأقصى لطول رسالة تيليجرام الواحدة
TG_MAX_POLL_OPTIONS = 10  # حد تيليجرام لعدد خيارات الاستطلاع الواحد

DESTINATIONS = {
    "قناتي الرئيسية" : MAIN_CHANNEL,
    "نفس المحادثة"   : "SAME_CHAT",
}


# ---------------------------------------------------------------------------
# أدوات مساعدة عامة
# ---------------------------------------------------------------------------
def split_message(text, size=4000):
    """يقسّم نصاً طويلاً إلى أجزاء آمنة لإرسالها كرسائل تيليجرام منفصلة.
    يحاول القطع عند آخر سطر جديد ضمن الحد لتفادي تقطيع الكلمات في المنتصف."""
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
    """يستخرج النص من ملفات txt / docx / pdf."""
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

    # txt أو أي امتداد آخر: نتعامل معه كنص عادي
    return data.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# استخراج الأسئلة من النص
# ---------------------------------------------------------------------------
def extract_questions(text):
    """
    يستخرج الأسئلة من نص بصيغ متعددة:
      - ترقيم الأسئلة: Q1.  Question 1:  1.  1)  1-   أو بدون ترقيم إطلاقاً
      - رموز الخيارات: A. A) A-  وكذلك أحرف صغيرة a. a)   حتى الحرف J (٢-١٠ خيارات)
      - خيارات بسطر واحد: A. نص B. نص C. نص D. نص
      - صيغ الإجابة: Answer: B / Ans:B / Correct Answer: B / Correct=B
        أو حرف الإجابة وحده في سطر منفصل بعد الخيارات
      - مفتاح إجابات منفصل (Answer Key) عادة في نهاية النص بصيغة: 1-B  2-A  3-D ...
    """
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

    # --- المرحلة ١: اكتشاف مفتاح إجابات منفصل ---
    # نشترط ٣ أسطر مطابقة على الأقل حتى لا نخلط بين مفتاح حقيقي وأسئلة
    # مرقّمة قصيرة تتشابه صدفة مع صيغة "رقم-حرف"
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
        """ينظف نص الخيار من أي نقاط أو مسافات زائدة في نهايته فقط، بحيث
        'خيار .' و'خيار.' و'خيار' تُعامل كلها كنفس الخيار 'خيار'. هذا التنظيف
        يُطبَّق هنا في مكان واحد على كل الخيارات (سطر مستقل أو سطر واحد inline)
        بدل تكراره في كل مسار استخراج على حدة."""
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
        # لا توجد إجابة صريحة بجانب السؤال؟ جرّب مفتاح الإجابات المنفصل
        if ans is None and cur_num is not None and cur_num in answer_key:
            ans = ord(answer_key[cur_num]) - ord('A')
        if ans is not None and 2 <= len(cur_opts) <= TG_MAX_POLL_OPTIONS and ans < len(cur_opts):
            questions.append({
                "question": " ".join(cur_q).strip(),
                "options" : [clean_option_text(o) for o in cur_opts],
                "correct" : ans,
            })

    def reset():
        nonlocal cur_q, cur_opts, cur_ans, cur_opt_idx, cur_num
        cur_q, cur_opts, cur_ans, cur_opt_idx, cur_num = [], [], None, None, None

    for i, raw in enumerate(lines):
        if i in key_line_idx:
            continue  # سطر تابع لمفتاح الإجابات، تجاهله من تحليل الأسئلة

        line = raw.strip()

        if not line:
            # سطر فارغ بعد سؤال مكتمل (له إجابة) = فاصل بين سؤالين بدون ترقيم
            if cur_ans is not None and cur_opts:
                flush()
                reset()
            continue

        if ANSWER_KEY_HEADER.match(line):
            # عنوان قسم "مفتاح الإجابات" (Answer Key / Answers:) — أنهِ السؤال
            # الحالي إن وُجد، وتجاهل هذا السطر نفسه دون إلحاقه بأي خيار
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
            # سطر منفصل يحوي فقط حرف الإجابة بعد عرض كل الخيارات
            cur_ans = ord(line.upper()) - ord('A')
            continue

        if cur_opt_idx is not None and cur_ans is None:
            # تكملة لنص الخيار الحالي، أو خيارات إضافية مكتوبة بنفس السطر
            inline = parse_inline_options(line)
            if inline:
                cur_opts    = inline
                cur_opt_idx = len(cur_opts) - 1
            else:
                cur_opts[cur_opt_idx] += " " + line
            continue

        if cur_q and cur_ans is None and cur_opt_idx is None:
            # تكملة لنص السؤال نفسه (سطر إضافي قبل ظهور أي خيار)
            cur_q.append(line)
            continue

        # سؤال جديد بدون أي رقم أو رمز تعريف (السؤال الحالي إمّا مكتمل أو غير موجود)
        flush()
        auto_counter += 1
        cur_num = auto_counter
        cur_q   = [line]
        cur_opts, cur_ans, cur_opt_idx = [], None, None

    flush()
    return questions


# ---------------------------------------------------------------------------
# إرسال الأسئلة كاستطلاعات (Polls)
# ---------------------------------------------------------------------------
async def send_polls(bot, chat_id, questions, ctx, progress_msg=None,
                      start_index=0, control_chat_id=None):
    """
    يرسل الأسئلة كـ Quiz Polls إلى chat_id.

    - الإلغاء الفوري: يُتحقق من ctx.bot_data['cancel_flags'][control_chat_id]
      قبل كل سؤال وقبل كل محاولة إعادة إرسال، فيتوقف البوت فوراً عند /cancel
      بدل الانتظار حتى نهاية السؤال الحالي.
    - سؤال أطول من حد تيليجرام (300 حرف): يُرسل كاملاً كرسالة نصية مباشرة
      (بدون أي عنوان/هيدر)، ويُستبدل نص الاستطلاع بعبارة "Choose the correct answer".
    - خيار أطول من حد تيليجرام (100 حرف): يُرسل السؤال مع كل الخيارات كاملة
      في رسالة نصية واحدة (بدون عنوان)، ويُكتفى داخل الاستطلاع نفسه بحروف
      الخيارات فقط (A, B, C ...).
    - RetryAfter يُعالَج تلقائياً بالانتظار ثم إعادة المحاولة لنفس السؤال.
    - last_sent يُحفظ في ctx.user_data بعد كل سؤال ناجح لدعم /resume.
    """
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

        # --- معالجة تجاوز الحدود: نص السؤال أطول من 300، أو أي خيار أطول من 100 ---
        question_overflow = len(question_text) > MAX_Q
        options_overflow  = any(len(o) > MAX_OPT for o in options)

        poll_question = question_text
        poll_options  = list(options)

        if question_overflow or options_overflow:
            # رسالة نصية واحدة فقط، بدون أي عنوان/هيدر: السؤال كاملاً، ومعه
            # الخيارات كاملة أيضاً إن كانت هي السبب في تجاوز الحد
            full_text = question_text
            if options_overflow:
                letters = [chr(ord('A') + idx) for idx in range(len(options))]
                opts_block = "\n".join(f"{l}. {o}" for l, o in zip(letters, options))
                full_text = f"{question_text}\n\n{opts_block}"
                poll_options = letters

            for chunk in split_message(full_text):
                await bot.send_message(chat_id=chat_id, text=chunk)

            # الاستطلاع نفسه: يعرض نص السؤال الحقيقي إن كان يتسع ضمن الحد،
            # وإلا يُستبدل بعبارة مختصرة لأن النص الكامل أُرسل أعلاه بالفعل
            poll_question = question_text if not question_overflow else "Choose the correct answer"

        opts = [o[:MAX_OPT] for o in poll_options]
        if len(opts) < TG_MAX_POLL_OPTIONS:
            opts.append(CHANNEL_LABEL)  # علامة القناة، فقط إن وُجدت مساحة ضمن حد ١٠ خيارات

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
    """يحفظ الأسئلة الفاشلة في ملف failed.txt ويرسله للمستخدم."""
    if not failed:
        return
    lines = [f"#{item['index']}: {item['question']}" for item in failed]
    content = "\n\n".join(lines)
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = "failed.txt"
    await bot.send_document(chat_id=chat_id, document=buf, filename="failed.txt")


# ---------------------------------------------------------------------------
# أوامر البوت
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 أهلاً!\n\nأرسل لي الأسئلة نصاً أو ملف (txt / docx / pdf)\n\n"
        "تدعم صيغ ترقيم متعددة: Q1. / Question 1: / 1. / 1) / 1- / بدون ترقيم\n"
        "وصيغ خيارات متعددة: A. A) A- a. a) ... حتى J\n"
        "وصيغ إجابة متعددة: Answer: B / Ans:B / Correct=B / أو حرف منفرد\n"
        "كما يدعم مفتاح إجابات منفصل بصيغة: 1-B 2-A 3-D ...\n\n"
        "📌 لو النص طويل وانقسم تلقائياً لعدة رسائل، البوت يجمعها تلقائياً "
        "قبل المعالجة، فقط انتظر ثانيتين بعد آخر رسالة.\n\n"
        "أوامر مفيدة:\n"
        "/cancel — إيقاف الإرسال فوراً\n"
        "/resume — استئناف آخر عملية إرسال متوقفة\n"
        "/status — عرض حالة الاستخراج والإرسال الحالية\n"
        "/myid — معرفة آيديك على تيليجرام"
    )


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 آيديك: `{update.effective_user.id}`", parse_mode="Markdown"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """يلغي أي عملية تجميع نص جارية، ويوقف أي إرسال جارٍ فوراً قبل السؤال التالي."""
    if not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id

    timer = ctx.bot_data.get("flush_timers", {}).pop(chat_id, None)
    if timer:
        timer.cancel()
    ctx.bot_data.get("text_buffers", {}).pop(chat_id, None)

    # رفع علم الإلغاء: send_polls يتحقق منه قبل كل سؤال فيتوقف فوراً
    ctx.bot_data.setdefault("cancel_flags", {})[chat_id] = True

    ctx.user_data["sending"] = False
    await update.message.reply_text(
        "✅ تم إلغاء العملية الحالية فوراً.\n"
        "يمكنك إرسال أسئلة جديدة، أو استخدام /resume لاستكمال نفس الدفعة لاحقاً."
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """يعرض إجمالي الأسئلة المستخرجة، عدد المُرسل، عدد الفاشل، وهل هناك إرسال جارٍ."""
    if not is_admin(update.effective_user.id):
        return
    questions = ctx.user_data.get("questions", [])
    last_sent = ctx.user_data.get("last_sent", 0)
    failed    = ctx.user_data.get("last_failed", [])
    sending   = ctx.user_data.get("sending", False)
    await update.message.reply_text(
        "📊 الحالة الحالية:\n\n"
        f"📝 إجمالي الأسئلة المستخرجة: {len(questions)}\n"
        f"✅ تم إرسال: {last_sent}\n"
        f"❌ فشل: {len(failed)}\n"
        f"🚀 عملية إرسال جارية الآن: {'نعم' if sending else 'لا'}"
    )


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """يستأنف الإرسال من السؤال التالي مباشرة لآخر سؤال أُرسل بنجاح (last_sent)."""
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


def _merge_chunks(chunks):
    """يلصق أجزاء النص المرسلة على دفعات بأمان (بدون تغيير في المنطق الأصلي)."""
    merged = ""
    for i, chunk in enumerate(chunks):
        if i == 0:
            merged = chunk
            continue
        prev_was_split = len(chunks[i - 1]) >= TG_MSG_LIMIT
        merged += chunk if prev_was_split else "\n" + chunk
    return merged


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """يدعم الآن ملفات txt / docx / pdf (إضافة على الصيغة الأصلية txt فقط)."""
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

    # منع التكرار: لا تبدأ إرسالاً جديداً إن كان هناك إرسال جارٍ بالفعل لنفس المستخدم
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
    control_chat_id = query.message.chat_id  # الشات اللي يتحكم بالإلغاء/الاستئناف هو شات الأدمن دائماً

    ctx.user_data["send_target"] = chat_id
    ctx.user_data["last_sent"]   = 0
    ctx.user_data["sending"]     = True

    await query.edit_message_text(f"🚀 جاري الإرسال (0/{len(questions)})...")
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


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(CallbackQueryHandler(handle_destination, pattern=r"^dest:"))
    print("✅ البوت يعمل...")
    app.run_polling()


if __name__ == "__main__":
    main()
