import io
import base64
import logging
import os
import asyncio
import re

import httpx

logger = logging.getLogger("AIExtractor")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]

MCQ_PROMPT = """You are a world-class MCQ extraction expert specialized in medical education content.

Your ONLY job: extract EVERY SINGLE question with its options and identify the correct answer with 100% accuracy.

CRITICAL: Extract ALL questions visible on this page. Missing even one question is a failure.

=== HOW TO DETECT THE CORRECT ANSWER ===
- Circle drawn around a letter or option
- Highlighted or colored option (any color)
- Checkmark ✓ or tick mark
- Filled bubble ● (vs empty ○)
- Strikethrough ✗ on WRONG options (correct = NOT crossed)
- Any mark, dot, star near an option
- Answer: X / Ans: X / Correct: X

IF NO SIGNAL EXISTS: use your medical knowledge.

=== EXTRACTION RULES ===
- Copy question text EXACTLY as written — zero modifications
- Copy every option EXACTLY as written
- Handle 2-column layouts: read a+c on left, b+d on right
- Include full clinical vignette text if present

=== OUTPUT FORMAT ===
Q1. [full question text]
A. [option a]
B. [option b]
C. [option c]
D. [option d]
Answer: [letter]

=== RULES ===
- Output NOTHING before Q1
- Output NOTHING after last answer
- No comments, no notes, no markdown
- If no questions found: output NO_QUESTIONS_FOUND"""


async def ask_gemini(contents: list) -> str:
    if not GEMINI_KEY:
        raise ValueError("GEMINI_API_KEY is not set!")

    payload = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 8000,
            "temperature": 0.05,
        }
    }

    last_error = None

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

        for attempt in range(4):
            try:
                wait_before = [0, 5, 15, 30][attempt]
                if wait_before:
                    await asyncio.sleep(wait_before)

                logger.info("Trying model=%s attempt=%d", model, attempt + 1)
                async with httpx.AsyncClient(timeout=300) as client:
                    resp = await client.post(
                        f"{url}?key={GEMINI_KEY}",
                        json=payload,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        result = data["candidates"][0]["content"]["parts"][0]["text"]
                        logger.info("Success — %d chars (model=%s)", len(result), model)
                        return result

                    if resp.status_code in (503, 429):
                        last_error = f"{resp.status_code} (model={model})"
                        continue

                    last_error = f"{resp.status_code}: {resp.text[:150]}"
                    break

            except httpx.TimeoutException:
                last_error = f"Timeout (model={model})"
                continue
            except Exception as e:
                last_error = str(e)
                break

        logger.warning("Model %s exhausted — trying next", model)

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


async def ask_gemini_single_image(img_b64: str, media_type: str, page_num: int, total: int) -> str:
    contents = [{
        "role": "user",
        "parts": [
            {"inline_data": {"mime_type": media_type, "data": img_b64}},
            {"text": MCQ_PROMPT + f"\n\nThis is page {page_num} of {total}. Extract ALL questions on this page."}
        ]
    }]
    return await ask_gemini(contents)


async def ask_gemini_with_text(text: str) -> str:
    if len(text) > 80000:
        text = text[:80000]
    contents = [{
        "role": "user",
        "parts": [{"text": MCQ_PROMPT + "\n\n---\n\n" + text}]
    }]
    return await ask_gemini(contents)


async def ask_gemini_with_image(img_b64: str, media_type: str) -> str:
    contents = [{
        "role": "user",
        "parts": [
            {"inline_data": {"mime_type": media_type, "data": img_b64}},
            {"text": MCQ_PROMPT}
        ]
    }]
    return await ask_gemini(contents)


def pdf_to_images(data: bytes, scale: float = 3.5) -> list[tuple[str, str]]:
    """يحول PDF إلى صور عالية الدقة."""
    try:
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        images = []
        mat = fitz.Matrix(scale, scale)
        for page in doc:
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("jpeg", jpg_quality=88)
            img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            images.append((img_b64, "image/jpeg"))
        doc.close()
        logger.info("Converted PDF to %d images at scale=%.1f", len(images), scale)
        return images
    except Exception as e:
        logger.error("PDF to images failed: %s", e)
        return []


def renumber_and_deduplicate(text: str) -> str:
    """
    يعيد ترقيم الأسئلة ويحذف المكررات.

    ملاحظة إصلاح: النسخة القديمة استخدمت regex واحد شامل (بصيغة DOTALL)
    كان يفترض 4 خيارات فقط (A-D) دائماً. مع أي سؤال بعدد خيارات مختلف
    (5 خيارات A-E مثلاً، شائعة جداً في بنوك أسئلة كثيرة)، كان الـ regex
    يفشل بصمت أو "يبتلع" نصاً ضخماً بالخطأ بسبب الجشع (greedy) مع DOTALL،
    مما يُسقط أو يُشوّه أسئلة عشوائية حسب شكل النص. الحل: تحليل سطراً
    بسطر بدل regex واحد هش، يدعم أي عدد خيارات وأي حرف إجابة.
    """
    # نقسم النص إلى كتل، كل كتلة تبدأ بسطر "Qرقم."
    blocks = re.split(r'(?=^Q\d+\.\s)', text, flags=re.MULTILINE)

    _OPT_RE = re.compile(r'^([A-Za-z])\.\s*(.+)$')
    _ANS_RE = re.compile(r'^Answer:\s*([A-Za-z])\s*$', re.IGNORECASE)

    parsed: list[tuple[str, list[str], str]] = []
    for block in blocks:
        block = block.strip()
        if not block or not re.match(r'^Q\d+\.', block):
            continue

        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue

        question_lines = [re.sub(r'^Q\d+\.\s*', '', lines[0])]
        options: list[str] = []
        answer: str | None = None

        for line in lines[1:]:
            m_ans = _ANS_RE.match(line)
            if m_ans:
                answer = m_ans.group(1).upper()
                continue
            m_opt = _OPT_RE.match(line)
            if m_opt:
                options.append(line)
                continue
            if not options:
                # سطر إضافي لنص سؤال متعدد الأسطر (قبل بدء الخيارات)
                question_lines.append(line)

        if answer and len(options) >= 2:
            parsed.append((" ".join(question_lines).strip(), options, answer))

    if not parsed:
        return text

    seen = set()
    unique = []
    for question, options, answer in parsed:
        # مفتاح التكرار: النص الكامل بعد تطبيع المسافات (وليس أول 60 حرف
        # فقط) — لأن أسئلة كثيرة تتشارك نفس الصياغة الأولى بالضبط
        # (مثال: "Which of the following is a characteristic feature of
        # eukaryotic transcription?" و"...eukaryotic translation
        # initiation?") فتُعتبر خطأً سؤالاً واحداً مكرراً وتُحذف إحداهما
        # بصمت رغم أنهما سؤالان مختلفان تماماً.
        key = re.sub(r'\s+', ' ', question.strip().lower())
        if key not in seen:
            seen.add(key)
            unique.append((question.strip(), options, answer.strip()))

    lines_out = []
    for i, (q, opts, ans) in enumerate(unique, 1):
        lines_out.append(f"Q{i}. {q}")
        lines_out.extend(opts)
        lines_out.append(f"Answer: {ans}")
        lines_out.append("")

    logger.info("Deduplicated: %d → %d questions", len(parsed), len(unique))
    return "\n".join(lines_out)


async def process_pages_parallel(images: list[tuple[str, str]]) -> str:
    """معالجة متوازية — كل الصفحات في نفس الوقت."""
    total = len(images)
    logger.info("Processing %d pages in parallel", total)

    tasks = [
        ask_gemini_single_image(img_b64, media_type, i + 1, total)
        for i, (img_b64, media_type) in enumerate(images)
    ]

    semaphore = asyncio.Semaphore(3)

    async def bounded_task(task):
        async with semaphore:
            return await task

    results = await asyncio.gather(
        *[bounded_task(task) for task in tasks],
        return_exceptions=True
    )

    combined = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error("Page %d failed: %s", i + 1, result)
        elif result and result.strip() != "NO_QUESTIONS_FOUND":
            combined.append(result.strip())

    return "\n\n".join(combined) if combined else "NO_QUESTIONS_FOUND"


async def smart_extract_mcq(filename: str, data: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "txt"
    size_mb = len(data) / (1024 * 1024)

    logger.info("▶ smart_extract_mcq — file: %s | type: %s | size: %.2f MB",
                filename, ext, size_mb)

    if not GEMINI_KEY:
        raise ValueError("GEMINI_API_KEY is not configured!")

    image_types = {
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "png":  "image/png",
        "webp": "image/webp",
    }

    if ext in image_types:
        img_b64 = base64.standard_b64encode(data).decode("utf-8")
        logger.info("Single image — sending to Gemini Vision")
        return await ask_gemini_with_image(img_b64, image_types[ext])

    if ext == "pdf":
        images = pdf_to_images(data, scale=3.5)

        if images:
            logger.info("Processing %d pages in parallel", len(images))
            raw_result = await process_pages_parallel(images)
            final = renumber_and_deduplicate(raw_result)
            logger.info("Final result: %d chars", len(final))
            return final

        logger.info("Fallback: sending PDF directly")
        pdf_b64 = base64.standard_b64encode(data).decode("utf-8")
        contents = [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}},
                {"text": MCQ_PROMPT}
            ]
        }]
        return await ask_gemini(contents)

    for enc in ("utf-8", "utf-8-sig", "cp1256", "latin-1"):
        try:
            text = data.decode(enc)
            return await ask_gemini_with_text(text)
        except UnicodeDecodeError:
            continue

    return await ask_gemini_with_text(data.decode("utf-8", errors="replace"))
