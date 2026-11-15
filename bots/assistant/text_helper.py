"""Финальный текст для пересылки: анализ делает основная модель по данным,
а сообщение на пересылку причёсывается отдельным коротким вызовом с жёстким стилем."""
from openai import OpenAI

from config import OPENAI_API_KEY, OPENAI_MODEL

_client = OpenAI(api_key=OPENAI_API_KEY, timeout=60, max_retries=2)


def _sys(audience):
    base = ("Ты пишешь короткое рабочее сообщение в чат. Коротко, по делу, живым языком, "
            "без канцелярита, без заголовков и списков. Бери только факты и цифры из вводных, "
            "ничего не придумывай. Не подписывайся.")
    if audience == "merchant":
        # Мерчу — официально и на «вы»: первый контакт не должен звучать панибратски
        return base + " Адресат — внешний партнёр: вежливо, на «вы», без сленга."
    return base + " Адресат — коллеги: можно на «ты», без лишней вежливости."


def draft(theses, audience="colleagues"):
    """Вернуть готовый текст для пересылки. audience: colleagues|merchant."""
    try:
        r = _client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _sys(audience)},
                {"role": "user", "content": "Напиши из этих вводных готовое сообщение своими словами, "
                 "цифры сохрани точно:\n\n" + str(theses)},
            ],
            temperature=0.7,
        )
        return {"text": r.choices[0].message.content.strip()}
    except Exception as e:
        return {"error": str(e)[:300]}
