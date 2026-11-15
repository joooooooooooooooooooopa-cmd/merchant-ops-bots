# -*- coding: utf-8 -*-
"""
Сборка отдельного дашборда «Гейт Конверсия» в Metabase Acme.
На одном экране почасовая конверсия по всем РУБЛЁВЫМ гейтам,
отдельным «окошком» на каждый гейт.

Каждое окошко: столбики Заявок (серый) + Оплачено (зелёный),
линия Конверсия = оплата/заявки, % (синяя, правая ось). Окно скользящее, МСК.
Сверху сводная таблица по всем рублёвым гейтам со всеми тремя конверсиями.
Период регулируется параметром «Период, часов» (по умолчанию 24).

НЕ трогает основной дашборд (id=4). Кука — в cookie.txt.
"""
import json, uuid, mb

DB = 2
COLLECTION = 22                              # «Бизнес» (сюда кладём сам дашборд)
CARDS_SUBNAME = "Гейт Конверсия — карточки"  # подпапка для карточек (порядок в коллекции)
CARDS_COLLECTION = None                      # id подпапки, проставляется в main()
CHASOV_PID = "chasovwin"                     # id параметра дашборда «Период, часов»

# Рублёвые гейты по объёму трафика за 7д (mock_gate и неактивные исключены)
GATES = [
    ("gate-orion",   "Orion"),
    ("gate-sigma", "Sigma"),
    ("gate-vega",     "Vega"),
    ("gate-lyra",  "Lyra"),
    ("gate-altair",    "Altair"),
    ("gate-rigel",    "Rigel"),
]

SUCC = "i.payment_status_id IN (3,6)"
ISS = ("(EXISTS (SELECT 1 FROM pricing_invoicerequisite r WHERE r.invoice_id=i.id) "
       "OR COALESCE(i.data->'route_result'->>'status'='success',false) "
       "OR i.payment_status_id IN (3,6))")
NOTEST = ("i.is_test=false AND i.merchant_id NOT IN "
          "(SELECT id FROM pricing_merchant WHERE COALESCE(brand_name,iname,'') ILIKE '%test%')")
WINDOW = "i.created_at >= now() - ({{chasov}} * interval '1 hour')"
RUB = "i.currency_code_id='RUB'"


def chasov_tag():
    return {"chasov": {
        "id": str(uuid.uuid4()), "name": "chasov", "display-name": "Период, часов",
        "type": "number", "default": "24", "required": True}}


def native(query):
    return {"database": DB, "type": "native",
            "native": {"query": query, "template-tags": chasov_tag()}}


def gate_query(iname):
    return f"""SELECT
  date_trunc('hour', i.created_at AT TIME ZONE 'Europe/Moscow') AS "Час",
  count(*)                                       AS "Заявок",
  count(*) FILTER (WHERE {SUCC})                 AS "Оплачено",
  round(100.0*count(*) FILTER (WHERE {SUCC})/NULLIF(count(*),0),1) AS "Конверсия %"
FROM pricing_invoice i
JOIN pricing_paymentgate g ON g.id=i.primary_payment_gate_id
WHERE g.iname='{iname}'
  AND {RUB}
  AND {NOTEST}
  AND {WINDOW}
GROUP BY 1
ORDER BY 1"""


def summary_query():
    gate_list = ",".join(f"'{g}'" for g, _ in GATES)
    return f"""SELECT
  initcap(replace(g.iname,'gate-','')) AS "Гейт",
  count(*)                              AS "Заявок",
  count(*) FILTER (WHERE {ISS})         AS "Выдано",
  count(*) FILTER (WHERE {SUCC})        AS "Оплачено",
  round(100.0*count(*) FILTER (WHERE {ISS})/NULLIF(count(*),0),1)                       AS "Конв. в выдачу, %",
  round(100.0*count(*) FILTER (WHERE {SUCC})/NULLIF(count(*) FILTER (WHERE {ISS}),0),1) AS "Конв. в успех, %",
  round(100.0*count(*) FILTER (WHERE {SUCC})/NULLIF(count(*),0),1)                      AS "Конв. оплата/заявки, %",
  COALESCE(sum(i.amount_paid) FILTER (WHERE {SUCC}),0)::bigint                          AS "Оборот, ₽"
FROM pricing_invoice i
JOIN pricing_paymentgate g ON g.id=i.primary_payment_gate_id
WHERE {RUB}
  AND {NOTEST}
  AND g.iname IN ({gate_list})
  AND {WINDOW}
GROUP BY 1
ORDER BY 2 DESC"""


def gate_viz():
    return {
        "graph.dimensions": ["Час"],
        "graph.metrics": ["Заявок", "Оплачено", "Конверсия %"],
        "series_settings": {
            "Заявок":      {"display": "bar",  "color": "#A0AEC0"},
            "Оплачено":    {"display": "bar",  "color": "#84BB4C"},
            "Конверсия %": {"display": "line", "color": "#509EE3", "axis": "right"},
        },
        "graph.x_axis.title_text": "Час (МСК)",
        "graph.y_axis.title_text": "Заявок / Оплачено",
        "graph.show_values": False,
    }


def summary_viz():
    return {
        "table.column_formatting": [{
            "columns": ["Конв. оплата/заявки, %"],
            "type": "range",
            "colors": ["#ED6E6E", "#F9E07F", "#84BB4C"],
            "min_type": "custom", "min_value": 0,
            "max_type": "custom", "max_value": 100,
        }],
        "column_settings": {
            '["name","Оборот, ₽"]': {"number_separators": " ", "suffix": " ₽"},
        },
    }


def create_card(name, query, display, viz, desc=None):
    body = {
        "name": name, "dataset_query": native(query), "display": display,
        "visualization_settings": viz, "collection_id": CARDS_COLLECTION,
    }
    if desc:
        body["description"] = desc
    c = mb.api("POST", "/api/card", body)
    print(f"  card {c['id']:>4}  {name}")
    return c["id"]


def main():
    global CARDS_COLLECTION
    # найти или создать подпапку карточек под «Бизнес» (чтобы не валялись в корне)
    sub = next((it for it in mb.api("GET", f"/api/collection/{COLLECTION}/items").get("data", [])
                if it.get("model") == "collection" and it.get("name") == CARDS_SUBNAME), None)
    CARDS_COLLECTION = sub["id"] if sub else mb.api(
        "POST", "/api/collection",
        {"name": CARDS_SUBNAME, "parent_id": COLLECTION,
         "description": "Карточки дашборда «Гейт Конверсия» (id=7)."})["id"]
    print("== подпапка карточек id =", CARDS_COLLECTION, "==")

    print("== создаю карточки ==")
    summary_id = create_card("Гейты — сводка за период", summary_query(), "table", summary_viz(),
                             desc="Все рублёвые гейты за выбранный период. Три конверсии для полного диагноза.")
    gate_cards = []
    for iname, nice in GATES:
        cid = create_card(f"{nice} — почасовая конверсия", gate_query(iname), "combo", gate_viz(),
                          desc=f"{nice}: почасовая динамика заявок/оплат и конверсия (оплата/заявки), RUB, МСК.")
        gate_cards.append((cid, nice))

    print("== создаю дашборд ==")
    dash = mb.api("POST", "/api/dashboard",
                  {"name": "Гейт Конверсия", "collection_id": COLLECTION,
                   "description": "Почасовая конверсия по рублёвым гейтам — отдельное окошко на каждый гейт."})
    did = dash["id"]
    print("  dashboard id =", did)

    # параметр «Период, часов»
    parameters = [{
        "id": CHASOV_PID, "name": "Период, часов", "slug": "chasov",
        "type": "number/=", "sectionId": "number", "default": 24,
    }]

    def pmap(card_id):
        return [{"parameter_id": CHASOV_PID, "card_id": card_id,
                 "target": ["variable", ["template-tag", "chasov"]]}]

    dashcards = [
        {"id": -1, "card_id": summary_id, "row": 0, "col": 0, "size_x": 24, "size_y": 6,
         "series": [], "parameter_mappings": pmap(summary_id), "visualization_settings": {}},
    ]
    # окошки гейтов: 2 в ряд
    row, col, nid = 6, 0, -2
    for cid, nice in gate_cards:
        dashcards.append({"id": nid, "card_id": cid, "row": row, "col": col,
                          "size_x": 12, "size_y": 7, "series": [],
                          "parameter_mappings": pmap(cid), "visualization_settings": {}})
        nid -= 1
        if col == 0:
            col = 12
        else:
            col = 0
            row += 7

    mb.api("PUT", f"/api/dashboard/{did}",
           {"parameters": parameters, "dashcards": dashcards})
    print("== готово ==")
    print(f"URL: {mb.BASE}/dashboard/{did}")
    return did


if __name__ == "__main__":
    main()
