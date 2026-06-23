"""
╔══════════════════════════════════════════════════════════════════╗
║           بوت الكويز — النسخة المثالية النهائية                ║
║  Zero-error · Maximum flexibility · Full Arabic/English support ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sys
import textwrap
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── اختياريات ──────────────────────────────────────────────────────────────
try:
    from docx import Document as DocxDocument
except ImportError:
    DocxDocument = None

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None

try:
    import pdfplumber  # أفضل من PyPDF2 لاستخراج النصوص
except ImportError:
    pdfplumber = None

# ═══════════════════════════════════════════════════════════════════════════
#  إعداد السجلات
# ═══════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("QuizBot")

# ═══════════════════════════════════════════════════════════════════════════
#  الإعدادات
# ═══════════════════════════════════════════════════════════════════════════
TOKEN: str = os.environ["BOT_TOKEN"]


def _parse_ids(*env_names: str) -> set[int]:
    ids: set[int] = set()
    for name in env_names:
        for part in os.environ.get(name, "").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
    return ids


ADMIN_IDS: set[int] = _parse_ids("ADMIN_ID", "ADMIN_IDS") | {8693892771}

CHANNEL_LABEL      = "𝐏𝐬𝐞𝐮𝐝𝐨𝐬𝐜𝐢𝐞𝐧𝐜𝐞"
MAIN_CHANNEL       = os.environ.get("MAIN_CHANNEL", "@mj515678")
SEND_DELAY         = float(os.environ.get("SEND_DELAY",        "0.5"))
BUFFER_DELAY       = float(os.environ.get("BUFFER_DELAY",      "2.0"))
IMAGE_BUFFER_DELAY = float(os.environ.get("IMAGE_BUFFER_DELAY","4.0"))

MAX_Q              = 300      # حد Telegram لنص السؤال
MAX_OPT            = 100      # حد Telegram للخيار
TG_MSG_LIMIT       = 4096
TG_MAX_POLL_OPTS   = 10
MAX_QUESTIONS      = 500      # حد واحدة قائمة لحماية الذاكرة
MAX_SAVED          = 2000     # حد المحفوظات

# وجهات الإرسال
SEND_DESTINATIONS: dict[str, str] = {
    "📡 قناتي الرئيسية": MAIN_CHANNEL,
    "💬 نفس المحادثة":   "SAME_CHAT",
}

# ═══════════════════════════════════════════════════════════════════════════
#  بنية السؤال
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Question:
    question: str
    options:  list[str]
    correct:  int
    image:    str | None = None
    explanation: str | None = None  # شرح الإجابة (اختياري)

    def is_valid(self) -> bool:
        return (
            bool(self.question.strip())
            and 2 <= len(self.options) <= TG_MAX_POLL_OPTS
            and 0 <= self.correct < len(self.options)
            and all(o.strip() for o in self.options)
        )

    def to_dict(self) -> dict:
        return {
            "question":    self.question,
            "options":     self.options,
            "correct":     self.correct,
            "image":       self.image,
            "explanation": self.explanation,
        }

    @staticmethod
    def from_dict(d: dict) -> "Question":
        return Question(
            question=    d.get("question", ""),
            options=     d.get("options",  []),
            correct=     d.get("correct",  0),
            image=       d.get("image"),
            explanation= d.get("explanation"),
        )


# ═══════════════════════════════════════════════════════════════════════════
#  أدوات النص
# ═══════════════════════════════════════════════════════════════════════════
def normalize_text(text: str) -> str:
    """تطبيع: توحيد المسافات، تنظيف Unicode، إزالة BOM."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u200b", "").replace("\ufeff", "")  # zero-width + BOM
    # توحيد الهمزات والألفات (اختياري لمطابقة أفضل)
    text = re.sub(r"[أإآ]", "ا", text)
    # توحيد الياء والتاء المربوطة
    text = re.sub(r"[ىة]$", lambda m: "ة" if m.group() == "ة" else "ي", text)
    return text


def clean_option(opt: str) -> str:
    """تنظيف نهاية الخيار من النقاط والمسافات الزائدة."""
    opt = opt.strip()
    opt = re.sub(r"\s*\.{2,}\s*$", "", opt)   # ... أو .. في النهاية
    opt = re.sub(r"\s*\.\s*$", "", opt)         # نقطة واحدة
    opt = re.sub(r"\s+", " ", opt)              # مسافات متعددة
    return opt.strip()


def split_message(text: str, size: int = 4000) -> list[str]:
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= size:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    return parts


def extract_text_from_file(filename: str, data: bytes) -> str:
    """استخراج النص من txt/docx/pdf مع دعم pdfplumber كخيار أفضل."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "txt"

    if ext == "docx":
        if DocxDocument is None:
            raise RuntimeError("python-docx غير مثبتة — أضفها لـ requirements.txt")
        doc = DocxDocument(io.BytesIO(data))
        lines: list[str] = []
        for para in doc.paragraphs:
            lines.append(para.text)
        # جداول docx
        for table in doc.tables:
            for row in table.rows:
                lines.append("\t".join(cell.text for cell in row.cells))
        return "\n".join(lines)

    if ext == "pdf":
        # الأولوية: pdfplumber > PyPDF2
        if pdfplumber is not None:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
            return "\n".join(pages)
        if PdfReader is not None:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join(
                (page.extract_text() or "") for page in reader.pages
            )
        raise RuntimeError("لا توجد مكتبة PDF — أضف pdfplumber أو PyPDF2")

    # txt (أو أي ملف نصي)
    for enc in ("utf-8", "utf-8-sig", "cp1256", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════════════════════
#  تحليل قيمة الإجابة
# ═══════════════════════════════════════════════════════════════════════════
# أنماط ALL / NONE / MULTI / حرف منفرد
_ALL_PAT = re.compile(
    r"^(?:"
    r"ALL(?:\s+OF\s+(?:THE\s+)?ABOVE)?"
    r"|كل\s*(?:ما\s*)?(?:سبق|ذكر)"
    r"|جميع\s*(?:ما\s*)?(?:سبق|ذكر)"
    r"|كلها|كلهم|جميعها|جميعهم"
    r"|كل\s*الخيارات?\s*(?:صحيحة|صحيح)?"
    r")$",
    re.I | re.U,
)
_NONE_PAT = re.compile(
    r"^(?:"
    r"NONE(?:\s+OF\s+(?:THE\s+)?ABOVE)?"
    r"|لا\s*شيء(?:\s*مما\s*(?:سبق|ذكر))?"
    r"|لا\s*(?:شيء|إجابة)\s*(?:من\s*)?(?:ما\s*)?(?:سبق|صحيحة?)?"
    r")$",
    re.I | re.U,
)
_MULTI_LETTER_PAT = re.compile(
    r"^[A-Ja-j](?:\s*(?:and|or|و|،|,|&|\+|/)\s*[A-Ja-j])+$",
    re.I | re.U,
)

AnswerValue = int | str | None   # int=0-9, "ALL", "NONE", "MULTI:XY", None


def parse_answer_value(raw: str) -> AnswerValue:
    """تحويل نص الإجابة الخام إلى قيمة داخلية."""
    s = raw.strip()

    if _ALL_PAT.match(s):
        return "ALL"
    if _NONE_PAT.match(s):
        return "NONE"

    # متعددة صريحة: B&D, A,C, A and C
    if _MULTI_LETTER_PAT.match(s):
        letters = re.findall(r"\b([A-Ja-j])\b", s, re.I)
        unique  = list(dict.fromkeys(l.upper() for l in letters))
        if len(unique) > 1:
            return f"MULTI:{''.join(unique)}"

    # حرف واحد فقط (word boundary لتجنب حروف ALL/NONE)
    letters = re.findall(r"\b([A-Ja-j])\b", s, re.I)
    if len(letters) == 1:
        return ord(letters[0].upper()) - ord("A")
    if len(letters) > 1:
        unique = list(dict.fromkeys(l.upper() for l in letters))
        return f"MULTI:{''.join(unique)}"

    return None


def build_multi_option(
    opts: list[str], letters: str, clean_fn=clean_option
) -> tuple[list[str], int]:
    """
    يبني خيار مدمج (B & D كلاهما صحيح) كآخر خيار.
    يعيد (new_options, correct_index).
    """
    label = " & ".join(letters)
    combo = f"({label}) كلاهما صحيح ✔"
    new_opts = [clean_fn(o) for o in opts] + [combo]
    return new_opts, len(new_opts) - 1


# ═══════════════════════════════════════════════════════════════════════════
#  محلّل الأسئلة الرئيسي
# ═══════════════════════════════════════════════════════════════════════════
# ── أنماط التعرف ────────────────────────────────────────────────────────
_Q_PAT = re.compile(
    r"^(?:Q(?:uestion|s?\.?)?\s*)?(\d+)\s*[.)\-:؟\s]\s*",
    re.I,
)
_OPT_PAT = re.compile(r"^([A-Ja-j])\s*[.):\-]\s*", re.I)
_ANS_KEYWORD = re.compile(
    r"^(?:"
    r"Correct\s*Answer|Answer|Answers?|Ans|Correct"
    r"|الإجابة\s*الصحيحة?|الإجابة|الجواب|الحل"
    r")\s*[:=\-]\s*",
    re.I | re.U,
)
_ANS_ONLY   = re.compile(r"^([A-Ja-j])\s*$", re.I)
_KEY_LINE   = re.compile(r"^(\d+)\s*[-.):\s]\s*([A-Ja-j])\s*$", re.I)
_KEY_HDR    = re.compile(
    r"^(?:answers?\s*key|answer\s*key|key|answers?|الإجابات?|مفتاح\s*الإجابات?)\s*:?\s*$",
    re.I | re.U,
)
_INLINE_OPT = re.compile(r"([A-Ja-j])\s*[.)]\s*(.*?)(?=\s+[A-Ja-j]\s*[.)]|$)", re.I)
_DIVIDER    = re.compile(r"^[-_=*#]{3,}\s*$")  # خطوط فاصلة

# تعرف على بداية سؤال جديد بدون رقم (Stem قائم بذاته)
_STEM_START = re.compile(
    r"^(?:Which|What|Who|When|Where|How|Why|Choose|Select|True|False|ما|من|أي|اختر|حدد|صح|خطأ)\b",
    re.I | re.U,
)

# شرح الإجابة
_EXPL_PAT = re.compile(
    r"^(?:Explanation|Rationale|Note|ملاحظة|الشرح|التفسير|السبب)\s*[:=\-]\s*",
    re.I | re.U,
)


def extract_questions(raw_text: str) -> list[Question]:
    """
    المحلّل الرئيسي — يدعم:
    • ترقيم الأسئلة (1. / Q1 / Q.1)
    • بدون ترقيم (stem مباشرة)
    • خيارات A) / A. / A- / (A)
    • إجابات: حرف / ALL / NONE / متعدد (B&D)
    • Answer Key منفصل في نهاية الملف
    • أسئلة متراكمة بدون فاصل
    • شرح الإجابة (Explanation:)
    • خيارات inline: A. opt1  B. opt2  C. opt3
    """
    text  = normalize_text(raw_text)
    lines = text.splitlines()

    # ── 1. كشف Answer Key منفصل ─────────────────────────────────────────
    answer_key: dict[int, str]  = {}
    key_lines:  set[int]        = set()
    in_key_section              = False

    for i, raw in enumerate(lines):
        s = raw.strip()
        if _KEY_HDR.match(s):
            in_key_section = True
            key_lines.add(i)
            continue
        if in_key_section:
            m = _KEY_LINE.match(s)
            if m:
                answer_key[int(m.group(1))] = m.group(2).upper()
                key_lines.add(i)
            elif s and not _DIVIDER.match(s):
                in_key_section = False  # خرجنا من قسم المفتاح

    # لا نعتمد المفتاح إلا إذا كان فيه 3 مدخلات على الأقل
    if len(key_lines) < 3:
        answer_key, key_lines = {}, set()

    # ── 2. تجميع السياق لكل سطر ─────────────────────────────────────────
    questions: list[Question]  = []
    cur_q:     list[str]       = []
    cur_opts:  list[str]       = []
    cur_ans:   AnswerValue     = None
    cur_expl:  str | None      = None
    cur_num:   int | None      = None
    cur_img:   str | None      = None
    opt_idx:   int             = -1
    auto_ctr:  int             = 0

    def _parse_inline(line: str) -> list[str]:
        m = _INLINE_OPT.findall(line)
        return [clean_option(t) for _, t in m] if len(m) >= 2 else []

    def flush():
        nonlocal cur_ans
        if not cur_q:
            return

        # استرجاع الإجابة من المفتاح إذا لم تُكتشف
        if cur_ans is None and cur_num is not None and cur_num in answer_key:
            cur_ans = ord(answer_key[cur_num]) - ord("A")

        if cur_ans is None or len(cur_opts) < 2:
            return

        cleaned = [clean_option(o) for o in cur_opts]
        q_text  = " ".join(cur_q).strip()

        def _add(opts: list[str], correct: int):
            if 2 <= len(opts) <= TG_MAX_POLL_OPTS and correct < len(opts):
                questions.append(Question(
                    question=    q_text,
                    options=     opts,
                    correct=     correct,
                    image=       cur_img,
                    explanation= cur_expl,
                ))

        if cur_ans == "ALL":
            _add(cleaned + ["All of the above ✔"], len(cleaned))
        elif cur_ans == "NONE":
            _add(cleaned + ["None of the above ✔"], len(cleaned))
        elif isinstance(cur_ans, str) and cur_ans.startswith("MULTI:"):
            new_opts, correct = build_multi_option(cur_opts, cur_ans[6:])
            _add(new_opts, correct)
        elif isinstance(cur_ans, int):
            _add(cleaned, cur_ans)

    def reset():
        nonlocal cur_q, cur_opts, cur_ans, cur_expl, cur_num, cur_img, opt_idx
        cur_q, cur_opts, cur_ans = [], [], None
        cur_expl, cur_num, cur_img, opt_idx = None, None, None, -1

    for i, raw in enumerate(lines):
        if i in key_lines:
            continue

        line = raw.strip()

        # سطر فارغ أو فاصل
        if not line or _DIVIDER.match(line):
            if cur_ans is not None and cur_opts:
                flush(); reset()
            continue

        # رأس Answer Key
        if _KEY_HDR.match(line):
            if cur_q:
                flush(); reset()
            continue

        # ── شرح الإجابة ──
        m_expl = _EXPL_PAT.match(line)
        if m_expl:
            cur_expl = line[m_expl.end():].strip()
            continue

        # ── سؤال برقم ──
        m_q = _Q_PAT.match(line)
        if m_q and not _ANS_KEYWORD.match(line):
            flush()
            auto_ctr += 1
            cur_num  = int(m_q.group(1))
            cur_q    = [line[m_q.end():].strip()]
            cur_opts, cur_ans, cur_expl, cur_img, opt_idx = [], None, None, None, -1
            continue

        # ── سطر الإجابة ──
        m_ans = _ANS_KEYWORD.match(line)
        if m_ans:
            cur_ans = parse_answer_value(line[m_ans.end():].strip())
            continue

        # ── خيار ──
        m_opt = _OPT_PAT.match(line)
        if m_opt:
            inline = _parse_inline(line)
            if inline:
                cur_opts = inline
                opt_idx  = len(cur_opts) - 1
            else:
                cur_opts.append(line[m_opt.end():].strip())
                opt_idx = len(cur_opts) - 1
            continue

        # ── حرف وحيد كإجابة (بعد خيارات وقبل إجابة صريحة) ──
        if opt_idx >= 0 and cur_ans is None and _ANS_ONLY.match(line):
            cur_ans = parse_answer_value(line)
            continue

        # ── استمرار الخيار الحالي ──
        if opt_idx >= 0 and cur_ans is None:
            inline = _parse_inline(line)
            if inline:
                cur_opts = inline
                opt_idx  = len(cur_opts) - 1
            else:
                cur_opts[opt_idx] += " " + line
            continue

        # ── استمرار نص السؤال ──
        if cur_q and cur_ans is None and opt_idx < 0:
            cur_q.append(line)
            continue

        # ── سؤال جديد بدون رقم (تعرف على الـ stem) ──
        if _STEM_START.match(line):
            flush()
            auto_ctr += 1
            cur_num  = auto_ctr
            cur_q    = [line]
            cur_opts, cur_ans, cur_expl, cur_img, opt_idx = [], None, None, None, -1
            continue

    flush()
    logger.info("✅ تم استخراج %d سؤال", len(questions))
    return questions


# ═══════════════════════════════════════════════════════════════════════════
#  محلّل خيارات الصور
# ═══════════════════════════════════════════════════════════════════════════
def extract_options_and_answer(text: str) -> tuple[list[str] | None, int | None]:
    """يحلل نص الخيارات والإجابة للأسئلة المصوّرة."""
    lines   = normalize_text(text).strip().splitlines()
    opts:   list[str]   = []
    raw_ans: str | None = None
    opt_idx: int        = -1

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        m_ans = _ANS_KEYWORD.match(line)
        if m_ans:
            raw_ans = line[m_ans.end():].strip()
            continue

        m_opt = _OPT_PAT.match(line)
        if m_opt:
            inline = _INLINE_OPT.findall(line)
            if len(inline) >= 2:
                opts    = [clean_option(t) for _, t in inline]
                opt_idx = len(opts) - 1
            else:
                opts.append(clean_option(line[m_opt.end():].strip()))
                opt_idx = len(opts) - 1
            continue

        if opt_idx >= 0 and raw_ans is None and _ANS_ONLY.match(line):
            raw_ans = line
            continue

        if opt_idx >= 0 and raw_ans is None:
            opts[opt_idx] += " " + line.strip()
            continue

    if len(opts) < 2 or raw_ans is None:
        return None, None

    ans = parse_answer_value(raw_ans)

    if ans == "ALL":
        new_opts = opts + ["All of the above ✔"]
        return new_opts, len(new_opts) - 1
    if ans == "NONE":
        new_opts = opts + ["None of the above ✔"]
        return new_opts, len(new_opts) - 1
    if isinstance(ans, str) and ans.startswith("MULTI:"):
        new_opts, correct = build_multi_option(opts, ans[6:])
        return new_opts, correct
    if isinstance(ans, int) and ans < len(opts):
        return [clean_option(o) for o in opts], ans

    return None, None


# ═══════════════════════════════════════════════════════════════════════════
#  إرسال الاستطلاعات
# ═══════════════════════════════════════════════════════════════════════════
async def send_polls(
    bot,
    chat_id:         Any,
    questions:       list[Question],
    ctx:             ContextTypes.DEFAULT_TYPE,
    progress_msg=    None,
    start_index:     int = 0,
    control_chat_id: Any = None,
) -> tuple[int, list[dict]]:
    """
    يرسل الأسئلة كاستطلاعات.
    يعيد (success_count, failed_list).
    """
    control_chat_id = control_chat_id or chat_id
    flags = ctx.bot_data.setdefault("cancel_flags", {})
    flags[control_chat_id] = False

    failed: list[dict] = []
    success = 0
    total   = len(questions)

    for i in range(start_index, total):
        if flags.get(control_chat_id):
            logger.info("إلغاء بواسطة المستخدم عند السؤال %d", i + 1)
            break

        q   = questions[i]
        qn  = i + 1

        if not q.is_valid():
            logger.warning("سؤال غير صالح #%d، تخطي", qn)
            failed.append({"index": qn, "question": q.question, "reason": "invalid"})
            continue

        poll_question = q.question
        poll_options  = list(q.options)

        # ── صورة ──
        if q.image:
            try:
                cap = q.question[:1024] or "اختر الإجابة الصحيحة 👆"
                await bot.send_photo(chat_id=chat_id, photo=q.image, caption=cap)
            except TelegramError as e:
                logger.warning("فشل إرسال الصورة: %s", e)
            poll_question = "اختر الإجابة الصحيحة 👆"

        # ── نص أو خيارات طويلة ──
        elif len(q.question) > MAX_Q or any(len(o) > MAX_OPT for o in q.options):
            full_text = q.question
            if any(len(o) > MAX_OPT for o in q.options):
                letters   = [chr(ord("A") + k) for k in range(len(q.options))]
                opts_blk  = "\n".join(f"{l}. {o}" for l, o in zip(letters, q.options))
                full_text = f"{q.question}\n\n{opts_blk}"
                poll_options = letters
            for chunk in split_message(full_text):
                try:
                    await bot.send_message(chat_id=chat_id, text=chunk)
                except TelegramError as e:
                    logger.warning("فشل إرسال الرسالة: %s", e)
            if len(q.question) > MAX_Q:
                poll_question = "Choose the correct answer 👆"

        # ── إضافة شعار القناة كخيار أخير ──
        opts = [o[:MAX_OPT] for o in poll_options]
        if len(opts) < TG_MAX_POLL_OPTS:
            opts.append(CHANNEL_LABEL[:MAX_OPT])

        # ── إرسال مع retry تلقائي ──
        sent_ok = False
        retries = 0
        while retries < 5:
            if flags.get(control_chat_id):
                break
            try:
                await bot.send_poll(
                    chat_id           = chat_id,
                    question          = poll_question[:MAX_Q],
                    options           = opts,
                    type              = "quiz",
                    correct_option_id = q.correct,
                    is_anonymous      = True,
                    explanation       = (q.explanation[:200] if q.explanation else None),
                )
                success += 1
                sent_ok = True
                break
            except RetryAfter as e:
                wait = e.retry_after + 1
                logger.info("RetryAfter %ds للسؤال #%d", wait, qn)
                await asyncio.sleep(wait)
                retries += 1
            except BadRequest as e:
                logger.error("BadRequest للسؤال #%d: %s", qn, e)
                failed.append({"index": qn, "question": q.question, "reason": str(e)})
                break
            except Forbidden as e:
                logger.error("Forbidden: %s — توقف الإرسال", e)
                failed.append({"index": qn, "question": q.question, "reason": str(e)})
                # لا فائدة من الاستمرار
                flags[control_chat_id] = True
                break
            except TelegramError as e:
                retries += 1
                logger.warning("TelegramError للسؤال #%d (محاولة %d): %s", qn, retries, e)
                await asyncio.sleep(2 ** retries)  # exponential back-off
            except Exception as e:
                logger.exception("خطأ غير متوقع للسؤال #%d", qn)
                failed.append({"index": qn, "question": q.question, "reason": str(e)})
                break

        if sent_ok:
            ctx.user_data["last_sent"] = qn
        elif not sent_ok and not flags.get(control_chat_id) and retries >= 5:
            failed.append({"index": qn, "question": q.question, "reason": "max_retries"})

        # تحديث التقدم كل 10 أسئلة أو آخر سؤال
        if progress_msg and (qn % 10 == 0 or qn == total):
            try:
                pct = int(qn / total * 100)
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                await progress_msg.edit_text(
                    f"🚀 [{bar}] {pct}%\n"
                    f"({qn}/{total}) — ✅ {success} | ❌ {len(failed)}"
                )
            except TelegramError:
                pass

        # إرسال شرح الإجابة كرسالة منفصلة إذا وُجد ولم يُرسل مع الـ poll
        if q.explanation and len(q.explanation) > 200:
            try:
                await bot.send_message(
                    chat_id = chat_id,
                    text    = f"💡 *شرح الإجابة:*\n{q.explanation}",
                    parse_mode = "Markdown",
                )
            except TelegramError:
                pass

        await asyncio.sleep(SEND_DELAY)

    flags[control_chat_id] = False
    logger.info("إرسال منتهٍ: ✅ %d | ❌ %d", success, len(failed))
    return success, failed


async def send_failed_file(bot, chat_id: Any, failed: list[dict]):
    if not failed:
        return
    lines = [
        f"#{item['index']}: {item['question']}\n"
        f"   السبب: {item.get('reason', 'unknown')}"
        for item in failed
    ]
    content = "\n\n".join(lines)
    buf      = io.BytesIO(content.encode("utf-8"))
    buf.name = "failed_questions.txt"
    try:
        await bot.send_document(
            chat_id  = chat_id,
            document = buf,
            filename = "failed_questions.txt",
            caption  = f"📋 قائمة {len(failed)} سؤال فاشل",
        )
    except TelegramError as e:
        logger.warning("فشل إرسال ملف الفاشلة: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
#  لوحات المفاتيح
# ═══════════════════════════════════════════════════════════════════════════
def dest_keyboard(prefix: str = "dest") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:{target}")]
        for label, target in SEND_DESTINATIONS.items()
    ]
    if prefix == "dest":
        rows.append([InlineKeyboardButton("📥 حفظ في المحفوظات", callback_data=f"{prefix}:SAVE")])
    rows.append([InlineKeyboardButton("🚫 إلغاء", callback_data=f"{prefix}:CANCEL")])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(action: str, yes_label: str = "✅ نعم", no_label: str = "❌ لا") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(yes_label, callback_data=f"confirm:{action}:yes"),
        InlineKeyboardButton(no_label,  callback_data=f"confirm:{action}:no"),
    ]])


# ═══════════════════════════════════════════════════════════════════════════
#  الحماية من التزامن
# ═══════════════════════════════════════════════════════════════════════════
_send_locks: dict[int, asyncio.Lock] = {}

def get_send_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _send_locks:
        _send_locks[chat_id] = asyncio.Lock()
    return _send_locks[chat_id]


# ═══════════════════════════════════════════════════════════════════════════
#  معالجة الصور
# ═══════════════════════════════════════════════════════════════════════════
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    photo   = update.message.photo[-1]   # أعلى دقة
    file_id = photo.file_id
    caption = (update.message.caption or "").strip()

    # إذا كان الـ caption يحتوي خيارات + إجابة
    if caption:
        opts, ans = extract_options_and_answer(caption)
        if opts and ans is not None:
            q_text = _extract_question_from_caption(caption)
            await _queue_image_question(update, ctx, file_id, q_text, opts, ans)
            return

    # احتفظ بالصورة وانتظر الرسالة التالية
    ctx.bot_data.setdefault("pending_images", {})[chat_id] = {
        "file_id": file_id,
        "caption": caption,
    }
    _reset_image_timer(ctx, chat_id, update)
    await update.message.reply_text(
        "📸 *استلمت الصورة!*\n\n"
        "أرسل الآن الخيارات والإجابة بالصيغة:\n"
        "```\nA. الخيار الأول\nB. الخيار الثاني\nC. الخيار الثالث\nD. الخيار الرابع\nAnswer: B\n```",
        parse_mode="Markdown",
    )


def _extract_question_from_caption(caption: str) -> str:
    q_lines = []
    for line in caption.splitlines():
        line = line.strip()
        if not line:
            continue
        if _OPT_PAT.match(line) or _ANS_KEYWORD.match(line):
            break
        q_lines.append(line)
    return " ".join(q_lines).strip() or "اختر الإجابة الصحيحة"


def _reset_image_timer(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, update: Update):
    timers = ctx.bot_data.setdefault("image_timers", {})
    old = timers.get(chat_id)
    if old:
        old.cancel()

    async def _timeout():
        try:
            await asyncio.sleep(IMAGE_BUFFER_DELAY)
        except asyncio.CancelledError:
            return
        if ctx.bot_data.get("pending_images", {}).pop(chat_id, None):
            try:
                await ctx.bot.send_message(
                    chat_id,
                    "⏰ انتهى وقت الانتظار.\n"
                    "أرسل الصورة مجدداً مع الخيارات في الـ caption، أو أرسل الصورة ثم الخيارات مباشرة.",
                )
            except TelegramError:
                pass

    timers[chat_id] = asyncio.create_task(_timeout())


async def _queue_image_question(
    update, ctx, file_id: str, question: str,
    opts: list[str], ans: int,
):
    q = Question(
        question=question or "اختر الإجابة الصحيحة",
        options= opts,
        correct= ans,
        image=   file_id,
    )
    existing: list[Question] = ctx.user_data.get("questions", [])

    if len(existing) >= MAX_QUESTIONS:
        await update.message.reply_text(
            f"⚠️ وصلت للحد الأقصى ({MAX_QUESTIONS} سؤال). استخدم /send أو /clear أولاً."
        )
        return

    if not existing:
        ctx.user_data.update({
            "questions":   [q],
            "last_sent":   0,
            "last_failed": [],
            "sending":     False,
            "send_target": None,
        })
        await update.message.reply_text(
            "✅ تم استخراج سؤال مصوّر *(1 سؤال)*\n\n📤 أين تريد الإرسال؟",
            reply_markup = dest_keyboard(),
            parse_mode   = "Markdown",
        )
    else:
        existing.append(q)
        ctx.user_data["questions"] = existing
        await update.message.reply_text(
            f"✅ تمت إضافة السؤال المصوّر. الإجمالي: *{len(existing)}*\n"
            "استخدم /send للإرسال أو تابع إضافة المزيد.",
            parse_mode = "Markdown",
        )


async def handle_text_after_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    pending = ctx.bot_data.get("pending_images", {}).get(chat_id)
    if not pending:
        return False

    text = (update.message.text or "").strip()
    opts, ans = extract_options_and_answer(text)
    if opts is None:
        return False

    # إلغاء المؤقت
    timer = ctx.bot_data.get("image_timers", {}).pop(chat_id, None)
    if timer:
        timer.cancel()
    ctx.bot_data["pending_images"].pop(chat_id, None)

    file_id = pending["file_id"]
    q_text  = pending.get("caption", "").strip() or "اختر الإجابة الصحيحة"
    await _queue_image_question(update, ctx, file_id, q_text, opts, ans)
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  أوامر البوت
# ═══════════════════════════════════════════════════════════════════════════
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")
        return
    await update.message.reply_text(
        "👋 *أهلاً بك في بوت الكويز المثالي!*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 *طرق الإدخال:*\n"
        "📝 نص مباشر (كل الصيغ مدعومة)\n"
        "📎 ملف: `txt` / `docx` / `pdf`\n"
        "🖼 صورة + خيارات في نفس الرسالة أو بعدها\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📌 *صيغ الخيارات:*\n"
        "`A.` أو `A)` أو `A-` أو `(A)`\n\n"
        "📌 *صيغ الإجابة:*\n"
        "`Answer: B` · `Ans=C` · `Correct: D`\n"
        "`الإجابة: A` · حرف منفرد\n"
        "`Answer: B&D` · `Answer: ALL` · `Answer: NONE`\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📌 *الشرح (اختياري):*\n"
        "`Explanation: نص الشرح`\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📦 *وجهات الإرسال:*\n"
        "📡 قناتي الرئيسية\n"
        "💬 نفس المحادثة\n"
        "📥 المحفوظات (للإرسال لاحقاً)\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔧 *الأوامر:*\n"
        "/send — إرسال الأسئلة الحالية\n"
        "/preview — معاينة أول 3 أسئلة\n"
        "/saved — عرض المحفوظات\n"
        "/sendsaved — إرسال المحفوظات\n"
        "/clearsaved — مسح المحفوظات\n"
        "/cancel — إيقاف الإرسال\n"
        "/resume — استئناف آخر عملية\n"
        "/status — الحالة الحالية\n"
        "/stats — إحصائيات مفصّلة\n"
        "/clear — مسح الأسئلة الحالية\n"
        "/myid — معرفة آيديك\n"
        "/delay [ثواني] — ضبط التأخير بين الأسئلة\n"
        "/test — إرسال سؤال اختبار",
        parse_mode = "Markdown",
    )


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"🆔 آيديك: `{uid}`\n"
        f"{'✅ أنت مشرف' if is_admin(uid) else '⛔ لست مشرفاً'}",
        parse_mode = "Markdown",
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id

    for store in ("flush_timers", "image_timers"):
        t = ctx.bot_data.get(store, {}).pop(chat_id, None)
        if t:
            t.cancel()

    ctx.bot_data.get("text_buffers",   {}).pop(chat_id, None)
    ctx.bot_data.get("pending_images", {}).pop(chat_id, None)
    ctx.bot_data.setdefault("cancel_flags", {})[chat_id] = True
    ctx.user_data["sending"] = False

    await update.message.reply_text(
        "✅ تم إلغاء العملية الحالية.\n"
        "استخدم /resume لاستكمال نفس الدفعة، أو أرسل أسئلة جديدة."
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
    pending   = chat_id in ctx.bot_data.get("pending_images", {})
    delay     = ctx.user_data.get("send_delay", SEND_DELAY)

    img_q     = sum(1 for q in questions if (q.get("image") if isinstance(q, dict) else q.image))
    saved_img = sum(1 for q in saved if (q.get("image") if isinstance(q, dict) else q.image))

    await update.message.reply_text(
        "📊 *الحالة الحالية:*\n\n"
        f"📝 أسئلة جاهزة: *{len(questions)}*\n"
        f"   • نصية: {len(questions) - img_q} | مصوّرة: {img_q}\n"
        f"✅ تم إرساله: {last_sent}\n"
        f"❌ فشل: {len(failed)}\n\n"
        f"📥 *المحفوظات:* {len(saved)} سؤال\n"
        f"   • نصية: {len(saved) - saved_img} | مصوّرة: {saved_img}\n\n"
        f"🚀 إرسال جارٍ: {'نعم ⏳' if sending else 'لا'}\n"
        f"📸 صورة معلّقة: {'نعم ⏳' if pending else 'لا'}\n"
        f"⏱ التأخير الحالي: {delay}s",
        parse_mode = "Markdown",
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """إحصائيات مفصّلة."""
    if not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    session = ctx.bot_data.get("session_stats", {}).get(chat_id, {})
    total_sent   = session.get("total_sent",   0)
    total_failed = session.get("total_failed", 0)
    sessions     = session.get("sessions",     0)

    await update.message.reply_text(
        "📈 *إحصائيات الجلسة:*\n\n"
        f"📤 إجمالي المُرسل: {total_sent}\n"
        f"❌ إجمالي الفاشل: {total_failed}\n"
        f"🔄 عدد عمليات الإرسال: {sessions}\n"
        f"📊 معدل النجاح: "
        f"{total_sent / max(total_sent + total_failed, 1) * 100:.1f}%",
        parse_mode = "Markdown",
    )


async def cmd_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """معاينة أول 3 أسئلة."""
    if not is_admin(update.effective_user.id):
        return
    questions = ctx.user_data.get("questions", [])
    if not questions:
        await update.message.reply_text("⚠️ لا توجد أسئلة حالياً.")
        return
    lines = [f"👁 *معاينة أول {min(3, len(questions))} أسئلة:*\n"]
    for i, q in enumerate(questions[:3], 1):
        qd = q if isinstance(q, dict) else q.to_dict()
        opts_text = "\n".join(
            f"{'✅' if j == qd['correct'] else '  '} {chr(65+j)}. {o}"
            for j, o in enumerate(qd["options"])
        )
        img_mark = " 🖼" if qd.get("image") else ""
        lines.append(f"*{i}.{img_mark}* {qd['question'][:100]}\n{opts_text}")
        if qd.get("explanation"):
            lines.append(f"💡 _{qd['explanation'][:80]}_")
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
    lock = get_send_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("⚠️ عملية إرسال جارية بالفعل.")
        return

    async with lock:
        ctx.bot_data.setdefault("cancel_flags", {})[chat_id] = False
        ctx.user_data["sending"] = True
        msg = await update.message.reply_text(
            f"🔄 استئناف من السؤال *{last_sent + 1}*...", parse_mode="Markdown"
        )
        q_objs = _ensure_question_objects(questions)
        try:
            success, failed = await send_polls(
                ctx.bot, target, q_objs, ctx,
                progress_msg=msg, start_index=last_sent, control_chat_id=chat_id,
            )
            ctx.user_data["last_failed"] = failed
            _update_stats(ctx, chat_id, success, len(failed))
            await msg.reply_text(f"✅ تم الإرسال: {success}\n❌ فشل: {len(failed)}")
            await send_failed_file(ctx.bot, chat_id, failed)
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
        await update.message.reply_text(
            "⚠️ لا توجد أسئلة حالياً.\nأرسل نصاً أو ملفاً، أو استخدم /sendsaved."
        )
        return
    img_c = sum(1 for q in questions if (q.get("image") if isinstance(q, dict) else q.image))
    await update.message.reply_text(
        f"📤 لديك *{len(questions)}* سؤال جاهز\n"
        f"_(نصية: {len(questions) - img_c} | مصوّرة: {img_c})_\n\n"
        "📍 أين تريد الإرسال؟",
        reply_markup = dest_keyboard("dest"),
        parse_mode   = "Markdown",
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ لا يمكن المسح أثناء الإرسال. استخدم /cancel أولاً.")
        return
    count = len(ctx.user_data.get("questions", []))
    if count == 0:
        await update.message.reply_text("📭 القائمة فارغة بالفعل.")
        return
    ctx.user_data.update({
        "questions": [], "last_sent": 0,
        "last_failed": [], "send_target": None,
    })
    await update.message.reply_text(f"🗑 تم مسح *{count}* سؤال.", parse_mode="Markdown")


async def cmd_delay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ضبط التأخير بين الأسئلة: /delay 1.5"""
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args
    if not args:
        current = ctx.user_data.get("send_delay", SEND_DELAY)
        await update.message.reply_text(
            f"⏱ التأخير الحالي: *{current}s*\n\n"
            "لتغييره: `/delay 1.5` (بين 0.3 و 10 ثوانٍ)",
            parse_mode="Markdown",
        )
        return
    try:
        val = float(args[0])
        if not 0.3 <= val <= 10:
            raise ValueError
        ctx.user_data["send_delay"] = val
        # تحديث SEND_DELAY للجلسة
        global SEND_DELAY
        SEND_DELAY = val
        await update.message.reply_text(f"✅ تم ضبط التأخير على *{val}s*", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("⚠️ قيمة غير صالحة. مثال: `/delay 1.0`", parse_mode="Markdown")


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """إرسال سؤال اختبار للتحقق من عمل البوت."""
    if not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    try:
        await ctx.bot.send_poll(
            chat_id           = chat_id,
            question          = "🧪 هذا سؤال اختبار — البوت يعمل بشكل صحيح!",
            options           = ["✅ الإجابة الصحيحة", "❌ خاطئة", "❌ خاطئة", CHANNEL_LABEL],
            type              = "quiz",
            correct_option_id = 0,
            is_anonymous      = True,
            explanation       = "هذا مجرد اختبار للتأكد من عمل البوت 🤖",
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل الاختبار: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  أوامر المحفوظات
# ═══════════════════════════════════════════════════════════════════════════
async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    saved   = ctx.bot_data.get("saved_questions", {}).get(chat_id, [])
    if not saved:
        await update.message.reply_text("📭 المحفوظات فارغة حالياً.")
        return
    img_c   = sum(1 for q in saved if (q.get("image") if isinstance(q, dict) else q.image))
    preview = []
    for i, q in enumerate(saved[:5], 1):
        qd   = q if isinstance(q, dict) else q.to_dict()
        icon = "🖼" if qd.get("image") else "📝"
        txt  = qd["question"][:60] + ("…" if len(qd["question"]) > 60 else "")
        preview.append(f"{i}. {icon} {txt}")
    more = f"\n_…و {len(saved) - 5} سؤال آخر_" if len(saved) > 5 else ""
    await update.message.reply_text(
        f"📥 *المحفوظات:* {len(saved)} سؤال\n"
        f"_(نصية: {len(saved) - img_c} | مصوّرة: {img_c})_\n\n"
        + "\n".join(preview) + more
        + "\n\nاستخدم /sendsaved للإرسال أو /clearsaved للمسح.",
        parse_mode="Markdown",
    )


async def cmd_sendsaved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ هناك عملية إرسال جارية.")
        return
    chat_id = update.effective_chat.id
    saved   = ctx.bot_data.get("saved_questions", {}).get(chat_id, [])
    if not saved:
        await update.message.reply_text("📭 المحفوظات فارغة.")
        return
    img_c = sum(1 for q in saved if (q.get("image") if isinstance(q, dict) else q.image))
    await update.message.reply_text(
        f"📤 إرسال *{len(saved)}* سؤال من المحفوظات\n"
        f"_(نصية: {len(saved) - img_c} | مصوّرة: {img_c})_\n\n"
        "📍 أين تريد الإرسال؟",
        reply_markup = dest_keyboard("saved_dest"),
        parse_mode   = "Markdown",
    )


async def cmd_clearsaved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if ctx.user_data.get("sending"):
        await update.message.reply_text("⚠️ لا يمكن المسح أثناء الإرسال.")
        return
    chat_id = update.effective_chat.id
    saved   = ctx.bot_data.setdefault("saved_questions", {})
    count   = len(saved.get(chat_id, []))
    if count == 0:
        await update.message.reply_text("📭 المحفوظات فارغة بالفعل.")
        return
    saved[chat_id] = []
    await update.message.reply_text(f"🗑 تم مسح *{count}* سؤال من المحفوظات.", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════
#  معالجة النص والملفات
# ═══════════════════════════════════════════════════════════════════════════
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    # أولوية: هل الرسالة خيارات لصورة معلّقة؟
    if await handle_text_after_image(update, ctx):
        return

    chat_id = update.effective_chat.id
    buffers = ctx.bot_data.setdefault("text_buffers", {})
    timers  = ctx.bot_data.setdefault("flush_timers", {})

    buffers.setdefault(chat_id, []).append(update.message.text or "")

    old = timers.get(chat_id)
    if old:
        old.cancel()

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


def _merge_chunks(chunks: list[str]) -> str:
    if not chunks:
        return ""
    merged = chunks[0]
    for i in range(1, len(chunks)):
        prev_full = len(chunks[i - 1]) >= TG_MSG_LIMIT - 100
        merged   += chunks[i] if prev_full else "\n" + chunks[i]
    return merged


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    doc      = update.message.document
    filename = doc.file_name or "file.txt"
    ext      = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext not in ("txt", "docx", "pdf"):
        await update.message.reply_text(
            "⚠️ الصيغ المدعومة: `txt` · `docx` · `pdf`", parse_mode="Markdown"
        )
        return
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ حجم الملف يتجاوز 20MB.")
        return

    prog = await update.message.reply_text("⏳ جارٍ قراءة الملف…")
    try:
        tg_file = await doc.get_file()
        data    = bytes(await tg_file.download_as_bytearray())
        text    = extract_text_from_file(filename, data)
    except Exception as e:
        await prog.edit_text(f"⚠️ تعذّر قراءة الملف: {e}")
        return

    await prog.delete()
    await _process(update, ctx, text)


async def _process(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    questions = extract_questions(text)
    if not questions:
        await update.message.reply_text(
            "⚠️ لم أجد أسئلة في النص.\n\n"
            "تأكد من الصيغة:\n"
            "```\n1. نص السؤال\nA. خيار 1\nB. خيار 2\nAnswer: A\n```",
            parse_mode="Markdown",
        )
        return

    existing: list = ctx.user_data.get("questions", [])

    # حد أقصى للقائمة
    remaining = MAX_QUESTIONS - len(existing)
    if remaining <= 0:
        await update.message.reply_text(
            f"⚠️ وصلت للحد الأقصى ({MAX_QUESTIONS} سؤال). استخدم /send أو /clear أولاً."
        )
        return
    if len(questions) > remaining:
        questions = questions[:remaining]
        await update.message.reply_text(
            f"⚠️ تم اقتطاع القائمة إلى {remaining} سؤال (الحد الأقصى {MAX_QUESTIONS})."
        )

    if existing and not ctx.user_data.get("send_target"):
        all_q = existing + questions
        ctx.user_data["questions"] = all_q
        await update.message.reply_text(
            f"✅ تمت إضافة *{len(questions)}* سؤال. الإجمالي: *{len(all_q)}*\n\n"
            "استخدم /send للإرسال أو /preview للمعاينة.",
            parse_mode="Markdown",
        )
        return

    ctx.user_data.update({
        "questions":   questions,
        "last_sent":   0,
        "last_failed": [],
        "sending":     False,
        "send_target": None,
    })
    await update.message.reply_text(
        f"✅ تم استخراج *{len(questions)}* سؤال\n\n📤 أين تريد الإرسال؟",
        reply_markup = dest_keyboard("dest"),
        parse_mode   = "Markdown",
    )


# ═══════════════════════════════════════════════════════════════════════════
#  معالجة الأزرار
# ═══════════════════════════════════════════════════════════════════════════
async def handle_destination(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    action = query.data.split(":", 1)[1]

    if action == "CANCEL":
        await query.edit_message_text("🚫 تم الإلغاء.")
        return

    if action == "SAVE":
        questions = ctx.user_data.get("questions", [])
        if not questions:
            await query.edit_message_text("⚠️ انتهت الجلسة، أرسل الأسئلة مجدداً.")
            return
        chat_id = query.message.chat_id
        saved   = ctx.bot_data.setdefault("saved_questions", {})
        cur     = saved.get(chat_id, [])
        if len(cur) + len(questions) > MAX_SAVED:
            await query.edit_message_text(
                f"⚠️ المحفوظات ممتلئة ({MAX_SAVED} سؤال كحد أقصى). استخدم /clearsaved أولاً."
            )
            return
        saved.setdefault(chat_id, []).extend(questions)
        total = len(saved[chat_id])
        ctx.user_data.update({
            "questions": [], "last_sent": 0,
            "last_failed": [], "send_target": None,
        })
        await query.edit_message_text(
            f"📥 تم حفظ *{len(questions)}* سؤال!\n"
            f"إجمالي المحفوظات: *{total}* سؤال\n\n"
            "استخدم /sendsaved للإرسال لاحقاً.",
            parse_mode="Markdown",
        )
        return

    # ── إرسال مباشر ──
    if ctx.user_data.get("sending"):
        await query.message.reply_text("⚠️ هناك عملية إرسال جارية.")
        return

    questions = ctx.user_data.get("questions", [])
    if not questions:
        await query.edit_message_text("⚠️ انتهت الجلسة، أرسل الأسئلة مجدداً.")
        return

    chat_id         = query.message.chat_id if action == "SAME_CHAT" else action
    control_chat_id = query.message.chat_id

    lock = get_send_lock(control_chat_id)
    if lock.locked():
        await query.message.reply_text("⚠️ عملية إرسال جارية.")
        return

    ctx.user_data.update({
        "send_target": chat_id,
        "last_sent":   0,
        "sending":     True,
    })
    q_objs  = _ensure_question_objects(questions)
    img_c   = sum(1 for q in q_objs if q.image)
    await query.edit_message_text(
        f"🚀 جاري الإرسال (0/{len(q_objs)})…\n"
        f"📝 نصية: {len(q_objs) - img_c} | 🖼 مصوّرة: {img_c}"
    )

    async with lock:
        try:
            success, failed = await send_polls(
                ctx.bot, chat_id, q_objs, ctx,
                progress_msg=query.message, control_chat_id=control_chat_id,
            )
            ctx.user_data["last_failed"] = failed
            _update_stats(ctx, control_chat_id, success, len(failed))
            await query.message.reply_text(
                f"✅ تم الإرسال: *{success}*\n❌ فشل: *{len(failed)}*",
                parse_mode="Markdown",
            )
            await send_failed_file(ctx.bot, control_chat_id, failed)
        finally:
            ctx.user_data["sending"] = False


async def handle_saved_destination(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    action = query.data.split(":", 1)[1]

    if action == "CANCEL":
        await query.edit_message_text("🚫 تم الإلغاء.")
        return

    if ctx.user_data.get("sending"):
        await query.message.reply_text("⚠️ هناك عملية إرسال جارية.")
        return

    chat_id         = query.message.chat_id
    saved           = ctx.bot_data.get("saved_questions", {}).get(chat_id, [])
    if not saved:
        await query.edit_message_text("📭 المحفوظات فارغة.")
        return

    dest            = chat_id if action == "SAME_CHAT" else action
    control_chat_id = chat_id

    lock = get_send_lock(control_chat_id)
    if lock.locked():
        await query.message.reply_text("⚠️ عملية إرسال جارية.")
        return

    q_objs = _ensure_question_objects(list(saved))
    ctx.user_data.update({
        "questions":   q_objs,
        "last_sent":   0,
        "last_failed": [],
        "sending":     True,
        "send_target": dest,
    })
    ctx.bot_data.setdefault("cancel_flags", {})[control_chat_id] = False

    img_c = sum(1 for q in q_objs if q.image)
    await query.edit_message_text(
        f"🚀 جاري إرسال المحفوظات (0/{len(q_objs)})…\n"
        f"📝 نصية: {len(q_objs) - img_c} | 🖼 مصوّرة: {img_c}"
    )

    async with lock:
        try:
            success, failed = await send_polls(
                ctx.bot, dest, q_objs, ctx,
                progress_msg=query.message, control_chat_id=control_chat_id,
            )
            ctx.user_data["last_failed"] = failed
            _update_stats(ctx, chat_id, success, len(failed))

            if not failed:
                ctx.bot_data["saved_questions"][chat_id] = []
                note = "\n🗑 تم مسح المحفوظات تلقائياً بعد الإرسال الكامل."
            else:
                failed_indices = {f["index"] - 1 for f in failed}
                ctx.bot_data["saved_questions"][chat_id] = [
                    q for idx, q in enumerate(q_objs) if idx in failed_indices
                ]
                note = f"\n⚠️ تم الاحتفاظ بـ {len(failed)} سؤال فاشل في المحفوظات."

            await query.message.reply_text(
                f"✅ تم الإرسال: *{success}*\n❌ فشل: *{len(failed)}*" + note,
                parse_mode="Markdown",
            )
            await send_failed_file(ctx.bot, control_chat_id, failed)
        finally:
            ctx.user_data["sending"] = False


async def handle_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """معالجة أزرار التأكيد (confirm:action:yes/no)."""
    query = update.callback_query
    await query.answer()
    _, action, choice = query.data.split(":", 2)
    if choice == "no":
        await query.edit_message_text("🚫 تم الإلغاء.")


# ═══════════════════════════════════════════════════════════════════════════
#  أدوات مساعدة داخلية
# ═══════════════════════════════════════════════════════════════════════════
def _ensure_question_objects(qs: list) -> list[Question]:
    """تحويل dicts قديمة إلى Question objects إذا لزم."""
    result = []
    for q in qs:
        if isinstance(q, Question):
            result.append(q)
        elif isinstance(q, dict):
            result.append(Question.from_dict(q))
        else:
            logger.warning("نوع سؤال غير معروف: %s", type(q))
    return result


def _update_stats(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, success: int, failed: int):
    stats = ctx.bot_data.setdefault("session_stats", {})
    s     = stats.setdefault(chat_id, {"total_sent": 0, "total_failed": 0, "sessions": 0})
    s["total_sent"]   += success
    s["total_failed"] += failed
    s["sessions"]     += 1


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # أوامر
    for cmd, fn in [
        ("start",      cmd_start),
        ("help",       cmd_start),
        ("cancel",     cmd_cancel),
        ("status",     cmd_status),
        ("stats",      cmd_stats),
        ("resume",     cmd_resume),
        ("myid",       cmd_myid),
        ("send",       cmd_send),
        ("clear",      cmd_clear),
        ("preview",    cmd_preview),
        ("saved",      cmd_saved),
        ("sendsaved",  cmd_sendsaved),
        ("clearsaved", cmd_clearsaved),
        ("delay",      cmd_delay),
        ("test",       cmd_test),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # رسائل
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL,            handle_file))

    # أزرار
    app.add_handler(CallbackQueryHandler(handle_destination,       pattern=r"^dest:"))
    app.add_handler(CallbackQueryHandler(handle_saved_destination, pattern=r"^saved_dest:"))
    app.add_handler(CallbackQueryHandler(handle_confirm,           pattern=r"^confirm:"))

    logger.info("✅ البوت يعمل — جاهز للاستقبال...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
