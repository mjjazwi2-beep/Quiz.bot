import io
import base64
import logging
import os
import httpx

logger = logging.getLogger("AIExtractor")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MCQ_PROMPT = """أنت أداة متخصصة في استخراج وتنسيق امتحانات الاختيار من متعدد MCQ بدقة عالية جداً.

المطلوب: استخرج جميع الأسئلة وربط كل سؤال بإجابته الصحيحة من قسم Answer Key.

تعليمات إلزامية:
- انسخ نص كل سؤال حرفياً 100% كما هو في الملف الأصلي
- لا تلخص ولا تصحح ولا تفسر
- حافظ على جميع الرموز وعلامات الترقيم كما هي
- لا تحذف أي كلمة ولا تضف أي كلمة
- استخرج خيارات كل سؤال بنفس ترتيبها الأصلي
- بعد الخيارات مباشرة أضف: Answer: X

تنسيق الإخراج الإلزامي بالضبط:
Q1. نص السؤال
A. الخيار الأول
B. الخيار الثاني
C. الخيار الثالث
D. الخيار الرابع
Answer: A

واستمر بنفس النمط حتى آخر سؤال.
لا تضف أي عناوين أو ملاحظات أو تعليقات."""


async def extract_text_from_pdf(data: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
            text = "\n".join(pages)
            if len(text.strip()) > 50:
                return text
    except Exception as e:
        logger.warning("pdfplumber فشل: %s", e)

    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages)
        if len(text.strip()) > 50:
            return text
    except Exception as e:
        logger.warning("PyPDF2 فشل: %s", e)

    return ""


async def ask_claude(messages: list, use_pdf_beta: bool = False) -> str:
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if use_pdf_beta:
        headers["anthropic-beta"] = "pdfs-2024-09-25"

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 8000,
        "messages": messages,
    }

    logger.info("إرسال طلب لـ Claude — use_pdf_beta=%s", use_pdf_beta)

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
        )
        logger.info("استجابة Claude: %d", resp.status_code)
        if resp.status_code != 200:
            logger.error("خطأ Claude كامل: %s", resp.text)
            resp.raise_for_status()
        return resp.json()["content"][0]["text"]


async def ask_claude_with_text(text: str) -> str:
    if len(text) > 80000:
        text = text[:80000]
        logger.warning("النص كبير — تم اقتطاعه")

    messages = [{
        "role": "user",
        "content": MCQ_PROMPT + "\n\n---\n\n" + text
    }]
    return await ask_claude(messages)


async def ask_claude_with_image(img_b64: str, media_type: str) -> str:
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": img_b64,
                }
            },
            {
                "type": "text",
                "text": MCQ_PROMPT,
            }
        ]
    }]
    return await ask_claude(messages)


async def ask_claude_with_pdf_bytes(data: bytes) -> str:
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("ملف PDF كبير جداً (أكثر من 15MB)")

    pdf_b64 = base64.standard_b64encode(data).decode("utf-8")
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64,
                }
            },
            {
                "type": "text",
                "text": MCQ_PROMPT,
            }
        ]
    }]
    return await ask_claude(messages, use_pdf_beta=True)


async def smart_extract_mcq(filename: str, data: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "txt"

    logger.info("معالجة ملف: %s (نوع: %s، حجم: %d bytes)", filename, ext, len(data))

    image_types = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }

    if ext in image_types:
        img_b64 = base64.standard_b64encode(data).decode("utf-8")
        logger.info("صورة — إرسال لـ Claude Vision")
        return await ask_claude_with_image(img_b64, image_types[ext])

    if ext == "pdf":
        text = await extract_text_from_pdf(data)
        if text:
            logger.info("PDF نصي — إرسال النص لـ Claude")
            return await ask_claude_with_text(text)
        logger.info("PDF ممسوح — إرسال مباشر لـ Claude")
        return await ask_claude_with_pdf_bytes(data)

    for enc in ("utf-8", "utf-8-sig", "cp1256", "latin-1"):
        try:
            text = data.decode(enc)
            logger.info("ملف نصي — إرسال لـ Claude")
            return await ask_claude_with_text(text)
        except UnicodeDecodeError:
            continue

    return await ask_claude_with_text(data.decode("utf-8", errors="replace"))
