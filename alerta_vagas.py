"""Alerta de Vagas: avisa por e-mail quando surge uma vaga aderente ao perfil.

Como usar:
    python alerta_vagas.py                 busca as vagas, envia o alerta e grava as vistas
    python alerta_vagas.py --validar       confere tudo, nao envia e-mail; so grava
                                           a contagem de chamadas da Adzuna
    python alerta_vagas.py --teste-email   manda 1 e-mail de exemplo
"""

import argparse
import csv
import email
import hashlib
import imaplib
import os
import re
import smtplib
import ssl
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from html import escape
from html.parser import HTMLParser
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

PASTA_PROJETO = Path(__file__).parent

SMTP_SERVIDOR = "smtp.gmail.com"
SMTP_PORTA = 465  # porta do Gmail com conexao criptografada (SSL)
SMTP_TIMEOUT = 30  # segundos

IMAP_SERVIDOR = "imap.gmail.com"
IMAP_PORTA = 993
IMAP_TIMEOUT = 30  # segundos

ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/br/search/1"
ADZUNA_VAGAS_POR_PAGINA = 50
ADZUNA_MAX_DIAS = 3  # so vagas publicadas nos ultimos dias
ADZUNA_MESES_NO_ANO = 12  # a Adzuna informa o salario por ano
ADZUNA_TIMEOUT = 30  # segundos
ADZUNA_PAUSA = 2  # segundos entre uma consulta e outra
ADZUNA_LIMITE_SEMANAL = 1000  # limite do plano gratis (o mais apertado)
ADZUNA_HTTP_RECUSA = {401, 402, 403, 429}  # acesso negado ou limite atingido
ADZUNA_ARQUIVO_USO = PASTA_PROJETO / "dados" / "adzuna_uso.csv"
ADZUNA_CAMPOS_USO = ["data_hora_utc", "busca", "status", "total_acumulado"]
AVISO_ADZUNA = "Adzuna indisponível (limite ou plano)"
EXECUCOES_POR_DIA = 6  # agenda: 7h, 10h, 13h, 16h, 19h e 22h

# Pontos da nota (0 a 100)
PONTOS_CARGO = {1: 50, 2: 40, 3: 30}
PONTOS_COMPETENCIA = 5
MAX_PONTOS_COMPETENCIAS = 25
PONTOS_MODELO_PREFERIDO = 10
PONTOS_SENIORIDADE = 15
PONTOS_LACUNA = 10
MAX_DESCONTO_LACUNAS = 20
PONTOS_ALERTA_PJ = 10

ALERTA_PJ = "⚠ verificar se é PJ"
ALERTA_SEM_DESCRICAO = "⚠ alerta sem descrição: confira modelo e regime CLT na página da vaga"

# Como os nomes internos aparecem no e-mail, com acento
ROTULOS_MODELO = {
    "hibrido": "Híbrido",
    "remoto": "Remoto",
    "presencial": "Presencial",
    "nao informado": "Não informado",
}
ROTULOS_NIVEL = {"junior": "júnior", "pleno": "pleno", "senior": "sênior"}
ROTULOS_DESCARTE = {
    "palavra excluida": "palavra excluída",
    "palavra excluida no titulo": "palavra excluída no título",
    "salario abaixo do minimo": "salário abaixo do mínimo",
    "nota abaixo do minimo": "nota abaixo do mínimo",
}

# Valor mensal menor que este e erro de dado, nao salario real ("R$ 7,4 por mes").
SALARIO_MENSAL_VALIDO = 1000

# Textos do alerta da Adzuna que nao fazem parte da vaga
ETIQUETAS_ALERTA_ADZUNA = {"NOVA", "FEITA PARA VOCÊ", "Leia mais »"}

# Textos do alerta do Indeed que nao fazem parte da descricao
ETIQUETAS_ALERTA_INDEED = re.compile(r"^(há \d+ dias?|hoje|novo|candidate-se)", re.IGNORECASE)

# Parecido a partir daqui e a mesma vaga. "Jr" e "Pl" no titulo ficam abaixo disso.
SIMILARIDADE_TITULO = 0.92

ARQUIVO_VISTAS = PASTA_PROJETO / "dados" / "vagas_vistas.csv"
CAMPOS_VISTAS = ["id", "fonte", "titulo", "empresa", "link", "visto_em_utc", "enviada"]
DIAS_SEM_REPETIR = 30  # mesma empresa e titulo parecido nao voltam neste prazo

ARQUIVO_ULTIMO_AVISO = PASTA_PROJETO / "dados" / "ultimo_aviso.txt"
FUSO_LOCAL = timezone(timedelta(hours=-3))

# Outras formas de escrever cada nivel no titulo da vaga
APELIDOS_SENIORIDADE = {
    "estagio": ["estagio", "estagiario", "estagiaria"],
    "junior": ["junior", "jr"],
    "pleno": ["pleno", "pl"],
    "senior": ["senior", "sr"],
    "coordenador": ["coordenador", "coordenadora"],
}

TERMOS_HIBRIDO = ["hibrido", "hibrida"]
TERMOS_REMOTO = ["remoto", "remota", "home office", "homeoffice", "teletrabalho"]
TERMOS_PRESENCIAL = ["presencial"]

PALAVRAS_VAZIAS = {"a", "o", "e", "de", "da", "do", "em"}


# --- Texto ---


def normalizar(texto):
    """Deixa o texto em minusculas, sem acentos e sem pontuacao (mantem o & de P&D)."""
    sem_acento = (
        unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    )
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", sem_acento.lower()).split())


def tem_termo(texto_normalizado, termo):
    """Procura o termo como palavra inteira: "mei" nao casa com "meio"."""
    return f" {normalizar(termo)} " in f" {texto_normalizado} "


def tem_algum(texto_normalizado, termos):
    return any(tem_termo(texto_normalizado, termo) for termo in termos)


def separar_alternativas(itens):
    """Transforma "indicadores / KPIs" em dois termos: "indicadores" e "KPIs"."""
    return [parte.strip() for item in itens for parte in item.split("/")]


# --- Perfil ---


def ler_perfil():
    arquivo_perfil = PASTA_PROJETO / "perfil.yaml"
    if not arquivo_perfil.exists():
        arquivo_perfil = PASTA_PROJETO / "perfil.exemplo.yaml"
    with open(arquivo_perfil, encoding="utf-8") as arquivo:
        return yaml.safe_load(arquivo)


# --- Coleta: Adzuna ---


class FonteRecusouAcesso(ConnectionError):
    """A fonte recusou a consulta por limite de uso ou plano."""


def ler_uso_adzuna():
    if not ADZUNA_ARQUIVO_USO.exists():
        return []
    with open(ADZUNA_ARQUIVO_USO, encoding="utf-8", newline="") as arquivo:
        return list(csv.DictReader(arquivo))


def chamadas_na_semana(uso):
    """Conta as chamadas dos ultimos 7 dias, a janela do limite semanal."""
    inicio = datetime.now(timezone.utc) - timedelta(days=7)
    return sum(
        datetime.fromisoformat(linha["data_hora_utc"]) >= inicio for linha in uso
    )


def registrar_chamada_adzuna(busca, status):
    """Acrescenta 1 chamada ao historico. Nunca grava URL nem chave."""
    novo = not ADZUNA_ARQUIVO_USO.exists()
    total = len(ler_uso_adzuna()) + 1
    with open(ADZUNA_ARQUIVO_USO, "a", encoding="utf-8", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, ADZUNA_CAMPOS_USO, lineterminator="\n")
        if novo:
            escritor.writeheader()
        escritor.writerow(
            {
                "data_hora_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "busca": " / ".join(busca.values()),
                "status": status,
                "total_acumulado": total,
            }
        )


def adzuna_para_vaga(item):
    """Converte uma vaga da Adzuna para o formato usado no projeto."""
    # Salario estimado pela Adzuna nao conta como salario informado.
    estimado = str(item.get("salary_is_predicted", "0")) == "1"
    salario_anual = None
    if not estimado:
        salario_anual = item.get("salary_max") or item.get("salary_min")
    salario = salario_anual / ADZUNA_MESES_NO_ANO if salario_anual else None

    local = item.get("location") or {}
    return {
        "id": f"adzuna-{item.get('id')}",
        "fonte": "Adzuna",
        "titulo": item.get("title") or "",
        "empresa": (item.get("company") or {}).get("display_name") or "Não informada",
        "local": local.get("display_name") or "",
        "regioes": local.get("area") or [],
        "descricao": item.get("description") or "",
        "salario": salario,
        # Tira os parametros do link, que trazem dados da conta da Adzuna.
        "link": (item.get("redirect_url") or "").split("?")[0],
    }


def coletar_adzuna(perfil):
    load_dotenv(PASTA_PROJETO / ".env")
    app_id = os.getenv("ADZUNA_APP_ID", "").strip()
    app_key = os.getenv("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        raise ValueError("faltando preencher ADZUNA_APP_ID e ADZUNA_APP_KEY")

    busca = perfil["busca_adzuna"]
    parametros = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": ADZUNA_VAGAS_POR_PAGINA,
        "what_or": " ".join(busca["palavras_alternativas"]),
        "max_days_old": ADZUNA_MAX_DIAS,
        "sort_by": "date",
    }

    uso_semana = chamadas_na_semana(ler_uso_adzuna())
    if uso_semana + len(busca["buscas"]) > ADZUNA_LIMITE_SEMANAL:
        raise FonteRecusouAcesso(
            f"limite semanal protegido: {uso_semana} de {ADZUNA_LIMITE_SEMANAL} chamadas"
        )

    vagas = []
    chamadas = 0
    try:
        for extras in busca["buscas"]:
            if chamadas:
                time.sleep(ADZUNA_PAUSA)
            # Atencao: o endereco leva a chave, entao ele nunca pode ir para o log.
            chamadas += 1
            try:
                resposta = requests.get(
                    ADZUNA_URL,
                    params={**parametros, **extras},
                    timeout=ADZUNA_TIMEOUT,
                )
            except requests.RequestException as erro:
                registrar_chamada_adzuna(extras, "sem resposta")
                raise ConnectionError(f"sem resposta ({type(erro).__name__})") from None

            registrar_chamada_adzuna(extras, resposta.status_code)
            if resposta.status_code in ADZUNA_HTTP_RECUSA:
                raise FonteRecusouAcesso(f"acesso recusado (HTTP {resposta.status_code})")
            if resposta.status_code != 200:
                raise ConnectionError(f"resposta HTTP {resposta.status_code}")

            resultados = resposta.json().get("results", [])
            vagas.extend(adzuna_para_vaga(item) for item in resultados)
    finally:
        # Registra mesmo se der erro: chamada com falha tambem conta no limite.
        uso = ler_uso_adzuna()
        print(
            f"Chamadas a Adzuna nesta execucao: {chamadas} "
            f"(ultimos 7 dias: {chamadas_na_semana(uso)} de {ADZUNA_LIMITE_SEMANAL}; "
            f"total acumulado: {len(uso)})."
        )

    return vagas


# --- Coleta: alertas do Gmail ---


class LeitorAlerta(HTMLParser):
    """Guarda os textos que aparecem depois de cada link de vaga do e-mail."""

    def __init__(self, padrao_link):
        super().__init__()
        self.padrao_link = padrao_link
        self.vaga_atual = None
        self.textos = {}
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        link = dict(attrs).get("href") or ""
        achado = re.search(self.padrao_link, link)
        self.vaga_atual = achado.group(1) if achado else None
        if self.vaga_atual:
            self.textos.setdefault(self.vaga_atual, [])
            self.links.setdefault(self.vaga_atual, link)

    def handle_data(self, data):
        # O ͏ e o ‌ sao caracteres invisiveis usados nos e-mails de alerta.
        texto = " ".join(data.split()).strip("͏‌ ")
        if texto and self.vaga_atual:
            self.textos[self.vaga_atual].append(texto)


def salario_do_alerta(texto):
    """Le "R$ 4.000-R$ 5.000 por mes". Sem valor confiavel, devolve None."""
    valores = re.findall(r"R\$\s*([\d.,]+)", texto)
    if not valores:
        return None

    salario = max(float(v.replace(".", "").replace(",", ".")) for v in valores)
    periodo = normalizar(texto)
    if "por ano" in periodo:
        salario /= ADZUNA_MESES_NO_ANO
    elif "por mes" not in periodo:
        return None
    return salario if salario >= SALARIO_MENSAL_VALIDO else None


def linkedin_para_vaga(codigo, textos):
    """Monta a vaga: titulo, depois "Empresa · Local (Modelo)", depois o salario."""
    empresa_local = next((texto for texto in textos if " · " in texto), None)
    if not textos or not empresa_local:
        return None

    empresa, local = empresa_local.split(" · ", 1)
    modelo = re.search(r"\(([^)]*)\)\s*$", local)
    salario = next((texto for texto in textos if texto.startswith("R$")), "")
    return {
        "id": f"linkedin-{codigo}",
        "fonte": "LinkedIn",
        "titulo": textos[0],
        "empresa": empresa,
        "local": re.sub(r"\s*\([^)]*\)\s*$", "", local),
        "regioes": [],
        "descricao": modelo.group(1) if modelo else "",
        "salario": salario_do_alerta(salario),
        "link": f"https://www.linkedin.com/jobs/view/{codigo}/",
        "sem_descricao": True,
    }


def alerta_adzuna_para_vaga(codigo, textos):
    """Monta a vaga: titulo, etiquetas e depois "Empresa - Cidade"."""
    uteis = [texto for texto in textos if texto not in ETIQUETAS_ALERTA_ADZUNA]
    empresa_local = next((texto for texto in uteis[1:] if " - " in texto), None)
    if not empresa_local:
        return None

    empresa, local = empresa_local.rsplit(" - ", 1)
    return {
        # Mesmo codigo da API: a vaga vista pelos dois caminhos e uma so.
        "id": f"adzuna-{codigo}",
        "fonte": "Adzuna",
        "titulo": uteis[0],
        "empresa": empresa,
        "local": local,
        "regioes": [],
        "descricao": "",
        "salario": None,
        "link": f"https://www.adzuna.com.br/details/{codigo}",
        "sem_descricao": True,
    }


def alerta_indeed_para_vaga(textos, link):
    """Monta a vaga: titulo, empresa, nota da empresa, "- Cidade", salario e descricao."""
    posicao = next(i for i, texto in enumerate(textos) if texto.startswith("- "))
    titulo = textos[0]
    empresa = textos[1] if posicao > 1 else "Não informada"
    local = textos[posicao][2:]
    resto = textos[posicao + 1:]
    salario = next((texto for texto in resto if texto.startswith("R$")), "")
    descricao = [
        texto
        for texto in resto
        if not texto.startswith("R$") and not ETIQUETAS_ALERTA_INDEED.match(texto)
    ]

    # O link muda a cada e-mail: o codigo estavel vem dos dados da vaga.
    chave = normalizar(f"{titulo} {empresa} {local}")
    codigo = hashlib.sha256(chave.encode()).hexdigest()[:16]
    return {
        "id": f"indeed-{codigo}",
        "fonte": "Indeed",
        "titulo": titulo,
        "empresa": empresa,
        "local": local,
        "regioes": [],
        "descricao": " ".join(descricao),
        "salario": salario_do_alerta(salario),
        "link": link,
        "sem_descricao": True,
    }


def extrair_vagas(html, padrao_link, montar_vaga):
    leitor = LeitorAlerta(padrao_link)
    leitor.feed(html)

    vagas = []
    for codigo, textos in leitor.textos.items():
        vaga = montar_vaga(codigo, textos)
        if vaga:
            vagas.append(vaga)
        else:
            print(f"Alerta por e-mail: formato nao reconhecido na vaga {codigo}.")
    return vagas


def extrair_vagas_linkedin(html):
    padrao = r"/jobs/view/(?:[^/?\"]*-)?(\d+)"
    return extrair_vagas(html, padrao, linkedin_para_vaga)


def extrair_vagas_adzuna(html):
    return extrair_vagas(html, r"[?&]topdoc_id=(\d+)", alerta_adzuna_para_vaga)


def extrair_vagas_indeed(html):
    leitor = LeitorAlerta(r"engage\.indeed\.com/f/a/([^/]+)/")
    leitor.feed(html)
    # Links de rodape e cancelamento nao tem a linha "- Cidade".
    return [
        alerta_indeed_para_vaga(textos, leitor.links[codigo])
        for codigo, textos in leitor.textos.items()
        if any(texto.startswith("- ") for texto in textos)
    ]


LEITORES_GMAIL = {
    "jobalerts-noreply@linkedin.com": extrair_vagas_linkedin,
    "no-reply@adzuna.com.br": extrair_vagas_adzuna,
    "donotreply@jobalert.indeed.com": extrair_vagas_indeed,
}


def vagas_do_email(bruto):
    """Devolve as vagas do e-mail, ou None se ainda nao ha leitor para ele."""
    mensagem = email.message_from_bytes(bruto, policy=policy.default)
    leitor = LEITORES_GMAIL.get(parseaddr(mensagem["from"] or "")[1].lower())
    corpo = mensagem.get_body(preferencelist=("html",))
    if not leitor or not corpo:
        return None
    return leitor(corpo.get_content())


def coletar_gmail(perfil):
    """Le as vagas dos alertas na caixa de entrada, sempre em modo somente leitura."""
    usuario, senha = ler_credenciais()
    config = perfil["alertas_gmail"]
    remetentes = " OR ".join(config["remetentes"])
    consulta = f'"from:({remetentes}) newer_than:{config["dias"]}d"'

    try:
        conexao = imaplib.IMAP4_SSL(IMAP_SERVIDOR, IMAP_PORTA, timeout=IMAP_TIMEOUT)
        try:
            conexao.login(usuario, senha)
            conexao.select("INBOX", readonly=True)
            _, resultado = conexao.search(None, "X-GM-RAW", consulta)
            numeros = resultado[0].split()
            # O PEEK nao marca o e-mail como lido.
            emails = [
                conexao.fetch(numero.decode(), "(BODY.PEEK[])")[1][0][1]
                for numero in numeros
            ]
        finally:
            conexao.logout()
    except imaplib.IMAP4.error as erro:
        raise ConnectionError(f"erro no IMAP ({type(erro).__name__})") from None

    vagas = []
    sem_leitor = 0
    for bruto in emails:
        lidas = vagas_do_email(bruto)
        if lidas is None:
            sem_leitor += 1
        else:
            vagas.extend(lidas)

    print(f"Alertas no Gmail: {len(emails)} e-mails nos ultimos {config['dias']} dias "
          f"({sem_leitor} sem leitor de vagas).")
    return vagas


def coletar_vagas(perfil):
    """Roda cada fonte. Se uma falhar, registra e segue com as outras."""
    fontes = {"Adzuna": coletar_adzuna, "Gmail": coletar_gmail}
    vagas = {}
    falhas = []
    avisos = []
    for nome, coletar in fontes.items():
        try:
            coletadas = coletar(perfil)
        except (ValueError, OSError) as erro:
            print(f"FALHA na fonte {nome}: {erro}")
            falhas.append(nome)
            if isinstance(erro, FonteRecusouAcesso):
                avisos.append(AVISO_ADZUNA)
            continue
        print(f"Fonte {nome}: {len(coletadas)} vagas coletadas.")
        for vaga in coletadas:
            # A mesma vaga so entra uma vez: fica a da fonte com mais dados, que vem antes.
            vagas.setdefault(vaga["id"], vaga)

    return list(vagas.values()), falhas, avisos


# --- Filtros ---


def detectar_modelo(texto):
    if tem_algum(texto, TERMOS_HIBRIDO):
        return "hibrido"
    if tem_algum(texto, TERMOS_REMOTO):
        return "remoto"
    if tem_algum(texto, TERMOS_PRESENCIAL):
        return "presencial"
    return "nao informado"


def niveis_do_titulo(titulo, niveis):
    """Devolve quais niveis da lista aparecem no titulo da vaga."""
    return [
        nivel
        for nivel in niveis
        if tem_algum(titulo, APELIDOS_SENIORIDADE.get(nivel, [nivel]))
    ]


def motivo_descarte(vaga, perfil):
    """Devolve o motivo para descartar a vaga, ou None se ela continua."""
    titulo = normalizar(vaga["titulo"])
    texto = normalizar(f"{vaga['titulo']} {vaga['descricao']}")
    preferencias = perfil["preferencias"]

    if tem_algum(texto, perfil["excluir"]):
        return "palavra excluida"

    if tem_algum(titulo, perfil["excluir_no_titulo"]):
        return "palavra excluida no titulo"

    # Olha so o titulo: a descricao pode citar "reporta ao gerente".
    if niveis_do_titulo(titulo, perfil["senioridade"]["evitar"]):
        return "senioridade"

    remoto_vale = preferencias["remoto_qualquer_cidade"]
    if not (detectar_modelo(texto) == "remoto" and remoto_vale):
        local = normalizar(" ".join([vaga["local"], *vaga["regioes"]]))
        if not tem_algum(local, preferencias["cidades_presencial"]):
            return "fora da cidade"

    # Vaga sem salario informado nao e descartada.
    if vaga["salario"] is not None and vaga["salario"] < preferencias["salario_minimo"]:
        return "salario abaixo do minimo"

    return None


# --- Nota ---


def palavras_do_cargo(texto, sinonimos):
    palavras = set(normalizar(texto).split()) - PALAVRAS_VAZIAS
    return {sinonimos.get(palavra, palavra) for palavra in palavras}


def encontrar_cargo(titulo, perfil):
    """Devolve (prioridade, cargo) do melhor cargo do perfil, ou (None, None)."""
    sinonimos = perfil.get("sinonimos_cargo") or {}
    palavras_titulo = palavras_do_cargo(titulo, sinonimos)
    for prioridade in PONTOS_CARGO:
        for cargo in perfil["cargos"][f"prioridade_{prioridade}"]:
            if palavras_do_cargo(cargo, sinonimos) <= palavras_titulo:
                return prioridade, cargo
    return None, None


def alertas_da_vaga(vaga, perfil):
    """Lista o que voce precisa conferir antes de se candidatar."""
    texto = normalizar(f"{vaga['titulo']} {vaga['descricao']}")
    alertas = []
    if tem_algum(texto, perfil["termos_alerta_pj"]):
        alertas.append(ALERTA_PJ)
    if vaga.get("sem_descricao"):
        alertas.append(ALERTA_SEM_DESCRICAO)
    return alertas


def calcular_nota(vaga, perfil):
    """Devolve (nota de 0 a 100, lista de motivos da nota)."""
    titulo = normalizar(vaga["titulo"])
    texto = normalizar(f"{vaga['titulo']} {vaga['descricao']}")
    nota = 0
    motivos = []

    prioridade, cargo = encontrar_cargo(vaga["titulo"], perfil)
    if prioridade:
        nota += PONTOS_CARGO[prioridade]
        motivos.append(f"cargo de prioridade {prioridade} ({cargo})")

    competencias = perfil["competencias"]
    termos = separar_alternativas(competencias["tecnicas"] + competencias["dominio"])
    encontradas = [termo for termo in termos if tem_termo(texto, termo)]
    if encontradas:
        nota += min(len(encontradas) * PONTOS_COMPETENCIA, MAX_PONTOS_COMPETENCIAS)
        motivos.append("competências: " + ", ".join(encontradas))

    modelo = detectar_modelo(texto)
    if modelo in perfil["preferencias"]["modelo_preferido"]:
        nota += PONTOS_MODELO_PREFERIDO
        motivos.append(f"modelo {ROTULOS_MODELO[modelo].lower()}")

    niveis = niveis_do_titulo(titulo, perfil["senioridade"]["aceitar"])
    if niveis:
        nota += PONTOS_SENIORIDADE
        motivos.append("nível " + "/".join(ROTULOS_NIVEL.get(n, n) for n in niveis))

    lacunas = [t for t in separar_alternativas(perfil["lacunas"]) if tem_termo(texto, t)]
    if lacunas:
        nota -= min(len(lacunas) * PONTOS_LACUNA, MAX_DESCONTO_LACUNAS)
        motivos.append("lacunas: " + ", ".join(lacunas))

    # Sem descricao nao ha competencias para pontuar: o cargo principal garante o alerta.
    nota_minima = perfil["preferencias"]["alerta_email_nota_minima"]
    if vaga.get("sem_descricao") and prioridade == 1 and nota < nota_minima:
        nota = nota_minima
        motivos.append("nota mínima pelo cargo, pois o alerta não traz descrição")

    if ALERTA_PJ in alertas_da_vaga(vaga, perfil):
        nota -= PONTOS_ALERTA_PJ
        motivos.append("possível contrato PJ")

    return max(0, min(nota, 100)), motivos


def mesma_vaga(vaga, outra):
    """Mesma empresa com titulo igual ou muito parecido."""
    empresa = normalizar(vaga["empresa"])
    if empresa != normalizar(outra["empresa"]) or empresa == "nao informada":
        return False
    parecido = SequenceMatcher(
        None, normalizar(vaga["titulo"]), normalizar(outra["titulo"])
    )
    return parecido.ratio() >= SIMILARIDADE_TITULO


def remover_repetidas(vagas):
    """Guarda a primeira de cada grupo (a lista ja vem ordenada pela nota)."""
    unicas = []
    for vaga in vagas:
        if not any(mesma_vaga(vaga, outra) for outra in unicas):
            unicas.append(vaga)
    return unicas


def avaliar_vagas(vagas, perfil):
    """Filtra e da nota. Devolve (aprovadas, talvez interesse, contagem dos descartes)."""
    nota_minima = perfil["preferencias"]["alerta_email_nota_minima"]
    nota_talvez = perfil["preferencias"]["alerta_email_nota_talvez"]
    aprovadas = []
    descartes = Counter()
    for vaga in vagas:
        motivo = motivo_descarte(vaga, perfil)
        if motivo:
            descartes[motivo] += 1
            continue

        nota, motivos = calcular_nota(vaga, perfil)
        if nota < nota_talvez:
            descartes["nota abaixo do minimo"] += 1
            continue

        modelo = detectar_modelo(normalizar(f"{vaga['titulo']} {vaga['descricao']}"))
        alertas = alertas_da_vaga(vaga, perfil)
        aprovadas.append(
            {**vaga, "nota": nota, "motivos": motivos, "modelo": modelo, "alertas": alertas}
        )

    aprovadas.sort(key=lambda vaga: vaga["nota"], reverse=True)
    unicas = remover_repetidas(aprovadas)
    if len(unicas) < len(aprovadas):
        descartes["repetida (mesma empresa)"] += len(aprovadas) - len(unicas)

    principais = [vaga for vaga in unicas if vaga["nota"] >= nota_minima]
    talvez = [vaga for vaga in unicas if vaga["nota"] < nota_minima]
    return principais, talvez, descartes


def formatar_salario(salario):
    if salario is None:
        return "Não informado"
    return f"R$ {salario:,.0f}".replace(",", ".")


def formatar_vaga(vaga):
    alertas = "".join(f"{alerta}\n" for alerta in vaga["alertas"])
    return (
        f"{vaga['titulo']} - {vaga['empresa']}\n"
        f"{alertas}"
        f"Local/modelo: {vaga['local']} - {ROTULOS_MODELO.get(vaga['modelo'], vaga['modelo'])}\n"
        f"Salário: {formatar_salario(vaga['salario'])}\n"
        f"Nota: {vaga['nota']}/100\n"
        f"Por que combina: {'; '.join(vaga['motivos'])}\n"
        f"Link: {vaga['link']}\n"
    )


def formatar_vaga_compacta(vaga):
    return f"{vaga['titulo']} - {vaga['empresa']}\n{vaga['local']}\n{vaga['link']}\n"


def montar_email(aprovadas, talvez, avisos):
    """Monta (assunto, corpo) do alerta: avisos, lista principal e "Talvez interesse"."""
    assunto = (
        f"[Alerta de Vagas] {len(aprovadas)} vagas aderentes, {len(talvez)} talvez"
    )
    blocos = [*avisos, *(formatar_vaga(vaga) for vaga in aprovadas)]
    if talvez:
        blocos.append("===== Talvez interesse =====\n")
        blocos.extend(formatar_vaga_compacta(vaga) for vaga in talvez)
    return assunto, "\n".join(blocos)


# --- E-mail em HTML (o texto simples acima vai junto, para quem nao abre HTML) ---


def cor_da_nota(nota):
    if nota >= 80:
        return "#1e7d32"  # verde
    if nota >= 60:
        return "#1565c0"  # azul
    return "#6d6d6d"  # cinza


def vaga_em_html(vaga):
    link = escape(vaga["link"])
    modelo = ROTULOS_MODELO.get(vaga["modelo"], vaga["modelo"])
    alertas = "".join(
        f'<p style="margin:8px 0 0;font-size:13px;color:#9a6700;">{escape(alerta)}</p>'
        for alerta in vaga["alertas"]
    )
    return f"""
<tr><td style="padding:18px 0;border-bottom:1px solid #e3e3e3;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
    <td style="vertical-align:top;">
      <a href="{link}" style="font-size:17px;font-weight:bold;color:#1a1a1a;text-decoration:none;">{escape(vaga["titulo"])}</a>
      <div style="margin-top:4px;font-size:14px;color:#555;">{escape(vaga["empresa"])} · {escape(vaga["local"])} · {escape(modelo)}</div>
    </td>
    <td style="vertical-align:top;text-align:right;white-space:nowrap;padding-left:12px;">
      <span style="display:inline-block;padding:6px 12px;border-radius:6px;background:{cor_da_nota(vaga["nota"])};color:#fff;font-size:16px;font-weight:bold;">{vaga["nota"]}/100</span>
    </td>
  </tr></table>
  {alertas}
  <p style="margin:10px 0 0;font-size:14px;color:#333;"><b>Salário:</b> {escape(formatar_salario(vaga["salario"]))}</p>
  <p style="margin:4px 0 0;font-size:14px;color:#333;"><b>Por que combina:</b> {escape("; ".join(vaga["motivos"]))}</p>
  <p style="margin:10px 0 0;font-size:14px;"><a href="{link}" style="color:#1565c0;">Ver vaga →</a></p>
</td></tr>"""


def vaga_compacta_em_html(vaga):
    return f"""
<tr><td style="padding:10px 0;border-bottom:1px solid #eeeeee;font-size:14px;">
  <a href="{escape(vaga["link"])}" style="font-weight:bold;color:#1a1a1a;text-decoration:none;">{escape(vaga["titulo"])}</a>
  <span style="color:#555;"> · {escape(vaga["empresa"])} · {escape(vaga["local"])}</span>
</td></tr>"""


def montar_email_html(aprovadas, talvez, avisos, rodape="Enviado automaticamente pelo Alerta de Vagas."):
    """Mesmo conteudo do texto simples, com titulo, nota em destaque e uma linha entre vagas."""
    caixa_avisos = "".join(
        f'<p style="margin:0 0 16px;padding:10px 12px;border-radius:6px;background:#fff4e5;'
        f'color:#7a4b00;font-size:14px;">{escape(aviso)}</p>'
        for aviso in avisos
    )
    principais = "".join(vaga_em_html(vaga) for vaga in aprovadas)
    secao_talvez = ""
    if talvez:
        secao_talvez = (
            '<h2 style="margin:28px 0 4px;font-size:16px;color:#1a1a1a;">Talvez interesse</h2>'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation">'
            + "".join(vaga_compacta_em_html(vaga) for vaga in talvez)
            + "</table>"
        )
    return f"""<!doctype html>
<html lang="pt-BR"><body style="margin:0;padding:24px 12px;background:#f2f2f2;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr><td align="center">
<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="max-width:640px;background:#ffffff;border-radius:8px;">
<tr><td style="padding:24px 28px 8px;">
  <h1 style="margin:0;font-size:22px;color:#1a1a1a;">Alerta de Vagas</h1>
  <p style="margin:6px 0 20px;font-size:14px;color:#555;">{len(aprovadas)} vagas aderentes · {len(talvez)} talvez</p>
  {caixa_avisos}
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation">{principais}</table>
  {secao_talvez}
  <p style="margin:24px 0 16px;font-size:12px;color:#888;">{escape(rodape)}</p>
</td></tr>
</table>
</td></tr></table>
</body></html>"""


# --- E-mail ---


def ler_credenciais():
    """Le usuario e senha do .env (no PC) ou dos Secrets (no GitHub Actions)."""
    load_dotenv(PASTA_PROJETO / ".env")
    usuario = os.getenv("GMAIL_USER", "").strip()
    # O Google mostra a senha de app em grupos separados por espaco.
    senha = "".join(os.getenv("GMAIL_APP_PASSWORD", "").split())

    faltando = []
    if not usuario:
        faltando.append("GMAIL_USER")
    if not senha:
        faltando.append("GMAIL_APP_PASSWORD")
    if faltando:
        raise ValueError(f"faltando preencher: {', '.join(faltando)}")

    return usuario, senha


def conectar_gmail(usuario, senha):
    """Abre a conexao criptografada com o Gmail e faz o login."""
    contexto = ssl.create_default_context()
    conexao = smtplib.SMTP_SSL(
        SMTP_SERVIDOR, SMTP_PORTA, context=contexto, timeout=SMTP_TIMEOUT
    )
    try:
        conexao.login(usuario, senha)
    except smtplib.SMTPException:
        conexao.close()
        raise
    return conexao


def enviar_email(usuario, senha, assunto, corpo, html=None):
    """Envia o e-mail em texto e, se houver, em HTML. Remetente e destinatario sao a mesma conta."""
    mensagem = EmailMessage()
    mensagem["Subject"] = assunto
    mensagem["From"] = usuario
    mensagem["To"] = usuario
    mensagem.set_content(corpo)
    if html:
        mensagem.add_alternative(html, subtype="html")

    with conectar_gmail(usuario, senha) as conexao:
        conexao.send_message(mensagem)


# --- Vagas vistas ---


def ler_vistas():
    if not ARQUIVO_VISTAS.exists():
        return []
    with open(ARQUIVO_VISTAS, encoding="utf-8", newline="") as arquivo:
        return list(csv.DictReader(arquivo))


def enviada_ha_pouco(vaga, vistas):
    """Mesma empresa e titulo parecido ja enviados, mesmo com outro codigo de vaga."""
    inicio = datetime.now(timezone.utc) - timedelta(days=DIAS_SEM_REPETIR)
    return any(
        linha["enviada"] == "sim"
        and datetime.fromisoformat(linha["visto_em_utc"]) >= inicio
        and mesma_vaga(vaga, linha)
        for linha in vistas
    )


def separar_novas(vagas, vistas):
    """Devolve (vagas novas, quantidade das que ja foram vistas)."""
    codigos = {linha["id"] for linha in vistas}
    novas = [
        vaga
        for vaga in vagas
        if vaga["id"] not in codigos and not enviada_ha_pouco(vaga, vistas)
    ]
    return novas, len(vagas) - len(novas)


def registrar_vistas(novas, enviadas):
    """Acrescenta as vagas ao historico. Nunca reescreve nem apaga o que ja existe."""
    if not novas:
        return

    codigos_enviados = {vaga["id"] for vaga in enviadas}
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    novo = not ARQUIVO_VISTAS.exists()
    with open(ARQUIVO_VISTAS, "a", encoding="utf-8", newline="") as arquivo:
        escritor = csv.DictWriter(
            arquivo, CAMPOS_VISTAS, extrasaction="ignore", lineterminator="\n"
        )
        if novo:
            escritor.writeheader()
        for vaga in novas:
            enviada = "sim" if vaga["id"] in codigos_enviados else "nao"
            # O link do Indeed traz um codigo de rastreamento: nao vai para o repositorio.
            link = "" if vaga["fonte"] == "Indeed" else vaga["link"]
            escritor.writerow(
                {**vaga, "link": link, "visto_em_utc": agora, "enviada": enviada}
            )


# --- Aviso de fonte indisponivel ---


def hoje_local():
    return datetime.now(FUSO_LOCAL).date().isoformat()


def aviso_enviado_hoje():
    if not ARQUIVO_ULTIMO_AVISO.exists():
        return False
    return ARQUIVO_ULTIMO_AVISO.read_text(encoding="utf-8").strip() == hoje_local()


def registrar_aviso():
    ARQUIVO_ULTIMO_AVISO.write_text(f"{hoje_local()}\n", encoding="utf-8")


def montar_email_aviso(avisos):
    assunto = f"[Alerta de Vagas] Aviso: {avisos[0]}"
    corpo = "\n".join(avisos) + "\n\nNenhuma vaga nova nesta execução. O projeto segue com as outras fontes.\n"
    return assunto, corpo


# --- Modos de execucao ---


def analisar_vagas(perfil):
    """Coleta, tira as ja vistas, avalia e registra o resumo no log."""
    vagas, falhas, avisos = coletar_vagas(perfil)
    novas, ja_vistas = separar_novas(vagas, ler_vistas())
    aprovadas, talvez, descartes = avaliar_vagas(novas, perfil)

    print(f"\nVagas ja vistas: {ja_vistas}")
    print(f"Descartadas: {sum(descartes.values())}")
    for motivo, quantidade in descartes.most_common():
        print(f"  {quantidade:4d}  {motivo}")
    print(f"\nVagas na lista principal do e-mail: {len(aprovadas)}")
    print(f"Vagas em Talvez interesse: {len(talvez)}")

    return novas, aprovadas, talvez, falhas, avisos


def executar():
    """Busca as vagas, envia o alerta e so entao grava as vagas como vistas."""
    usuario, senha = ler_credenciais()
    perfil = ler_perfil()
    novas, aprovadas, talvez, falhas, avisos = analisar_vagas(perfil)

    # Se o envio falhar, nada e gravado e as vagas voltam na proxima execucao.
    if aprovadas or talvez:
        assunto, corpo = montar_email(aprovadas, talvez, avisos)
        html = montar_email_html(aprovadas, talvez, avisos)
        enviar_email(usuario, senha, assunto, corpo, html)
        print(f"E-mail enviado: {assunto}")
        if avisos:
            registrar_aviso()
    elif avisos and not aviso_enviado_hoje():
        assunto, corpo = montar_email_aviso(avisos)
        enviar_email(usuario, senha, assunto, corpo)
        print(f"E-mail enviado: {assunto}")
        registrar_aviso()
    else:
        print("Nenhuma vaga para enviar.")

    registrar_vistas(novas, aprovadas + talvez)
    print(f"Vagas gravadas no historico: {len(novas)}.")
    if falhas:
        print(f"Execucao com problemas nas fontes: {', '.join(falhas)}.")
    return 0


def validar():
    """Confere tudo e mostra as vagas na tela, sem enviar e-mail nem gravar."""
    usuario, senha = ler_credenciais()
    print(f"Credenciais encontradas para {usuario}.")

    with conectar_gmail(usuario, senha):
        print("Login no Gmail: OK.")

    perfil = ler_perfil()
    print("Perfil lido: OK.")

    _, aprovadas, talvez, falhas, avisos = analisar_vagas(perfil)
    assunto, corpo = montar_email(aprovadas, talvez, avisos)
    print(f"\nPrevia do e-mail ({assunto}):\n\n{corpo}")

    if falhas:
        print(f"Validacao com problemas nas fontes: {', '.join(falhas)}.")
        return 1

    print("Validacao concluida. Nenhum e-mail foi enviado.")
    print("Unico arquivo gravado: dados/adzuna_uso.csv (contagem de chamadas).")
    return 0


def teste_email():
    """Manda 1 e-mail de exemplo, no formato do alerta de vagas."""
    usuario, senha = ler_credenciais()
    assunto = "[Alerta de Vagas] E-mail de teste"
    corpo = (
        "Este e-mail de teste mostra como os alertas vão chegar.\n"
        "A vaga abaixo é apenas um exemplo.\n\n"
        "Analista de Marketing Digital Pleno - Empresa Exemplo\n"
        "Local/modelo: Curitiba/PR - Híbrido\n"
        "Salário: R$ 6.500\n"
        "Nota: 85/100\n"
        "Por que combina: cargo de prioridade 1, Google Ads, Canva, copywriting\n"
        "Link: https://example.com/vaga-exemplo\n"
    )
    enviar_email(usuario, senha, assunto, corpo)
    print(f"E-mail de teste enviado para {usuario}.")


def main():
    # Evita erro ao mostrar caracteres incomuns de titulos de vagas no Windows.
    sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Alerta de vagas por e-mail.")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument(
        "--validar", action="store_true", help="confere tudo, nao envia e-mail"
    )
    modo.add_argument(
        "--teste-email", action="store_true", help="manda 1 e-mail de exemplo"
    )
    args = parser.parse_args()

    try:
        if args.teste_email:
            teste_email()
        elif args.validar:
            return validar()
        else:
            return executar()
    except ValueError as erro:
        print(f"ERRO de configuracao: {erro}. Confira o arquivo .env.")
        return 1
    except smtplib.SMTPAuthenticationError:
        print("ERRO: o Gmail recusou o login. Confira o usuario e a senha de app.")
        return 1
    except (smtplib.SMTPException, OSError) as erro:
        print(f"ERRO ao falar com o Gmail: {erro}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
