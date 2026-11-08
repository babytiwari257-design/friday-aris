import os
import datetime
from typing import List, Dict, Any
from groq import Groq
from duckduckgo_search import DDGS

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

MODEL_CASCADE = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
]


def get_ist_now() -> str:
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    ist_now = utc_now + datetime.timedelta(hours=5, minutes=30)
    return ist_now.strftime("%A, %d %B %Y, %I:%M %p IST")


def needs_live_web(text: str) -> bool:
    triggers = [
        "latest", "news", "today", "2026", "weather", "price", "score",
        "search", "live", "current", "who won", "update", "stock", "release"
    ]
    lower = text.lower()
    return any(t in lower for t in triggers)


def fetch_web_intel(query: str) -> str:
    try:
        clean_q = query.replace("@friday", "").replace("@FRIDAY", "").strip()
        with DDGS() as ddgs:
            results = list(ddgs.text(clean_q, max_results=4))
        if not results:
            return ""
        snippets = [f"- {r.get('title', '')}: {r.get('body', '')}" for r in results]
        return "\n".join(snippets)
    except Exception:
        return ""


def ask_friday(
    prompt: str,
    sender_name: str = "User",
    chat_context: List[Dict[str, str]] = None,
    is_group_mention: bool = False,
) -> Dict[str, Any]:
    if not client:
        return {
            "reply": "⚠️ F.R.I.D.A.Y. Core Alert: GROQ_API_KEY is missing in Render Environment Variables.",
            "web_used": False,
        }

    web_context = ""
    web_used = False
    if needs_live_web(prompt):
        web_context = fetch_web_intel(prompt)
        if web_context:
            web_used = True

    mode_instruction = (
        f"You were summoned via @friday inside a live social chat room by {sender_name}. "
        "Keep your answer crisp, helpful, friendly, and directly relevant to the chat context."
        if is_group_mention
        else f"You are in a 1-on-1 direct message with {sender_name}. Be warm, sharp, conversational, and deeply intelligent like Gemini."
    )

    system_prompt = (
        f"You are F.R.I.D.A.Y., an ultra-intelligent personal AI and social co-pilot built by Aris Industries (Architect: Mayank). "
        f"Current Date & Time: {get_ist_now()}. "
        f"Fluently adapt to English, Hindi, or Hinglish depending on how {sender_name} writes. "
        f"{mode_instruction}"
    )

    if web_context:
        system_prompt += f"\n\n[LIVE WEB INTELLIGENCE]:\n{web_context}"

    messages = [{"role": "system", "content": system_prompt}]

    if chat_context:
        for msg in chat_context[-10:]:
            messages.append({
                "role": msg.get("role", "user"),
                "content": str(msg.get("content", ""))[:1200],
            })

    messages.append({"role": "user", "content": prompt})

    last_error = ""
    for model_name in MODEL_CASCADE:
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.65,
                max_tokens=1500,
            )
            answer = resp.choices[0].message.content.strip()
            return {"reply": answer, "web_used": web_used, "model": model_name}
        except Exception as e:
            last_error = str(e)
            continue

    return {
        "reply": f"⚠️ F.R.I.D.A.Y. Neural Overload: {last_error}",
        "web_used": False,
    }
