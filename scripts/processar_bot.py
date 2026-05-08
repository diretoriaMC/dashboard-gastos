#!/usr/bin/env python3
"""
Bot de gastos via Telegram.
Recebe fotos/PDFs de comprovantes, extrai os dados com IA e atualiza o dashboard.
Também processa mensagens de texto para receitas e despesas sem comprovante.
"""

import os
import re
import json
import base64
import requests
import tempfile
import io
import calendar
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Configurações ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
REPO_DIR         = Path(__file__).parent.parent
DASHBOARD_FILE   = REPO_DIR / "index.html"
ESTADO_FILE      = REPO_DIR / "estado_bot.json"
CONTAS_FILE      = REPO_DIR / "contas_fixas.json"

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# ── Telegram helpers ───────────────────────────────────────────────────────────
def get_updates(offset=None):
    params = {"timeout": 5, "limit": 10}
    if offset:
        params["offset"] = offset
    r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=15)
    return r.json().get("result", [])

def send_message(text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }, timeout=10)

def download_file(file_id):
    """Baixa um arquivo do Telegram e retorna os bytes."""
    r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=10)
    file_path = r.json()["result"]["file_path"]
    file_url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    return requests.get(file_url, timeout=30).content, file_path


# ── Extração de texto ──────────────────────────────────────────────────────────
def extrair_texto(file_bytes, file_path):
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                texto = "\n".join(p.extract_text() or "" for p in pdf.pages)
            if texto.strip():
                return texto
        except Exception:
            pass
        # Fallback: OCR via imagem
        try:
            from pdf2image import convert_from_bytes
            import pytesseract
            imgs = convert_from_bytes(file_bytes, dpi=200)
            return "\n".join(pytesseract.image_to_string(img, lang="por") for img in imgs)
        except Exception:
            pass

    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        try:
            import pytesseract
            from PIL import Image
            img = Image.open(io.BytesIO(file_bytes))
            return pytesseract.image_to_string(img, lang="por")
        except Exception as e:
            return f"Erro OCR: {e}"

    return ""


# ── Claude: extrai dados do comprovante ───────────────────────────────────────
def extrair_dados_com_ia(texto, file_bytes=None, file_path="", contexto=""):
    """Usa Claude para extrair dados estruturados do comprovante."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    hoje = datetime.now().strftime("%d/%m/%Y")
    ext  = Path(file_path).suffix.lower() if file_path else ""

    content = []
    if ext in (".jpg", ".jpeg", ".png", ".webp") and file_bytes:
        b64 = base64.standard_b64encode(file_bytes).decode()
        media = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})

    contexto_extra = f"\n\nO usuário informou que se trata de: {contexto}" if contexto else ""

    prompt = f"""Você é um assistente que extrai dados de comprovantes de pagamento brasileiros.

{"Texto extraído por OCR:" if texto.strip() else "Analise a imagem do comprovante."}
{texto if texto.strip() else ""}
{contexto_extra}

Data de hoje: {hoje}

Extraia os dados e responda APENAS com um JSON válido nesse formato:
{{
  "data": "DD/MM/AAAA",
  "valor": 0.00,
  "estabelecimento": "Nome do estabelecimento/beneficiário",
  "descricao": "Descrição curta do gasto",
  "categoria": "Categoria",
  "pagamento": "Pix|Boleto|Débito|Crédito|Dinheiro",
  "grupo": "kebab-case-para-agrupar-recorrentes",
  "precisa_confirmacao": false
}}

Categorias disponíveis: Alimentação, Restaurante, Saúde, Moradia, Transporte, Serviços, Beleza, Vestuário, Compras Online, Assinaturas, Educação, Viagem, Casa, Doações, Impostos, Outros

Regras:
- Se não conseguir identificar a data, use a data de hoje ({hoje})
- valor deve ser número (ex: 45.90, não "R$ 45,90")
- grupo em kebab-case, apenas para estabelecimentos recorrentes (ex: "supermercado-soberano")
- Se for compra no cartão de crédito, pagamento = "Cartão"
- Se o estabelecimento for nome de pessoa física sem descrição clara, ou se não conseguir identificar a categoria com segurança, coloque precisa_confirmacao: true
- Responda SOMENTE o JSON, sem texto antes ou depois"""

    content.append({"type": "text", "text": prompt})

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": content}]
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ── Claude: extrai receitas de mensagem de texto ───────────────────────────────
def extrair_receitas_com_ia(texto_msg):
    """Usa Claude para extrair receitas de uma mensagem de texto livre."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    ano_atual = datetime.now().year

    prompt = f"""Você é um assistente financeiro. O usuário enviou uma mensagem descrevendo receitas (entradas de dinheiro).

Mensagem: "{texto_msg}"

Ano atual: {ano_atual}

Extraia as receitas e responda APENAS com um JSON válido nesse formato:
[
  {{"mes": "AAAA-MM", "fonte": "Nome da fonte", "valor": 0.00}},
  ...
]

Meses em português → número: janeiro=01, fevereiro=02, março=03, abril=04, maio=05, junho=06, julho=07, agosto=08, setembro=09, outubro=10, novembro=11, dezembro=12

Fontes conhecidas (use exatamente esses nomes se identificar): Lavanderia, Pousada

Regras:
- valor deve ser número (ex: 35801.70, não "R$ 35.801,70")
- Se o mês não tiver ano, use {ano_atual}
- Responda SOMENTE o JSON, sem texto antes ou depois"""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ── Claude: detecta e extrai fatura de cartão ────────────────────────────────
def eh_fatura(texto):
    t = texto.lower()
    palavras = ["fatura", "cartão de crédito", "cartao de credito", "extrato", "vencimento", "limite disponível"]
    return sum(1 for p in palavras if p in t) >= 2

def extrair_fatura_com_ia(texto, file_bytes=None, file_path=""):
    """Usa Claude para extrair todas as transações de uma fatura de cartão."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    ext = Path(file_path).suffix.lower() if file_path else ""
    content = []
    if ext in (".jpg", ".jpeg", ".png", ".webp") and file_bytes:
        b64 = base64.standard_b64encode(file_bytes).decode()
        media = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})

    prompt = f"""Você é um assistente que extrai dados de faturas de cartão de crédito brasileiras.

Texto da fatura:
{texto}

Extraia o mês de referência da fatura e TODAS as transações/compras listadas.
Responda APENAS com um JSON válido nesse formato:
{{
  "mes_fatura": "AAAA-MM",
  "transacoes": [
    {{
      "valor": 0.00,
      "estabelecimento": "Nome do estabelecimento",
      "descricao": "Descrição curta",
      "categoria": "Categoria",
      "grupo": "kebab-case-opcional"
    }},
    ...
  ]
}}

Categorias disponíveis: Alimentação, Restaurante, Saúde, Moradia, Transporte, Serviços, Beleza, Vestuário, Compras Online, Assinaturas, Educação, Viagem, Casa, Doações, Impostos, Outros

Regras:
- valor deve ser número positivo (ex: 45.90)
- Ignore lançamentos de pagamento da fatura anterior, IOF sobre parcelamentos já registrados, e ajustes internos do cartão
- Inclua todas as compras, incluindo parcelas (ex: "Compra 2/6" é uma parcela válida)
- grupo em kebab-case apenas para estabelecimentos recorrentes
- mes_fatura é o mês a que se referem os gastos (ex: fatura de abril = "2026-04")
- Responda SOMENTE o JSON, sem texto antes ou depois"""

    content.append({"type": "text", "text": prompt})

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": content}]
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)

def adicionar_multiplas_transacoes(transacoes, mes_fatura):
    """Adiciona várias transações de uma vez ao dashboard, todas com data do mês da fatura."""
    with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    ids = re.findall(r'\{ id:(\d+),', html)
    next_id = max(int(i) for i in ids) + 1 if ids else 1

    novas_linhas = []
    for tx in transacoes:
        data_iso = f"{mes_fatura}-01"
        grupo_str = f', grupo:"{tx["grupo"]}"' if tx.get("grupo") else ""
        linha = (
            f'  {{ id:{next_id}, data:"{data_iso}", valor:{tx["valor"]}, '
            f'estabelecimento:"{tx["estabelecimento"]}", '
            f'descricao:"{tx["descricao"]}", '
            f'categoria:"{tx["categoria"]}", '
            f'pagamento:"Cartão"{grupo_str} }}'
        )
        novas_linhas.append(linha)
        next_id += 1

    bloco = ",\n".join(novas_linhas)

    novo_html = re.sub(
        r'(// ── GASTOS ─+\nconst ALL_TX = \[)(.*?)(\n\];)',
        lambda m: m.group(1) + m.group(2) + ",\n" + bloco + m.group(3),
        html,
        flags=re.DOTALL
    )

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(novo_html)


# ── Claude: extrai despesa sem nota de mensagem de texto ──────────────────────
def extrair_despesa_sem_nota_com_ia(texto_msg):
    """Usa Claude para extrair despesa sem comprovante de uma mensagem de texto livre."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    hoje = datetime.now().strftime("%d/%m/%Y")
    ano_atual = datetime.now().year

    prompt = f"""Você é um assistente financeiro. O usuário enviou uma mensagem descrevendo uma despesa sem comprovante/nota fiscal.

Mensagem: "{texto_msg}"

Data de hoje: {hoje}
Ano atual: {ano_atual}

Extraia os dados e responda APENAS com um JSON válido nesse formato:
{{
  "data": "DD/MM/AAAA",
  "valor": 0.00,
  "estabelecimento": "Nome ou descrição do gasto",
  "descricao": "Descrição curta",
  "categoria": "Categoria",
  "pagamento": "Pix|Boleto|Débito|Crédito|Dinheiro|—"
}}

Categorias disponíveis: Alimentação, Restaurante, Saúde, Moradia, Transporte, Serviços, Beleza, Vestuário, Compras Online, Assinaturas, Educação, Viagem, Casa, Doações, Impostos, Outros

Regras:
- valor deve ser número (ex: 1000.00, não "R$ 1.000,00")
- Se a data não tiver ano, use {ano_atual}
- Se não conseguir identificar a data, use hoje ({hoje})
- Se não conseguir identificar a forma de pagamento, use "—"
- Responda SOMENTE o JSON, sem texto antes ou depois"""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ── Atualiza ALL_TX no dashboard ───────────────────────────────────────────────
def proximo_id(html):
    ids = re.findall(r'\{ id:(\d+),', html)
    return max(int(i) for i in ids) + 1 if ids else 1

def adicionar_transacao(tx, sem_comprovante=False):
    """Adiciona uma transação ao dashboard HTML."""
    with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    next_id = proximo_id(html)

    partes = tx["data"].split("/")
    if len(partes) == 3:
        data_iso = f"{partes[2]}-{partes[1]}-{partes[0]}"
    else:
        data_iso = datetime.now().strftime("%Y-%m-%d")

    grupo_str = f', grupo:"{tx["grupo"]}"' if tx.get("grupo") else ""
    sem_comprovante_str = ", semComprovante:true" if sem_comprovante else ""

    nova_linha = (
        f'  {{ id:{next_id}, data:"{data_iso}", valor:{tx["valor"]}, '
        f'estabelecimento:"{tx["estabelecimento"]}", '
        f'descricao:"{tx["descricao"]}", '
        f'categoria:"{tx["categoria"]}", '
        f'pagamento:"{tx["pagamento"]}"{grupo_str}{sem_comprovante_str} }}'
    )

    novo_html = re.sub(
        r'(// ── GASTOS ─+\nconst ALL_TX = \[)(.*?)(\n\];)',
        lambda m: m.group(1) + m.group(2) + ",\n" + nova_linha + m.group(3),
        html,
        flags=re.DOTALL
    )

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(novo_html)

    return next_id


# ── Atualiza ALL_RECEITAS no dashboard ────────────────────────────────────────
def adicionar_receita(receita):
    """Adiciona ou atualiza uma receita no dashboard HTML."""
    with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    mes   = receita["mes"]
    fonte = receita["fonte"]
    valor = receita["valor"]

    # Se já existe entrada para esse mês+fonte, atualiza o valor
    padrao_existente = rf'(\{{ mes:"{mes}", fonte:"{fonte}", valor:)([\d.]+)(\}})'
    if re.search(padrao_existente, html):
        novo_html = re.sub(
            padrao_existente,
            lambda m: f'{m.group(1)}{valor}{m.group(3)}',
            html
        )
        acao = "atualizada"
    else:
        nova_linha = f'  {{ mes:"{mes}", fonte:"{fonte}", valor:{valor} }}'
        novo_html = re.sub(
            r'(const ALL_RECEITAS = \[)(.*?)(\n\];)',
            lambda m: m.group(1) + m.group(2) + ",\n" + nova_linha + m.group(3),
            html,
            flags=re.DOTALL
        )
        acao = "adicionada"

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(novo_html)

    return acao


# ── Detecta tipo de mensagem de texto ─────────────────────────────────────────
def eh_receita(texto):
    t = texto.lower()
    return "receita" in t or "faturamento" in t or "entrada" in t

def eh_despesa_sem_nota(texto):
    t = texto.lower()
    return any(p in t for p in ["sem nota", "sem comprovante", "despesa sem", "gasto sem"])


# ── Contas fixas: persistência ────────────────────────────────────────────────
def ler_contas():
    if CONTAS_FILE.exists():
        return json.loads(CONTAS_FILE.read_text(encoding="utf-8"))
    return {"contas": []}

def salvar_contas(dados):
    CONTAS_FILE.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Contas fixas: data de vencimento ──────────────────────────────────────────
def proxima_data_vencimento(dia_venc):
    hoje = date.today()
    ultimo_dia = calendar.monthrange(hoje.year, hoje.month)[1]
    dia_real = min(dia_venc, ultimo_dia)
    venc_este_mes = date(hoje.year, hoje.month, dia_real)
    if venc_este_mes >= hoje:
        return venc_este_mes
    # Próximo mês
    if hoje.month == 12:
        prox = date(hoje.year + 1, 1, 1)
    else:
        prox = date(hoje.year, hoje.month + 1, 1)
    ultimo_dia_prox = calendar.monthrange(prox.year, prox.month)[1]
    return date(prox.year, prox.month, min(dia_venc, ultimo_dia_prox))


# ── Contas fixas: lembretes diários ───────────────────────────────────────────
def verificar_lembretes():
    dados = ler_contas()
    contas = dados.get("contas", [])
    hoje = date.today()
    hoje_str = hoje.strftime("%Y-%m-%d")
    modificado = False

    for conta in contas:
        prox_venc  = proxima_data_vencimento(conta["dia_vencimento"])
        mes_venc   = prox_venc.strftime("%Y-%m")
        dias_falta = (prox_venc - hoje).days

        if conta.get("pago_mes") == mes_venc:
            continue
        if dias_falta > 1:
            continue
        if conta.get("ultimo_lembrete") == hoje_str:
            continue

        valor_fmt = f"R$ {conta['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if dias_falta == 1:
            urgencia = f"vence *amanhã* ({prox_venc.strftime('%d/%m')})"
        elif dias_falta == 0:
            urgencia = f"vence *hoje* ({prox_venc.strftime('%d/%m')})"
        else:
            urgencia = f"venceu em {prox_venc.strftime('%d/%m')} — *em atraso!*"

        send_message(
            f"🔔 *Lembrete de conta*\n\n"
            f"📋 {conta['descricao']}\n"
            f"💰 {valor_fmt}\n"
            f"📅 {urgencia}\n\n"
            f"Quando pagar, me diga:\n`paguei {conta['descricao']}`"
        )

        conta["ultimo_lembrete"] = hoje_str
        modificado = True

    if modificado:
        salvar_contas(dados)


# ── Contas fixas: Claude parseia texto livre ───────────────────────────────────
def extrair_conta_fixa_com_ia(texto_msg):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""O usuário quer cadastrar uma conta fixa mensal.

Mensagem: "{texto_msg}"

Extraia os dados e responda APENAS com JSON válido:
{{
  "descricao": "Nome da conta",
  "dia_vencimento": 15,
  "valor": 250.00
}}

Regras:
- dia_vencimento é o dia do mês (1 a 31)
- valor deve ser número (ex: 250.00)
- descricao deve ser curta e clara
- Responda SOMENTE o JSON"""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def identificar_conta_paga_com_ia(texto_msg, contas):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    lista = "\n".join(f"- {c['descricao']}" for c in contas)
    prompt = f"""O usuário disse que pagou uma conta. Identifique qual conta da lista ele se refere.

Mensagem: "{texto_msg}"

Contas cadastradas:
{lista}

Responda APENAS com o nome exato da conta (copiado da lista acima), ou "nenhuma" se não identificar."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()


# ── Contas fixas: ações ────────────────────────────────────────────────────────
def adicionar_conta_fixa(nova_conta):
    dados = ler_contas()
    ids = [c["id"] for c in dados["contas"]] or [0]
    nova_conta["id"] = max(ids) + 1
    nova_conta["pago_mes"] = None
    nova_conta["ultimo_lembrete"] = None
    dados["contas"].append(nova_conta)
    salvar_contas(dados)

def marcar_conta_paga(descricao_exata):
    dados = ler_contas()
    hoje = date.today()
    for conta in dados["contas"]:
        if conta["descricao"] == descricao_exata:
            prox_venc = proxima_data_vencimento(conta["dia_vencimento"])
            conta["pago_mes"] = prox_venc.strftime("%Y-%m")
            salvar_contas(dados)
            return True
    return False

def listar_contas_texto():
    dados = ler_contas()
    contas = dados.get("contas", [])
    if not contas:
        return "Nenhuma conta fixa cadastrada.\n\nPara cadastrar: `conta fixa: Nome, dia X, R$ valor`"

    hoje = date.today()
    linhas = []
    for c in contas:
        prox_venc = proxima_data_vencimento(c["dia_vencimento"])
        mes_venc  = prox_venc.strftime("%Y-%m")
        valor_fmt = f"R$ {c['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if c.get("pago_mes") == mes_venc:
            status = "✅ paga"
        else:
            dias = (prox_venc - hoje).days
            if dias < 0:
                status = f"⚠️ em atraso ({prox_venc.strftime('%d/%m')})"
            elif dias == 0:
                status = f"🔴 vence hoje"
            elif dias == 1:
                status = f"🟡 vence amanhã"
            else:
                status = f"🔵 vence dia {c['dia_vencimento']}"
        linhas.append(f"• *{c['descricao']}* — {valor_fmt} — {status}")

    return "📋 *Suas contas fixas:*\n\n" + "\n".join(linhas)


# ── Detectores de intenção para contas fixas ──────────────────────────────────
def eh_conta_fixa(texto):
    t = texto.lower()
    return any(p in t for p in ["conta fixa", "despesa fixa", "conta mensal", "despesa mensal", "lembrar de pagar", "me lembra"])

def eh_marcar_pago(texto):
    t = texto.lower()
    return any(p in t for p in ["paguei", "já paguei", "ja paguei", "marcar como pago", "marquei como pago", "foi pago"])

def eh_listar_contas(texto):
    t = texto.lower()
    return any(p in t for p in ["ver contas", "listar contas", "minhas contas", "quais contas", "contas fixas"])


# ── Verificação de saldo da Anthropic ─────────────────────────────────────────
SALDO_MINIMO_USD = 2.00

def verificar_saldo():
    try:
        r2 = requests.get(
            "https://api.anthropic.com/v1/billing/credit_balance",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            timeout=10
        )
        if r2.status_code == 200:
            data = r2.json()
            saldo = data.get("available_credit", data.get("balance", None))
            if saldo is not None:
                saldo_usd = float(saldo) / 100
                if saldo_usd < SALDO_MINIMO_USD:
                    send_message(
                        f"⚠️ *Saldo baixo na API Anthropic!*\n\n"
                        f"Saldo atual: *${saldo_usd:.2f}*\n"
                        f"Quando acabar, o bot para de processar comprovantes.\n\n"
                        f"Adicione créditos em:\nhttps://console.anthropic.com/settings/billing"
                    )
                    print(f"Aviso de saldo baixo enviado: ${saldo_usd:.2f}")
                else:
                    print(f"Saldo OK: ${saldo_usd:.2f}")
    except Exception as e:
        print(f"Não foi possível verificar saldo: {e}")


# ── Estado: controla offset do Telegram ───────────────────────────────────────
def ler_estado():
    if ESTADO_FILE.exists():
        return json.loads(ESTADO_FILE.read_text())
    return {"offset": 0, "ultima_verificacao_saldo": ""}

def salvar_estado(estado):
    ESTADO_FILE.write_text(json.dumps(estado, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    estado = ler_estado()
    offset = estado.get("offset", 0)

    hoje = datetime.now().strftime("%Y-%m-%d")
    if estado.get("ultima_verificacao_saldo") != hoje:
        verificar_saldo()
        estado["ultima_verificacao_saldo"] = hoje

    verificar_lembretes()

    updates = get_updates(offset)
    if not updates:
        print("Nenhuma mensagem nova.")
        return

    houve_atualizacao = False

    for upd in updates:
        offset = upd["update_id"] + 1
        msg = upd.get("message", {})

        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID):
            print(f"Mensagem de chat não autorizado: {chat_id}")
            continue

        file_id        = None
        file_path_hint = ""

        caption = msg.get("caption", "").strip()

        if "photo" in msg:
            file_id = msg["photo"][-1]["file_id"]
            file_path_hint = "comprovante.jpg"
        elif "document" in msg:
            doc = msg["document"]
            mime = doc.get("mime_type", "")
            if "pdf" in mime or "image" in mime:
                file_id = doc["file_id"]
                file_path_hint = doc.get("file_name", "comprovante.pdf")

        if not file_id:
            texto_msg = msg.get("text", "").strip()

            # Comprovante pendente aguardando contexto do usuário
            if estado.get("aguardando_contexto") and texto_msg and texto_msg.lower() not in ("/start", "/ajuda", "/help"):
                pendente = estado["aguardando_contexto"]
                try:
                    send_message("⏳ Processando com o contexto informado...")
                    tx = extrair_dados_com_ia(
                        pendente["texto_comprovante"],
                        contexto=texto_msg
                    )
                    tx_id = adicionar_transacao(tx)
                    houve_atualizacao = True
                    estado["aguardando_contexto"] = None
                    valor_fmt = f"R$ {tx['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    send_message(
                        f"✅ *Comprovante registrado!*\n\n"
                        f"📅 Data: {tx['data']}\n"
                        f"🏪 {tx['estabelecimento']}\n"
                        f"💰 {valor_fmt}\n"
                        f"🏷 {tx['categoria']} · {tx['pagamento']}\n\n"
                        f"Dashboard atualizado em instantes:\n"
                        f"https://diretoriamc.github.io/dashboard-gastos/"
                    )
                except Exception as e:
                    send_message(f"❌ Erro ao processar com contexto: {str(e)}")
                continue

            # Comandos de ajuda
            if texto_msg.lower() in ("/start", "/ajuda", "/help"):
                send_message(
                    "👋 *Bot de Gastos*\n\n"
                    "📎 *Comprovante:* mande a foto ou PDF\n"
                    "🗂 *Fatura do cartão:* mande o PDF da fatura\n\n"
                    "💰 *Receita:*\n"
                    "`receita em maio: 35.000 na lavanderia, 25.000 na pousada`\n\n"
                    "🧾 *Despesa sem nota:*\n"
                    "`despesa sem nota em maio: 1.000 em 20/05 - descrição`\n\n"
                    "🔔 *Cadastrar conta fixa:*\n"
                    "`conta fixa: Neo Energia, dia 15, R$ 250`\n\n"
                    "✅ *Marcar conta como paga:*\n"
                    "`paguei Neo Energia`\n\n"
                    "📋 *Ver contas fixas:*\n"
                    "`ver contas`\n\n"
                    "Dashboard: https://diretoriamc.github.io/dashboard-gastos/"
                )
                continue

            # Conta fixa — cadastrar
            if eh_conta_fixa(texto_msg):
                try:
                    nova = extrair_conta_fixa_com_ia(texto_msg)
                    adicionar_conta_fixa(nova)
                    valor_fmt = f"R$ {nova['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    send_message(
                        f"✅ *Conta fixa cadastrada!*\n\n"
                        f"📋 {nova['descricao']}\n"
                        f"💰 {valor_fmt}\n"
                        f"📅 Vence todo dia {nova['dia_vencimento']}\n\n"
                        f"Vou te lembrar no dia anterior ao vencimento."
                    )
                except Exception as e:
                    send_message(f"❌ Erro ao cadastrar conta: {str(e)}")
                continue

            # Conta fixa — marcar como paga
            if eh_marcar_pago(texto_msg):
                try:
                    dados = ler_contas()
                    contas = dados.get("contas", [])
                    if not contas:
                        send_message("Você não tem contas fixas cadastradas.")
                    else:
                        nome = identificar_conta_paga_com_ia(texto_msg, contas)
                        if nome == "nenhuma":
                            send_message(
                                "Não identifiquei qual conta foi paga. "
                                "Tente ser mais específico, ex: `paguei Neo Energia`"
                            )
                        elif marcar_conta_paga(nome):
                            send_message(f"✅ *{nome}* marcada como paga! Não vou mais lembrar até o próximo mês.")
                        else:
                            send_message(f"Não encontrei a conta *{nome}* na lista.")
                except Exception as e:
                    send_message(f"❌ Erro: {str(e)}")
                continue

            # Conta fixa — listar
            if eh_listar_contas(texto_msg):
                send_message(listar_contas_texto())
                continue

            # Receita
            if eh_receita(texto_msg):
                try:
                    send_message("⏳ Processando receita...")
                    receitas = extrair_receitas_com_ia(texto_msg)
                    linhas = []
                    for r in receitas:
                        acao = adicionar_receita(r)
                        valor_fmt = f"R$ {r['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                        linhas.append(f"• {r['fonte']}: {valor_fmt} ({acao})")
                    houve_atualizacao = True
                    send_message(
                        f"✅ *Receita registrada!*\n\n"
                        + "\n".join(linhas) +
                        f"\n\nDashboard atualizado em instantes:\n"
                        f"https://diretoriamc.github.io/dashboard-gastos/"
                    )
                except Exception as e:
                    send_message(f"❌ Erro ao processar receita: {str(e)}")
                    print(f"Erro receita: {e}")
                continue

            # Despesa sem nota
            if eh_despesa_sem_nota(texto_msg):
                try:
                    send_message("⏳ Processando despesa sem nota...")
                    tx = extrair_despesa_sem_nota_com_ia(texto_msg)
                    tx_id = adicionar_transacao(tx, sem_comprovante=True)
                    houve_atualizacao = True
                    valor_fmt = f"R$ {tx['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    send_message(
                        f"✅ *Despesa sem nota registrada!*\n\n"
                        f"📅 Data: {tx['data']}\n"
                        f"📝 {tx['descricao']}\n"
                        f"💰 {valor_fmt}\n"
                        f"🏷 {tx['categoria']}\n\n"
                        f"Dashboard atualizado em instantes:\n"
                        f"https://diretoriamc.github.io/dashboard-gastos/"
                    )
                except Exception as e:
                    send_message(f"❌ Erro ao processar despesa sem nota: {str(e)}")
                    print(f"Erro despesa sem nota: {e}")
                continue

            # Mensagem de texto não reconhecida
            if texto_msg:
                send_message(
                    "Não entendi. Digite /ajuda para ver tudo que sei fazer."
                )
            continue

        # ── Processar comprovante ou fatura (foto ou PDF) ────────────────────
        try:
            if estado.get("aguardando_contexto"):
                pendente = estado["aguardando_contexto"]
                tx_pendente = pendente.get("dados_parciais", {})
                if tx_pendente:
                    tx_pendente["categoria"] = "Outros"
                    tx_pendente.pop("precisa_confirmacao", None)
                    adicionar_transacao(tx_pendente)
                    houve_atualizacao = True
                    valor_fmt = f"R$ {tx_pendente['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    send_message(
                        f"⚠️ Comprovante anterior registrado como *Outros* (sem classificação):\n"
                        f"{tx_pendente['estabelecimento']} · {valor_fmt}\n\n"
                        f"Processando novo comprovante..."
                    )
                estado["aguardando_contexto"] = None

            file_bytes, real_path = download_file(file_id)
            file_path_hint = real_path or file_path_hint
            texto = extrair_texto(file_bytes, file_path_hint)

            if eh_fatura(texto):
                send_message("⏳ Fatura detectada! Extraindo todas as transações...")
                resultado = extrair_fatura_com_ia(texto, file_bytes, file_path_hint)
                mes_fatura = resultado["mes_fatura"]
                transacoes = resultado["transacoes"]
                adicionar_multiplas_transacoes(transacoes, mes_fatura)
                houve_atualizacao = True

                total = sum(t["valor"] for t in transacoes)
                total_fmt = f"R$ {total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                ano, mes = mes_fatura.split("-")
                meses_pt = {"01":"janeiro","02":"fevereiro","03":"março","04":"abril",
                            "05":"maio","06":"junho","07":"julho","08":"agosto",
                            "09":"setembro","10":"outubro","11":"novembro","12":"dezembro"}
                mes_nome = meses_pt.get(mes, mes)
                send_message(
                    f"✅ *Fatura de {mes_nome}/{ano} registrada!*\n\n"
                    f"🧾 {len(transacoes)} transações adicionadas\n"
                    f"💰 Total: {total_fmt}\n\n"
                    f"Dashboard atualizado em instantes:\n"
                    f"https://diretoriamc.github.io/dashboard-gastos/"
                )
            else:
                send_message("⏳ Processando comprovante...")
                tx = extrair_dados_com_ia(texto, file_bytes, file_path_hint, contexto=caption)

                if not caption and tx.pop("precisa_confirmacao", False):
                    estado["aguardando_contexto"] = {"texto_comprovante": texto, "dados_parciais": tx}
                    valor_fmt = f"R$ {tx['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    send_message(
                        f"🤔 *Não identifiquei bem esse comprovante*\n\n"
                        f"Valor: {valor_fmt} · {tx['data']}\n"
                        f"Beneficiário: {tx['estabelecimento']}\n\n"
                        f"Do que se trata essa despesa? Me responda com uma descrição, ex:\n"
                        f"_almoço de trabalho_, _médico particular_, _material de limpeza_..."
                    )
                else:
                    tx_id = adicionar_transacao(tx)
                    houve_atualizacao = True
                    valor_fmt = f"R$ {tx['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    send_message(
                        f"✅ *Comprovante registrado!*\n\n"
                        f"📅 Data: {tx['data']}\n"
                        f"🏪 {tx['estabelecimento']}\n"
                        f"💰 {valor_fmt}\n"
                        f"🏷 {tx['categoria']} · {tx['pagamento']}\n\n"
                        f"Dashboard atualizado em instantes:\n"
                        f"https://diretoriamc.github.io/dashboard-gastos/"
                    )

        except Exception as e:
            send_message(f"❌ Erro ao processar arquivo: {str(e)}")
            print(f"Erro: {e}")

    estado["offset"] = offset
    salvar_estado(estado)

    if houve_atualizacao:
        print("Dashboard atualizado com novas transações.")
    else:
        print("Nenhuma mensagem nova processada.")


if __name__ == "__main__":
    main()
