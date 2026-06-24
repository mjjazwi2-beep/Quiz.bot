import io
import base64
import logging
import os
import asyncio

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

CRITICAL: This document may contain 60 or more questions. You MUST extract ALL of them without missing any.

=== HOW TO DETECT THE CORRECT ANSWER ===
Look for ANY of these signals:
- Circle drawn around a letter or option (most common in handwritten exams)
- Highlighted or colored option
- Checkmark ✓ or tick mark next to an option
- Bold or underlined option
- Filled bubble ● in bubble sheets (vs empty ○)
- Strikethrough ✗ on WRONG options (correct = the one NOT crossed)
- Any mark, dot, star, or symbol near an option
- Answer: X / Ans: X / Correct: X written anywhere

IF NO SIGNAL EXISTS:
- Use your medical knowledge to determine the correct answer

=== EXTRACTION RULES ===
- Extract ALL questions from the ENTIRE document — do not stop early
- Copy question text EXACTLY as written
- Copy every option EXACTLY as written
- Preserve original option order
- Handle 2-column layouts (read left option then right option)
- If options are arranged in 2 columns: a and c are on left, b and d on right — read them correctly

=== OUTPUT FORMAT — STRICTLY FOLLOW THIS ===
Q1. [full question text]
A. [option a text]
B. [option b text]
C. [option c text]
D. [option d text]
Answer: [correct letter]

=== ABSOLUTE RULES ===
- Output NOTHING before Q1
- Output NOTHING after the last answer
- No comments, no notes, no explanations
- Extract ALL questions — missing questions is a critical failure"""


async def ask_gemini(contents: list) -> str:
    if not GEMINI_KEY:
        raise ValueError("GEMINI_API_KEY is not set!")

    payload = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 16000,
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
                    logger.info("Gemini response: %d (model=%s)", resp.status_code, model)

                    if resp.status_code == 200:
                        data = resp.json()
                        result = data["candidates"][0]["content"]["parts"][0]["text"]
                        logger.info("Success — %d chars (model=%s)", len(result), model)
                        return result

                    if resp.status_code in (503, 429):
                        logger.warning("Status %d — retrying (model=%s)", resp.status_code, model)
                        last_error = f"{resp.status_code} (model={model})"
                        continue

                    logger.error("Fatal error %d: %s", resp.status_code, resp.text[:200])
                    last_error = f"{resp.status_code}: {resp.text[:150]}"
                    break

            except httpx.TimeoutException:
                logger.warning("Timeout on model=%s attempt=%d", model, attempt + 1)
                last_error = f"Timeout (model={model})"
                continue

            except Exception as e:
                logger.error("Unexpected error: %s", e)
                last_error = str(e)
                break

        logger.warning("Model %s exhausted — trying next", model)

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


async def pdf_to_images_b64(data: bytes) -> list[tuple[str, str]]:
    """يحول PDF إلى صور base64 — كل صفحة صورة منفصلة."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        images = []
        for page in doc:
            mat = fitz.Matrix(2.0, 2.0)  # دقة x2
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("jpeg")
            img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            images.append((img_b64, "image/jpeg"))
        doc.close()
        logger.info("Converted PDF to %d images via PyMuPDF", len(images))
        return images
    except ImportError:
        logger.warning("PyMuPDF not available")
    except Exception as e:
        logger.warning("PyMuPDF failed: %s", e)

    try:
        from pdf2image import convert_from_bytes
        pil_images = convert_from_bytes(data, dpi=200)
        images = []
        for pil_img in pil_images:
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=85)
            img_b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
            images.append((img_b64, "image/jpeg"))
        logger.info("Converted PDF to %d images via pdf2image", len(images))
        return images
    except ImportError:
        logger.warning("pdf2image not available")
    except Exception as e:
        logger.warning("pdf2image failed: %s", e)

    return []


async def ask_gemini_all_pages(images: list[tuple[str, str]]) -> str:
    """يرسل كل صفحات PDF دفعة واحدة لـ Gemini ليرى الكل معاً."""
    parts = []
    for img_b64, media_type in images:
        parts.append({
            "inline_data": {
                "mime_type": media_type,
                "data": img_b64,
            }
        })
    parts.append({"text": MCQ_PROMPT})

    contents = [{"role": "user", "parts": parts}]
    return await ask_gemini(contents)


async def ask_gemini_page_by_page(images: list[tuple[str, str]]) -> str:
    """يعالج كل صفحة منفردة ثم يجمع النتائج."""
    all_results = []
    for i, (img_b64, media_type) in enumerate(images):
        logger.info("Processing page %d/%d", i + 1, len(images))
        contents = [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": media_type, "data": img_b64}},
                {"text": MCQ_PROMPT + f"\n\nNote: This is page {i+1} of {len(images)}. Extract all questions visible on this page only."}
            ]
        }]
        try:
            result = await ask_gemini(contents)
            if result.strip() and result.strip() != "NO_QUESTIONS_FOUND":
                all_results.append(result.strip())
        except Exception as e:
            logger.error("Failed page %d: %s", i + 1, e)

    return "\n\n".join(all_results) if all_results else "NO_QUESTIONS_FOUND"


async def ask_gemini_with_text(text: str) -> str:
    if len(text) > 80000:
        text = text[:80000]
        logger.warning("Text truncated to 80000 chars")

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


async def ask_gemini_with_pdf_direct(data: bytes) -> str:
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("PDF too large (max 20MB)")
    pdf_b64 = base64.standard_b64encode(data).decode("utf-8")
    contents = [{
        "role": "user",
        "parts": [
            {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}},
            {"text": MCQ_PROMPT}
        ]
    }]
    return await ask_gemini(contents)


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

    # صورة واحدة
    if ext in image_types:
        img_b64 = base64.standard_b64encode(data).decode("utf-8")
        logger.info("Image — sending to Gemini Vision")
        return await ask_gemini_with_image(img_b64, image_types[ext])

    # PDF
    if ext == "pdf":
        # الاستراتيجية: حول لصور أولاً لأن الامتحانات الملتقطة بالكاميرا لا يُستخرج منها نص صحيح
        logger.info("PDF — converting to images for full visual extraction")
        images = await pdf_to_images_b64(data)

        if images:
            logger.info("Got %d page images — sending all to Gemini at once", len(images))
            try:
                # أرسل كل الصفحات دفعة واحدة
                result = await ask_gemini_all_pages(images)
                logger.info("All-pages extraction returned %d chars", len(result))
                return result
            except Exception as e:
                logger.warning("All-pages failed (%s) — trying page by page", e)
                # احتياطي: صفحة صفحة
                return await ask_gemini_page_by_page(images)

        # إذا فشل تحويل الصور، جرب إرسال PDF مباشرة
        logger.info("Image conversion failed — sending PDF directly to Gemini")
        try:
            return await ask_gemini_with_pdf_direct(data)
        except Exception as e:
            logger.error("Direct PDF failed: %s", e)
            raise

    # نص عادي
    for enc in ("utf-8", "utf-8-sig", "cp1256", "latin-1"):
        try:
            text = data.decode(enc)
            logger.info("Text file (%s) — sending to Gemini", enc)
            return await ask_gemini_with_text(text)
        except UnicodeDecodeError:
            continue

    return await ask_gemini_with_text(data.decode("utf-8", errors="replace"))
