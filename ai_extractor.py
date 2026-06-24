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
    """استخراج النص من PDF النصي"""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = []
            for p in pdf.pages:
                t = p.extract_text()
                if t:
                    pages.append(t)
            text = "\n".join(pages)
            if len(text.strip()) > 50:
                return text
    except Exception as e:
        logger.warning("pdfplumber فشل: %s", e)

    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                pages.append(t)
        text = "\n".join(pages)
        if len(text.strip()) > 50:
            return text
    except Exception as e:
        logger.warning("PyPDF2 فشل: %s", e)

    return ""


async def ask_claude_with_text(text: str) -> str:
    """إرسال النص لـ Claude لاستخراج MCQ"""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 8000,
                "messages": [{
                    "role": "user",
                    "content": MCQ_PROMPT + "\n\n---\n\n" + text
                }]
            }
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


async def ask_claude_with_image(img_b64: str, media_type: str) -> str:
    """إرسال صورة لـ Claude Vision لاستخراج MCQ"""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 8000,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": img_b64
                            }
                        },
                        {
                            "type": "text",
                            "text": MCQ_PROMPT
                        }
                    ]
                }]
            }
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


async def ask_claude_with_pdf_bytes(data: bytes) -> str:
    """إرسال PDF مباشرة لـ Claude كـ document"""
    pdf_b64 = base64.standard_b64encode(data).decode("utf-8")
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 8000,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64
                            }
                        },
                        {
                            "type": "text",
                            "text": MCQ_PROMPT
                        }
                    ]
                }]
            }
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


async def smart_extract_mcq(filename: str, data: bytes) -> str:
    """
    الدالة الرئيسية — تختار الاستراتيجية تلقائياً:
    1. صورة → Claude Vision مباشرة
    2. PDF نصي → استخراج نص + Claude
    3. PDF ممسوح → Claude يقرأ PDF مباشرة
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "txt"

    # صورة مباشرة
    if ext in ("jpg", "jpeg"):
        img_b64 = base64.standard_b64encode(data).decode("utf-8")
        return await ask_claude_with_image(img_b64, "image/jpeg")

    if ext == "png":
        img_b64 = base64.standard_b64encode(data).decode("utf-8")
        return await ask_claude_with_image(img_b64, "image/png")

    if ext == "webp":
        img_b64 = base64.standard_b64encode(data).decode("utf-8")
        return await ask_claude_with_image(img_b64, "image/webp")

    # PDF
    if ext == "pdf":
        # أولاً: حاول استخراج النص
        text = await extract_text_from_pdf(data)
        if text:
            logger.info("PDF نصي — إرسال النص لـ Claude")
            return await ask_claude_with_text(text)
        # ثانياً: PDF ممسوح — أرسله لـ Claude مباشرة
        logger.info("PDF ممسوح — إرسال لـ Claude Vision")
        return await ask_claude_with_pdf_bytes(data)

    # ملف نصي عادي — أرسله لـ Claude للتنسيق
    for enc in ("utf-8", "utf-8-sig", "cp1256", "latin-1"):
        try:
            text = data.decode(enc)
            return await ask_claude_with_text(text)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
