import io
import base64
import logging
import os
import asyncio

import httpx

logger = logging.getLogger("AIExtractor")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

# نجرب النموذجين — إذا فشل الأول ننتقل للثاني
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-1.5-flash",
]

MCQ_PROMPT = """You are a world-class MCQ extraction expert specialized in medical education content.

Your ONLY job: extract every single question with its options and identify the correct answer with 100% accuracy.

=== HOW TO DETECT THE CORRECT ANSWER ===
Look for ANY of these signals — visual or textual:

VISUAL signals (in images/PDFs):
- Highlighted or colored option (yellow, green, blue, any color)
- Bold or underlined option
- Checkmark ✓ or tick mark next to an option
- Arrow → or asterisk * pointing to an option
- Filled bubble/circle ● in bubble sheets (vs empty ○)
- Strikethrough on WRONG options (correct = the one NOT crossed)
- Any mark, dot, star, or symbol near an option
- Handwritten circle or mark on an answer sheet

TEXTUAL signals:
- Answer: X / Ans: X / Correct: X / Key: X
- The letter alone on a line after options (A / B / C / D)
- Answer written at end of question or in a separate answer key section

IF NO SIGNAL EXISTS:
- Use your medical knowledge to determine the correct answer
- Choose the most scientifically accurate option

=== EXTRACTION RULES ===
- Copy question text EXACTLY — zero modifications, zero corrections
- Copy every option EXACTLY as written — spelling errors included
- Preserve original option order (A B C D or 1 2 3 4 or any format)
- Extract ALL questions even if format varies between them
- Handle any number of options (2, 3, 4, 5, or more)
- If a question has a clinical vignette/case — include the FULL case text
- If options span multiple lines — combine them correctly

=== HANDLE ANY FORMAT ===
✅ Numbered: 1. 2. 3. / Q1. Q2. Q3.
✅ Lettered options: A. B. C. D. / A) B) C) D) / (A) (B) (C) (D)
✅ Bubble sheets: detect filled bubbles
✅ Tables or columns: read left-to-right, top-to-bottom
✅ Mixed formats in same document
✅ Scanned handwritten exams
✅ Screenshots of question banks (Amboss, UWorld, Anki, etc.)
✅ Answer keys at end of document — match them to questions
✅ True/False questions → options are: True / False
✅ Questions with images described in text

=== OUTPUT FORMAT — STRICTLY FOLLOW THIS ===
Q1. [full question text exactly as written]
A. [option text exactly]
B. [option text exactly]
C. [option text exactly]
D. [option text exactly]
Answer: [correct letter only — A or B or C or D]

Q2. [full question text exactly as written]
A. [option text exactly]
...
Answer: [correct letter]

=== ABSOLUTE RULES ===
- Output NOTHING before Q1
- Output NOTHING after the last answer
- No comments, no notes, no explanations, no markdown
- No "Here are the questions:" or any intro text
- If you cannot find ANY questions, output only: NO_QUESTIONS_FOUND"""


async def extract_text_from_pdf(data: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
            text = "\n".join(pages)
            if len(text.strip()) > 50:
                logger.info("pdfplumber succeeded — %d chars", len(text))
                return text
    except Exception as e:
        logger.warning("pdfplumber failed: %s", e)

    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages)
        if len(text.strip()) > 50:
            logger.info("PyPDF2 succeeded — %d chars", len(text))
            return text
    except Exception as e:
        logger.warning("PyPDF2 failed: %s", e)

    return ""


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

        for attempt in range(3):  # 3 محاولات لكل نموذج
            try:
                logger.info("Trying model=%s attempt=%d", model, attempt + 1)
                async with httpx.AsyncClient(timeout=180) as client:
                    resp = await client.post(
                        f"{url}?key={GEMINI_KEY}",
                        json=payload,
                    )
                    logger.info("Gemini response: %d (model=%s)", resp.status_code, model)

                    if resp.status_code == 200:
                        data = resp.json()
                        result = data["candidates"][0]["content"]["parts"][0]["text"]
                        logger.info("Success — %d chars (model=%s)", len(result), model)
                        return result

                    if resp.status_code == 503:
                        wait = 5 * (attempt + 1)
                        logger.warning("503 overloaded — waiting %ds then retry", wait)
                        await asyncio.sleep(wait)
                        last_error = f"503 overloaded (model={model})"
                        continue

                    if resp.status_code == 429:
                        wait = 10 * (attempt + 1)
                        logger.warning("429 rate limit — waiting %ds", wait)
                        await asyncio.sleep(wait)
                        last_error = f"429 rate limit (model={model})"
                        continue

                    # أخطاء أخرى — سجل وانتقل للنموذج التالي
                    logger.error("Gemini error %d: %s", resp.status_code, resp.text[:200])
                    last_error = f"{resp.status_code}: {resp.text[:200]}"
                    break

            except httpx.TimeoutException:
                logger.warning("Timeout on model=%s attempt=%d", model, attempt + 1)
                last_error = f"Timeout (model={model})"
                await asyncio.sleep(5)
                continue

            except Exception as e:
                logger.error("Unexpected error: %s", e)
                last_error = str(e)
                break

        logger.warning("Model %s failed — trying next model", model)

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


async def ask_gemini_with_text(text: str) -> str:
    if len(text) > 80000:
        text = text[:80000]
        logger.warning("Text too large — truncated to 80000 chars")

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
        raise ValueError("PDF too large (max 20MB)")

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

    logger.info("▶ smart_extract_mcq — file: %s | type: %s | size: %d bytes",
                filename, ext, len(data))

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
        logger.info("Image — sending to Gemini Vision")
        return await ask_gemini_with_image(img_b64, image_types[ext])

    if ext == "pdf":
        text = await extract_text_from_pdf(data)
        if text:
            logger.info("Text PDF — sending text to Gemini (%d chars)", len(text))
            return await ask_gemini_with_text(text)
        logger.info("Scanned PDF — sending directly to Gemini Vision")
        return await ask_gemini_with_pdf(data)

    for enc in ("utf-8", "utf-8-sig", "cp1256", "latin-1"):
        try:
            text = data.decode(enc)
            logger.info("Text file (%s) — sending to Gemini", enc)
            return await ask_gemini_with_text(text)
        except UnicodeDecodeError:
            continue

    return await ask_gemini_with_text(data.decode("utf-8", errors="replace"))
