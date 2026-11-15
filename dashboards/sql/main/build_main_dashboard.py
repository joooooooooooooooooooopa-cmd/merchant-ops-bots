#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка «Основной дашборд» (Acme / БД Core) — версия v2.
Воронка: Запросы -> Выдачи -> Успехи. Конверсия в успех = успех / ВЫДАЧА (а не /все).
20 карточек, 7 фильтров: Дата, Endpoint(продукт), Терминал, Провайдер(гейт), Метод, Мерчант, Валюта.

ДОСТУП (Teleport): cookie из браузера в переменной окружения MB_COOKIE.
  export MB_COOKIE='metabase.SESSION=...; __Host-grv_app_session=...; metabase.DEVICE=...'
  python3 build_main_dashboard.py

КЛЮЧЕВЫЕ ОПРЕДЕЛЕНИЯ:
  успех (SUCC)  = payment_status_id IN (3,6)            # complete + partial_complete
  выдача (ISS)  = есть card-реквизит ИЛИ route_result.status='success'   # null-safe!
  метод         = COALESCE(requisite_type, route_result.terminal_data.provider_payment_type)
  endpoint      = продукт (invoice.primary_product_id, 100% заполнен; имя pricing_product.name)
  терминал      = invoice.primary_terminal_id (имя pricing_terminal.name = коридор/продукт)
  всегда is_test=false. Прибыль НЕ показываем (trader_fee в БД пуст -> некорректна).

ГРАБЛИ (важно):
  - product/terminal/gate/merchant ИМЕЮТ колонку `data` -> в формулах квалифицировать `pricing_invoice.data`, иначе ambiguous (400).
  - выдачу делать null-safe: COALESCE(... ='success', false), иначе NOT(...) = NULL и «Не выдалось» = 0.
  - таблицу pricing_invoice НЕ алиасить (ломает field-filter).
  - на запись слать legacy dataset_query (Metabase MLv2 на чтение отдаёт stages).
  - метод-фильтр = опциональная raw-переменная [[ AND ... = {{method}} ]]; остальные — field-filter + static-list + values_query_type:list.
Поля БД: created_at=961, primary_product_id(endpoint)=968, primary_terminal_id=986,
         primary_payment_gate_id=982, merchant_id=969, currency_code_id=970.
"""
import json, os, uuid, urllib.request, urllib.error, time
BASE=os.environ.get("MB_URL","https://metabase.example.com"); COOKIE=os.environ["MB_COOKIE"]
DB=2; PARENT_COLLECTION_NAME="Бизнес"
F_CREATED,F_ENDPOINT,F_TERMINAL,F_GATE,F_MERCH,F_CURR=961,968,986,982,969,970

def req(m,p,b=None,retry=5):
    for a in range(retry):
        try:
            r=urllib.request.Request(BASE+p,data=(json.dumps(b).encode() if b is not None else None),method=m)
            r.add_header("Cookie",COOKIE);r.add_header("Accept","application/json");r.add_header("Content-Type","application/json")
            raw=urllib.request.urlopen(r,timeout=120).read().decode(); return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e: print(f"  HTTP {e.code} {m} {p}: {e.read().decode()[:200]}"); return None
        except Exception as e:
            if a==retry-1: print("  NETERR",str(e)[:120]); return None
            time.sleep(3)
def rows(query): return (req("POST","/api/dataset",{"database":DB,"type":"native","native":{"query":query}}) or {}).get("data",{}).get("rows") or []

INV="pricing_invoice"
REQJ="LEFT JOIN (SELECT DISTINCT invoice_id FROM pricing_invoicerequisite) reqj ON reqj.invoice_id="+INV+".id"
ISS="(reqj.invoice_id IS NOT NULL OR COALESCE("+INV+".data->'route_result'->>'status'='success',false) OR payment_status_id IN (3,6))"  # успех всегда выдан -> conv<=100%
SUCC="payment_status_id IN (3,6)"
MEXPR=("COALESCE((SELECT pr2.requisite_type FROM pricing_invoicerequisite ir2 JOIN pricing_paymentrequisite pr2 "
       "ON pr2.id=ir2.requisite_id WHERE ir2.invoice_id="+INV+".id LIMIT 1), "+INV+".data->'route_result'->'terminal_data'->>'provider_payment_type')")
CA,EP,TE,GA,MR,CU,MT="{{created_at}}","{{endpoint}}","{{terminal}}","{{gate}}","{{merchant}}","{{currency}}","{{method}}"
F6="WHERE "+CA+" AND "+EP+" AND "+TE+" AND "+GA+" AND "+MR+" AND "+CU
MCL="[[ AND "+MEXPR+" = "+MT+" ]]"
def q(sel,joins="",conds="",tail=""): return sel+" FROM "+INV+" "+REQJ+(" "+joins if joins else "")+" "+F6+conds+" "+MCL+((" "+tail) if tail else "")+";"
CUR=lambda c:{"column_settings":{f'["name","{c}"]':{"number_style":"currency","currency":"RUB","currency_style":"symbol","decimals":0}}}
PRODJ="LEFT JOIN pricing_product p ON p.id="+INV+".primary_product_id"
TERMJ="LEFT JOIN pricing_terminal t ON t.id="+INV+".primary_terminal_id LEFT JOIN pricing_paymentgate g ON g.id="+INV+".primary_payment_gate_id"
GATEJ="LEFT JOIN pricing_paymentgate g ON g.id="+INV+".primary_payment_gate_id"
MERJ="LEFT JOIN pricing_merchant mr ON mr.id="+INV+".merchant_id"
STJ="JOIN pricing_paymentstatus s ON s.id="+INV+".payment_status_id"
P100=lambda x:"ROUND(100.0*COUNT(*) FILTER (WHERE "+x+")/NULLIF(COUNT(*) FILTER (WHERE "+ISS+"),0),1)"   # доля от выданных

defs=[
 ("Запросы","scalar", q("SELECT COUNT(*) AS requests"), {},0,0,4,3),
 ("Выдачи","scalar", q("SELECT COUNT(*) FILTER (WHERE "+ISS+") AS issued"), {},4,0,4,3),
 ("Успехи","scalar", q("SELECT COUNT(*) FILTER (WHERE "+SUCC+") AS success"), {},8,0,4,3),
 ("Конверсия в выдачу, %","scalar", q("SELECT ROUND(100.0*COUNT(*) FILTER (WHERE "+ISS+")/NULLIF(COUNT(*),0),2) AS conv_issue_pct"), {},12,0,4,3),
 ("Конверсия в успех, % (успех/выдача)","scalar", q("SELECT ROUND(100.0*COUNT(*) FILTER (WHERE "+SUCC+")/NULLIF(COUNT(*) FILTER (WHERE "+ISS+"),0),2) AS conv_success_pct"), {},16,0,4,3),
 ("Не выдалось","scalar", q("SELECT COUNT(*) FILTER (WHERE NOT "+ISS+") AS not_issued"), {},20,0,4,3),
 ("Выдано, не оплачено","scalar", q("SELECT COUNT(*) FILTER (WHERE "+ISS+" AND payment_status_id=4) AS issued_unpaid"), {},0,3,4,3),
 ("Оборот, ₽","scalar", q("SELECT ROUND(SUM(amount_paid) FILTER (WHERE "+SUCC+")) AS turnover"), CUR("turnover"),4,3,4,3),
 ("Средний чек, ₽","scalar", q("SELECT ROUND(AVG(amount_paid) FILTER (WHERE "+SUCC+")) AS avg_check"), CUR("avg_check"),8,3,4,3),
 ("% отказов от выданных","scalar", q("SELECT ROUND(100.0*COUNT(*) FILTER (WHERE payment_status_id=4 AND "+ISS+")/NULLIF(COUNT(*) FILTER (WHERE "+ISS+"),0),2) AS decline_pct"), {},12,3,4,3),
 ("Воронка статусов","row", q("SELECT s.iname AS status, COUNT(*) AS cnt", STJ,"","GROUP BY s.iname ORDER BY cnt DESC"), {"graph.dimensions":["status"],"graph.metrics":["cnt"]},0,6,8,5),
 ("Динамика по дням","bar", q("SELECT date_trunc('day', ("+INV+".created_at AT TIME ZONE 'Europe/Moscow')) AS day, COUNT(*) AS total, COUNT(*) FILTER (WHERE "+ISS+") AS issued, COUNT(*) FILTER (WHERE "+SUCC+") AS success","","","GROUP BY 1 ORDER BY 1"), {"graph.dimensions":["day"],"graph.metrics":["total","issued","success"]},8,6,16,5),
 ("Заявки по часам","line", q("SELECT date_trunc('hour', ("+INV+".created_at AT TIME ZONE 'Europe/Moscow')) AS hour, COUNT(*) AS orders, COUNT(*) FILTER (WHERE "+SUCC+") AS success","","","GROUP BY 1 ORDER BY 1"), {"graph.dimensions":["hour"],"graph.metrics":["orders","success"]},0,11,24,5),
 ("По endpoint (продукт)","table", q("SELECT COALESCE(p.name,p.iname,'—') AS endpoint, COUNT(*) AS requests, COUNT(*) FILTER (WHERE "+ISS+") AS issued, COUNT(*) FILTER (WHERE "+SUCC+") AS success, "+P100(SUCC)+" AS conv_pct, ROUND(SUM(amount_paid) FILTER (WHERE "+SUCC+")) AS turnover", PRODJ,"","GROUP BY 1 ORDER BY requests DESC LIMIT 30"), {},0,16,24,8),
 ("По терминалам","table", q("SELECT COALESCE(g.iname,'—') AS gate, COALESCE(t.name,t.iname,'—') AS terminal, COUNT(*) AS requests, COUNT(*) FILTER (WHERE "+ISS+") AS issued, COUNT(*) FILTER (WHERE "+SUCC+") AS success, "+P100(SUCC)+" AS conv_pct, ROUND(SUM(amount_paid) FILTER (WHERE "+SUCC+")) AS turnover", TERMJ,"","GROUP BY 1,2 ORDER BY requests DESC LIMIT 40"), {},0,24,24,8),
 ("По провайдерам (гейтам)","table", q("SELECT COALESCE(g.iname,'—') AS gate, COUNT(*) AS requests, COUNT(*) FILTER (WHERE "+ISS+") AS issued, COUNT(*) FILTER (WHERE "+SUCC+") AS success, "+P100(SUCC)+" AS conv_pct, ROUND(SUM(amount_paid) FILTER (WHERE "+SUCC+")) AS turnover", GATEJ,"","GROUP BY 1 ORDER BY requests DESC"), {},0,32,12,8),
 ("По методам","table", q("SELECT COALESCE("+MEXPR+",'(не определён)') AS method, COUNT(*) AS requests, COUNT(*) FILTER (WHERE "+ISS+") AS issued, COUNT(*) FILTER (WHERE "+SUCC+") AS success, "+P100(SUCC)+" AS conv_pct, ROUND(SUM(amount_paid) FILTER (WHERE "+SUCC+")) AS turnover","","","GROUP BY 1 ORDER BY requests DESC"), {},12,32,12,8),
 ("Топ-20 мерчантов","table", q("SELECT COALESCE(mr.brand_name,mr.iname,'—') AS merchant, COUNT(*) AS requests, COUNT(*) FILTER (WHERE "+SUCC+") AS success, "+P100(SUCC)+" AS conv_pct, ROUND(SUM(amount_paid) FILTER (WHERE "+SUCC+")) AS turnover", MERJ,"","GROUP BY 1 ORDER BY turnover DESC NULLS LAST LIMIT 20"), {},0,40,24,8),
 ("Ошибки по коду","table", q("SELECT COALESCE(last_payment_error_code,'(нет кода)') AS code, COUNT(*) AS errors, COUNT(*) FILTER (WHERE "+ISS+") AS issued_then_failed, COUNT(*) FILTER (WHERE NOT "+ISS+") AS never_issued","", " AND payment_status_id=4","GROUP BY 1 ORDER BY errors DESC LIMIT 20"), {},0,48,12,8),
 ("Выдано, не оплачено — детализация","table", q("SELECT "+INV+".external_id, "+INV+".merchant_order, ROUND(price) AS price, last_payment_error_code, created_at","", " AND "+ISS+" AND payment_status_id=4","ORDER BY created_at DESC LIMIT 50"), {},12,48,12,8),
]
def tags():
    def dim(fid,w): return {"id":str(uuid.uuid4()),"name":"x","display-name":"x","type":"dimension","dimension":["field",fid,None],"widget-type":w}
    t={"created_at":dim(F_CREATED,"date/all-options"),"endpoint":dim(F_ENDPOINT,"id"),"terminal":dim(F_TERMINAL,"id"),
       "gate":dim(F_GATE,"id"),"merchant":dim(F_MERCH,"id"),"currency":dim(F_CURR,"id"),
       "method":{"id":str(uuid.uuid4()),"name":"method","display-name":"Метод","type":"text"}}
    for k in ("created_at","endpoint","terminal","gate","merchant","currency"): t[k]["name"]=k; t[k]["display-name"]=k
    return t

def main():
    cols=req("GET","/api/collection") or []
    biz=next((c for c in cols if isinstance(c,dict) and c.get("name")==PARENT_COLLECTION_NAME), None)
    bid=biz["id"] if biz else req("POST","/api/collection",{"name":PARENT_COLLECTION_NAME})["id"]
    coll=req("POST","/api/collection",{"name":"Основной дашборд — карточки","parent_id":bid})["id"]
    cards=[]
    for (name,disp,query,viz,col,row,w,h) in defs:
        r=req("POST","/api/card",{"name":name,"collection_id":coll,"display":disp,
              "dataset_query":{"database":DB,"type":"native","native":{"query":query,"template-tags":tags()}},"visualization_settings":viz})
        cards.append((r["id"],col,row,w,h)); print("  card",r["id"],name)
    dash=req("POST","/api/dashboard",{"name":"Основной дашборд","collection_id":bid}); did=dash["id"]
    ep=[[x[0],x[1]] for x in rows("SELECT p.id, COALESCE(p.name,p.iname,'—') FROM pricing_product p WHERE EXISTS(SELECT 1 FROM pricing_invoice i WHERE i.primary_product_id=p.id) ORDER BY 2")]
    te=[[x[0],x[1]] for x in rows("SELECT t.id, COALESCE(t.name,t.iname,'—') FROM pricing_terminal t WHERE EXISTS(SELECT 1 FROM pricing_invoice i WHERE i.primary_terminal_id=t.id) ORDER BY 2")]
    ga=[[x[0],x[1]] for x in rows("SELECT id,iname FROM pricing_paymentgate ORDER BY iname")]
    me=[[x[0],x[1]] for x in rows("SELECT m.id,COALESCE(m.brand_name,m.iname,'—') FROM pricing_merchant m WHERE EXISTS(SELECT 1 FROM pricing_invoice i WHERE i.merchant_id=m.id) ORDER BY 2")]
    cu=[x[0] for x in rows("SELECT DISTINCT currency_code_id FROM pricing_invoice WHERE currency_code_id IS NOT NULL ORDER BY 1")]
    methods=[["sbp","SBP (реквизит)"],["nspk","NSPK / СБП"],["card","Карта"],["qr_pay","QR Pay"],["p2pcard","P2P карта"],["to_card_number","На номер карты"]]
    P={k:uuid.uuid4().hex[:8] for k in ["date","endpoint","terminal","gate","method","merchant","currency"]}
    st=lambda v:{"values_source_type":"static-list","values_query_type":"list","values_source_config":{"values":v}}
    conn={"values_query_type":"list"}  # connected: живые значения поля (нужен FK display remapping в Data Model, иначе покажет id)
    # report-timezone должен быть Europe/Moscow (Admin->Localization). FK remapping (Data Model): merchant_id->merchant.iname, gate->iname, terminal->name, product->name.
    params=[
     {"id":P["date"],"name":"Дата","slug":"date","type":"date/all-options","default":"past7days~","sectionId":"date"},  # past7days~ = 7 дней включая сегодня (МСК)
     dict({"id":P["endpoint"],"name":"Endpoint","slug":"endpoint","type":"id","sectionId":"id"},**conn),
     dict({"id":P["terminal"],"name":"Терминал","slug":"terminal","type":"id","sectionId":"id"},**conn),
     dict({"id":P["gate"],"name":"Провайдер","slug":"gate","type":"id","sectionId":"id"},**conn),
     dict({"id":P["method"],"name":"Метод","slug":"method","type":"string/=","sectionId":"string","isMultiSelect":False},**st(methods)),
     dict({"id":P["merchant"],"name":"Мерчант","slug":"merchant","type":"id","sectionId":"id"},**conn),
     dict({"id":P["currency"],"name":"Валюта","slug":"currency","type":"id","sectionId":"id"},**st(cu)),
    ]
    tm={"date":"created_at","endpoint":"endpoint","terminal":"terminal","gate":"gate","merchant":"merchant","currency":"currency"}
    tgt=lambda k:["variable",["template-tag","method"]] if k=="method" else ["dimension",["template-tag",tm[k]]]
    dc=[{"id":-(i+1),"card_id":cid,"row":row,"col":col,"size_x":w,"size_y":h,"series":[],"visualization_settings":{},
         "parameter_mappings":[{"parameter_id":P[k],"card_id":cid,"target":tgt(k)} for k in P]} for i,(cid,col,row,w,h) in enumerate(cards)]
    req("PUT",f"/api/dashboard/{did}",{"parameters":params,"dashcards":dc})
    print(f"DASHBOARD: {BASE}/dashboard/{did}")

if __name__=="__main__": main()
