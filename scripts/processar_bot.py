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
from datetime import datetime
from pathlib import Path

# ── Configurações ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
REPO_DIR         = Path(__file__).parent.parent
DASHBOARD_FILE   = REPO_DIR / "index.html"
ESTADO_FILE      = REPO_DIR / "estado_bot.json"

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
def extrair_dados_com_ia(texto, file_bytes=None, file_path=""):
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

    prompt = f"""Você é um assistente que extrai dados de comprovantes de pagamento brasileiros.

{"Texto extraído por OCR:" if texto.strip() else "Analise a imagem do comprovante."}
{texto if texto.strip() else ""}

Data de hoje: {hoje}

Extraia os dados e responda APENAS com um JSON válido nesse formato:
{{
  "data": "DD/MM/AAAA",
  "valor": 0.00,
  "estabelecimento": "Nome do estabelecimento/beneficiário",
  "descricao": "Descrição curta do gasto",
  "categoria": "Categoria",
  "pagamento": "Pix|Boleto|Débito|Crédito|Dinheiro",
  "grupo": "kebab-case-para-agrupar-recorrentes"
}}

Categorias disponíveis: Alimentação, Restaurante, Saúde, Moradia, Transporte, Serviços, Beleza, Vestuário, Compras Online, Assinaturas, Educação, Viagem, Casa, Doações, Impostos, Outros

Regras:
- Se não conseguir identificar a data, use a data de hoje ({hoje})
- valor deve ser número (ex: 45.90, não "R$ 45,90")
- grupo em kebab-case, apenas para estabelecimentos recorrentes (ex: "supermercado-soberano")
- Se for compra no cartão de crédito, pagamento = "Cartão"
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

            # Comandos de ajuda
            if texto_msg.lower() in ("/start", "/ajuda", "/help"):
                send_message(
                    "👋 *Bot de Gastos*\n\n"
                    "📎 *Comprovante:* mande a foto ou PDF\n\n"
                    "💰 *Receita:* escreva algo como:\n"
                    "`receita em maio: 35.000 na lavanderia, 25.000 na pousada`\n\n"
                    "🧾 *Despesa sem nota:* escreva algo como:\n"
                    "`despesa sem nota em maio: 1.000 em 20/05 - descrição`\n\n"
                    "Dashboard: https://diretoriamc.github.io/dashboard-gastos/"
                )
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
                    "Não entendi. Você pode:\n\n"
                    "📎 Mandar a *foto ou PDF* do comprovante\n"
                    "💰 Escrever `receita em [mês]: valor na [fonte]`\n"
                    "🧾 Escrever `despesa sem nota em [mês]: valor em [data]`\n\n"
                    "Digite /ajuda para ver exemplos."
                )
            continue

        # ── Processar comprovante (foto ou PDF) ───────────────────────────────
        try:
            send_message("⏳ Processando comprovante...")

            file_bytes, real_path = download_file(file_id)
            file_path_hint = real_path or file_path_hint

            texto = extrair_texto(file_bytes, file_path_hint)
            tx = extrair_dados_com_ia(texto, file_bytes, file_path_hint)
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
            send_message(f"❌ Erro ao processar comprovante: {str(e)}")
            print(f"Erro: {e}")

    estado["offset"] = offset
    salvar_estado(estado)

    if houve_atualizacao:
        print("Dashboard atualizado com novas transações.")
    else:
        print("Nenhuma mensagem nova processada.")


if __name__ == "__main__":
    main()
