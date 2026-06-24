import io
import base64
import logging
import os

import httpx

logger = logging.getLogger("AIExtractor")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

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
                logger.info("pdfplumber نجح — %d حرف", len(text))
                return text
    except Exception as e:
        logger.warning("pdfplumber فشل: %s", e)

    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages)
        if len(text.strip()) > 50:
            logger.info("PyPDF2 نجح — %d حرف", len(text))
            return text
    except Exception as e:
        logger.warning("PyPDF2 فشل: %s", e)

    return ""


async def ask_gemini(contents: list) -> str:
    if not GEMINI_KEY:
        raise ValueError("GEMINI_API_KEY غير موجود في المتغيرات البيئية!")

    payload = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 8000,
            "temperature": 0.1,
        }
    }

    logger.info("إرسال طلب لـ Gemini...")

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{GEMINI_URL}?key={GEMINI_KEY}",
                json=payload,
            )
            logger.info("استجابة Gemini: %d", resp.status_code)
            if resp.status_code != 200:
                logger.error("خطأ Gemini كامل: %s", resp.text)
                resp.raise_for_status()

            data = resp.json()
            result = data["candidates"][0]["content"]["parts"][0]["text"]
            logger.info("Gemini أعاد %d حرف", len(result))
            return result

    except httpx.TimeoutException:
        logger.error("انتهت مهلة الاتصال بـ Gemini")
        raise RuntimeError("انتهت مهلة Gemini (180 ثانية) — حاول مجدداً")
    except httpx.HTTPStatusError as e:
        logger.error("HTTP Error من Gemini: %s", e.response.text)
        raise RuntimeError(f"خطأ من Gemini API: {e.response.status_code} — {e.response.text[:300]}")
    except (KeyError, IndexError) as e:
        logger.error("خطأ في تحليل رد Gemini: %s", e)
        raise RuntimeError("رد غير متوقع من Gemini")


async def ask_gemini_with_text(text: str) -> str:
    if len(text) > 80000:
        text = text[:80000]
        logger.warning("النص كبير — تم اقتطاعه إلى 80000 حرف")

    contents = [{
        "role": "user",
        "parts": [{"text": MCQ_PROMPT + "\n\n---\n\n" + text}]
    }]
    return await ask_gemini(contents)


async def ask_gemini_with_image(img_b64: str, media_type: str) -> str:
    contents = [{
        "role": "user",
        "parts": [
            {
                "inline_data": {
                    "mime_type": media_type,
                    "data": img_b64,
                }
            },
            {"text": MCQ_PROMPT}
        ]
    }]
    return await ask_gemini(contents)


async def ask_gemini_with_pdf(data: bytes) -> str:
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("ملف PDF كبير جداً (أكثر من 20MB)")

    pdf_b64 = base64.standard_b64encode(data).decode("utf-8")
    contents = [{
        "role": "user",
        "parts": [
            {
                "inline_data": {
                    "mime_type": "application/pdf",
                    "data": pdf_b64,
                }
            },
            {"text": MCQ_PROMPT}
        ]
    }]
    return await ask_gemini(contents)


async def smart_extract_mcq(filename: str, data: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "txt"

    logger.info("▶ smart_extract_mcq — ملف: %s | نوع: %s | حجم: %d bytes",
                filename, ext, len(data))

    if not GEMINI_KEY:
        raise ValueError("GEMINI_API_KEY غير مضبوط!")

    image_types = {
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "png":  "image/png",
        "webp": "image/webp",
    }

    # صورة
    if ext in image_types:
        img_b64 = base64.standard_b64encode(data).decode("utf-8")
        logger.info("صورة — إرسال لـ Gemini Vision")
        return await ask_gemini_with_image(img_b64, image_types[ext])

    # PDF
    if ext == "pdf":
        # أولاً نجرب استخراج النص
        text = await extract_text_from_pdf(data)
        if text:
            logger.info("PDF نصي — إرسال النص لـ Gemini (%d حرف)", len(text))
            return await ask_gemini_with_text(text)
        # PDF ممسوح — نرسله مباشرة
        logger.info("PDF ممسوح — إرسال مباشر لـ Gemini")
        return await ask_gemini_with_pdf(data)

    # نص عادي
    for enc in ("utf-8", "utf-8-sig", "cp1256", "latin-1"):
        try:
            text = data.decode(enc)
            logger.info("ملف نصي (%s) — إرسال لـ Gemini", enc)
            return await ask_gemini_with_text(text)
        except UnicodeDecodeError:
            continue

    return await ask_gemini_with_text(data.decode("utf-8", errors="replace"))
