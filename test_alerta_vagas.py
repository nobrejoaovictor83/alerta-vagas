"""Testes dos filtros e da nota, com vagas inventadas (sem internet).

Como rodar:
    python -m unittest
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import alerta_vagas as av

PERFIL = av.ler_perfil()


def vaga(titulo="Analista de Marketing Digital Pleno", descricao="", local="Curitiba, Parana",
         salario=None, empresa="Empresa Exemplo"):
    return {
        "id": "teste-1",
        "fonte": "Teste",
        "titulo": titulo,
        "empresa": empresa,
        "local": local,
        "regioes": ["Brasil", "Parana", "Curitiba"] if "Curitiba" in local else ["Brasil"],
        "descricao": descricao,
        "salario": salario,
        "link": "https://example.com/vaga",
    }


class TestTexto(unittest.TestCase):
    def test_normalizar_tira_acento_e_pontuacao(self):
        self.assertEqual(av.normalizar("Comunicação, P&D!"), "comunicacao p&d")

    def test_termo_so_casa_com_palavra_inteira(self):
        self.assertTrue(av.tem_termo("empresa contrata mei hoje", "MEI"))
        self.assertFalse(av.tem_termo("meio periodo", "MEI"))


class TestDescarte(unittest.TestCase):
    def motivo(self, **campos):
        return av.motivo_descarte(vaga(**campos), PERFIL)

    def test_vaga_boa_na_cidade_passa(self):
        self.assertIsNone(self.motivo(descricao="Trabalho presencial"))

    def test_palavra_excluida(self):
        self.assertEqual(self.motivo(descricao="Contratacao PJ"), "palavra excluida")
        self.assertEqual(self.motivo(descricao="Estágio em marketing"), "palavra excluida")

    def test_pj_como_contrato_descarta(self):
        for descricao in ("Regime PJ", "Vaga PJ", "Modelo PJ", "Via PJ",
                          "Contratação: PJ", "Contratação como pessoa jurídica"):
            self.assertEqual(self.motivo(descricao=descricao), "palavra excluida", descricao)

    def test_pj_sozinho_nao_descarta(self):
        for descricao in ("Campanhas para clientes PJ", "Clientes PJ", "Nossa área de PJ",
                          "Atendimento a pessoa jurídica"):
            self.assertIsNone(self.motivo(descricao=descricao), descricao)

    def test_mei_descarta(self):
        self.assertEqual(self.motivo(descricao="Aceita MEI"), "palavra excluida")

    def test_associado_no_titulo_descarta(self):
        resultado = self.motivo(titulo="Vaga para Associado: Analista de Marketing Digital")
        self.assertEqual(resultado, "palavra excluida no titulo")

    def test_associado_na_descricao_nao_descarta(self):
        self.assertIsNone(self.motivo(descricao="Atendimento ao associado do clube"))

    def test_associados_no_nome_da_empresa_nao_descarta(self):
        self.assertIsNone(av.motivo_descarte(vaga(empresa="Silva Associados"), PERFIL))

    def test_senioridade_no_titulo(self):
        self.assertEqual(self.motivo(titulo="Analista de Marketing Digital Júnior"), "senioridade")
        self.assertEqual(self.motivo(titulo="Analista de Marketing Digital Jr"), "senioridade")

    def test_gerente_na_descricao_nao_descarta(self):
        self.assertIsNone(self.motivo(descricao="Reporta ao gerente de marketing"))

    def test_presencial_fora_da_cidade(self):
        resultado = self.motivo(local="São Paulo", descricao="Presencial")
        self.assertEqual(resultado, "fora da cidade")

    def test_hibrido_fora_da_cidade(self):
        resultado = self.motivo(local="São Paulo", descricao="Modelo híbrido")
        self.assertEqual(resultado, "fora da cidade")

    def test_sem_modelo_fora_da_cidade(self):
        self.assertEqual(self.motivo(local="São Paulo"), "fora da cidade")

    def test_remoto_vale_de_qualquer_cidade(self):
        self.assertIsNone(self.motivo(local="São Paulo", descricao="100% remoto"))
        self.assertIsNone(self.motivo(local="São Paulo", descricao="Home office"))

    def test_salario_abaixo_do_minimo(self):
        self.assertEqual(self.motivo(salario=5000), "salario abaixo do minimo")

    def test_salario_no_minimo_passa(self):
        self.assertIsNone(self.motivo(salario=6000))

    def test_sem_salario_nao_descarta(self):
        self.assertIsNone(self.motivo(salario=None))


class TestNota(unittest.TestCase):
    def nota(self, **campos):
        return av.calcular_nota(vaga(**campos), PERFIL)[0]

    def test_prioridade_do_cargo(self):
        p1 = self.nota(titulo="Analista de Marketing Digital")
        p2 = self.nota(titulo="Analista de Growth")
        p3 = self.nota(titulo="Analista de Branding")
        self.assertEqual((p1, p2, p3), (50, 40, 30))

    def test_cargo_aceita_variacoes(self):
        for titulo in ("Analista de Redes Sociais", "Analista de Mídia Social",
                       "Analista Marketing Digital", "ANALISTA DE CRM"):
            self.assertEqual(av.encontrar_cargo(titulo, PERFIL)[0], 1, titulo)

    def test_cargo_fora_do_perfil(self):
        self.assertEqual(av.encontrar_cargo("Analista de RH", PERFIL), (None, None))

    def test_competencias_somam_ate_o_teto(self):
        self.assertEqual(self.nota(titulo="Analista de Marketing Digital",
                                   descricao="Google Ads e Canva"), 60)
        muitas = "Google Analytics, Google Ads, Meta Ads, RD Station, HubSpot, Canva, WordPress e copywriting"
        self.assertEqual(self.nota(titulo="Analista de Marketing Digital", descricao=muitas), 75)

    def test_modelo_e_nivel_somam(self):
        self.assertEqual(self.nota(titulo="Analista de Marketing Digital Pleno",
                                   descricao="Modelo híbrido"), 75)

    def test_lacuna_desconta(self):
        self.assertEqual(self.nota(titulo="Analista de Marketing Digital",
                                   descricao="Alemão fluente"), 40)

    def test_pj_sozinho_desconta_e_gera_alerta(self):
        nova = vaga(titulo="Analista de Marketing Digital", descricao="Clientes PJ, Google Ads e Canva")
        self.assertEqual(av.calcular_nota(nova, PERFIL)[0], 50)
        self.assertEqual(av.alertas_da_vaga(nova, PERFIL), [av.ALERTA_PJ])

    def test_sem_pj_nao_tem_alerta(self):
        self.assertEqual(av.alertas_da_vaga(vaga(), PERFIL), [])

    def test_nota_fica_entre_0_e_100(self):
        self.assertEqual(self.nota(titulo="Auxiliar", descricao="Alemão fluente"), 0)
        tudo = "Google Analytics, Google Ads, Meta Ads, RD Station, HubSpot e Canva, remoto"
        self.assertEqual(self.nota(titulo="Analista de Marketing Digital Pleno", descricao=tudo), 100)


class TestAvaliar(unittest.TestCase):
    def test_ordena_por_nota_e_conta_descartes(self):
        vagas = [
            vaga(titulo="Analista de Marketing Digital", descricao="Google Ads e Canva", empresa="A"),
            vaga(titulo="Analista de Marketing Digital Pleno", descricao="Híbrido, Google Ads e Canva",
                 empresa="B"),
            vaga(titulo="Analista de Marketing Digital Júnior"),
            vaga(titulo="Analista de RH"),
        ]
        aprovadas, talvez, descartes = av.avaliar_vagas(vagas, PERFIL)
        self.assertEqual([v["nota"] for v in aprovadas], [85, 60])
        self.assertEqual(talvez, [])
        self.assertEqual(descartes["senioridade"], 1)
        self.assertEqual(descartes["nota abaixo do minimo"], 1)

    def test_separa_lista_principal_e_talvez(self):
        vagas = [
            vaga(titulo="Analista de Marketing Digital", descricao="Google Ads e Canva", empresa="A"),
            vaga(titulo="Analista de Marketing Digital", descricao="Google Ads", empresa="B"),
            vaga(titulo="Analista de Growth", empresa="C"),
            vaga(titulo="Analista de Branding", empresa="D"),
        ]
        aprovadas, talvez, descartes = av.avaliar_vagas(vagas, PERFIL)
        self.assertEqual([v["nota"] for v in aprovadas], [60])
        self.assertEqual([v["nota"] for v in talvez], [55, 40])
        self.assertEqual(descartes["nota abaixo do minimo"], 1)

    def test_repetida_nao_aparece_nas_duas_listas(self):
        vagas = [
            vaga(titulo="Analista de Marketing Digital", descricao="Google Ads e Canva"),
            vaga(titulo="ANALISTA DE MARKETING DIGITAL", descricao="Google Ads"),
        ]
        aprovadas, talvez, _ = av.avaliar_vagas(vagas, PERFIL)
        self.assertEqual((len(aprovadas), len(talvez)), (1, 0))

    def test_junta_mesma_empresa_e_titulo_parecido(self):
        vagas = [
            vaga(titulo="Analista de Marketing Digital - PR", descricao="Google Ads e Canva"),
            vaga(titulo="ANALISTA DE MARKETING DIGITAL", descricao="Híbrido, Google Ads e Canva"),
        ]
        aprovadas, _, descartes = av.avaliar_vagas(vagas, PERFIL)
        self.assertEqual([v["nota"] for v in aprovadas], [70])
        self.assertEqual(descartes["repetida (mesma empresa)"], 1)

    def test_nao_junta_empresas_diferentes(self):
        self.assertFalse(av.mesma_vaga(vaga(empresa="A"), vaga(empresa="B")))

    def test_nao_junta_empresa_nao_informada(self):
        anonima = vaga(empresa="Nao informada")
        self.assertFalse(av.mesma_vaga(anonima, anonima))

    def test_nao_junta_niveis_diferentes(self):
        self.assertFalse(av.mesma_vaga(vaga(titulo="Analista de CRM Jr"),
                                       vaga(titulo="Analista de CRM Pl")))


class TestVagasVistas(unittest.TestCase):
    def setUp(self):
        # Os testes nunca tocam no historico real.
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.arquivo = Path(pasta.name) / "vistas.csv"
        self.arquivo_aviso = Path(pasta.name) / "aviso.txt"
        for trocar in (
            patch.object(av, "ARQUIVO_VISTAS", self.arquivo),
            patch.object(av, "ARQUIVO_ULTIMO_AVISO", self.arquivo_aviso),
            patch.object(av, "ler_credenciais", return_value=("conta", "senha")),
            patch("builtins.print"),
        ):
            trocar.start()
            self.addCleanup(trocar.stop)
        self.envio = patch.object(av, "enviar_email").start()
        self.addCleanup(patch.stopall)

    def boa(self, codigo="a1", titulo="Analista de Marketing Digital Pleno", empresa="Empresa A"):
        return {**vaga(titulo=titulo, descricao="Google Ads e Canva", empresa=empresa), "id": codigo}

    def executar(self, *vagas, avisos=()):
        with patch.object(av, "coletar_vagas", return_value=(list(vagas), [], list(avisos))):
            return av.executar()

    def test_link_do_indeed_vai_no_email_mas_nao_no_historico(self):
        nova = {**self.boa("indeed-1"), "fonte": "Indeed", "link": "https://exemplo/rastreio"}
        self.executar(nova)
        self.assertIn("https://exemplo/rastreio", self.envio.call_args[0][3])
        self.assertNotIn("rastreio", self.arquivo.read_text(encoding="utf-8"))

    def test_aviso_sozinho_sai_uma_vez_por_dia(self):
        self.executar(avisos=[av.AVISO_ADZUNA])
        self.executar(avisos=[av.AVISO_ADZUNA])
        self.envio.assert_called_once()
        assunto, corpo = self.envio.call_args[0][2:4]
        self.assertIn("Adzuna indisponível", assunto)
        self.assertIn("Adzuna indisponível", corpo)

    def test_aviso_volta_no_dia_seguinte(self):
        self.arquivo_aviso.write_text("2000-01-01\n", encoding="utf-8")
        self.executar(avisos=[av.AVISO_ADZUNA])
        self.envio.assert_called_once()

    def test_aviso_dentro_do_alerta_vale_pelo_dia(self):
        self.executar(self.boa(), avisos=[av.AVISO_ADZUNA])
        self.executar(avisos=[av.AVISO_ADZUNA])
        self.envio.assert_called_once()
        self.assertIn("Adzuna indisponível", self.envio.call_args[0][3])

    def test_vagas_novas_saem_mesmo_depois_do_aviso(self):
        self.executar(avisos=[av.AVISO_ADZUNA])
        self.executar(self.boa(), avisos=[av.AVISO_ADZUNA])
        self.assertEqual(self.envio.call_count, 2)

    def test_sem_aviso_nao_grava_a_data(self):
        self.executar(self.boa())
        self.assertFalse(self.arquivo_aviso.exists())

    def test_aviso_nao_gravado_se_o_envio_falhar(self):
        self.envio.side_effect = av.smtplib.SMTPException("falhou")
        with self.assertRaises(av.smtplib.SMTPException):
            self.executar(avisos=[av.AVISO_ADZUNA])
        self.assertFalse(self.arquivo_aviso.exists())

    def test_envia_e_grava_na_primeira_vez(self):
        self.executar(self.boa())
        self.envio.assert_called_once()
        linhas = av.ler_vistas()
        self.assertEqual([l["id"] for l in linhas], ["a1"])
        self.assertEqual(linhas[0]["enviada"], "sim")

    def test_nao_repete_a_mesma_vaga(self):
        self.executar(self.boa())
        self.executar(self.boa())
        self.envio.assert_called_once()
        self.assertEqual(len(av.ler_vistas()), 1)

    def test_historico_so_cresce(self):
        self.executar(self.boa("a1"))
        antes = self.arquivo.read_text(encoding="utf-8")
        self.executar(self.boa("b2", titulo="Analista de Mídias Sociais", empresa="Empresa B"))
        depois = self.arquivo.read_text(encoding="utf-8")
        self.assertTrue(depois.startswith(antes))
        self.assertEqual(len(av.ler_vistas()), 2)

    def test_se_o_envio_falhar_nada_e_gravado(self):
        self.envio.side_effect = av.smtplib.SMTPException("falhou")
        with self.assertRaises(av.smtplib.SMTPException):
            self.executar(self.boa())
        self.assertFalse(self.arquivo.exists())

    def test_descartada_e_gravada_como_nao_enviada(self):
        self.executar(vaga(titulo="Analista de RH"))
        self.envio.assert_not_called()
        self.assertEqual(av.ler_vistas()[0]["enviada"], "nao")

    def test_mesma_vaga_com_outro_codigo_nao_repete(self):
        self.executar(self.boa("adzuna-1"))
        self.executar(self.boa("linkedin-1"))
        self.envio.assert_called_once()

    def test_vaga_parecida_depois_do_prazo_volta(self):
        antiga = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        vistas = [{"id": "x", "titulo": "Analista de Marketing Digital Pleno", "empresa": "Empresa A",
                   "enviada": "sim", "visto_em_utc": antiga}]
        self.assertEqual(av.separar_novas([self.boa()], vistas)[1], 0)

    def test_parecida_nao_enviada_nao_bloqueia(self):
        agora = datetime.now(timezone.utc).isoformat()
        vistas = [{"id": "x", "titulo": "Analista de Marketing Digital Pleno", "empresa": "Empresa A",
                   "enviada": "nao", "visto_em_utc": agora}]
        self.assertEqual(av.separar_novas([self.boa()], vistas)[1], 0)

    def test_sem_vagas_nao_envia_email(self):
        self.executar()
        self.envio.assert_not_called()
        self.assertFalse(self.arquivo.exists())

    def test_validar_nao_envia_nem_grava(self):
        with patch.object(av, "conectar_gmail", return_value=Mock(__enter__=Mock(), __exit__=Mock(return_value=False))), \
                patch.object(av, "coletar_vagas", return_value=([self.boa()], [], [])):
            av.validar()
        self.envio.assert_not_called()
        self.assertFalse(self.arquivo.exists())


class TestEmail(unittest.TestCase):
    def test_alerta_e_aviso_aparecem_no_email(self):
        aprovada = {**vaga(), "nota": 70, "motivos": ["cargo"], "modelo": "remoto",
                    "alertas": [av.ALERTA_PJ]}
        assunto, corpo = av.montar_email([aprovada], [], [av.AVISO_ADZUNA])
        self.assertIn("1 vagas", assunto)
        self.assertIn("Adzuna indisponível (limite ou plano)", corpo)
        self.assertIn("⚠ verificar se é PJ", corpo)
        self.assertNotIn("Talvez interesse", corpo)

    def test_talvez_interesse_fica_abaixo_e_compacto(self):
        principal = {**vaga(), "nota": 70, "motivos": ["cargo"], "modelo": "remoto",
                     "alertas": []}
        menor = {**vaga(titulo="Analista de Growth"), "nota": 45, "motivos": ["cargo"],
                 "modelo": "remoto", "alertas": []}
        _, corpo = av.montar_email([principal], [menor], [])
        secao = corpo.split("Talvez interesse")[1]
        self.assertIn("Analista de Growth - Empresa Exemplo", secao)
        self.assertIn("https://example.com/vaga", secao)
        self.assertNotIn("Nota:", secao)
        self.assertNotIn("Salario:", secao)


class TestAdzuna(unittest.TestCase):
    ITEM = {
        "id": "123",
        "title": "Analista de Marketing Digital",
        "company": {"display_name": "Empresa"},
        "location": {"display_name": "Curitiba, Paraná", "area": ["Brasil", "Paraná"]},
        "description": "Google Ads",
        "redirect_url": "https://example.com/1",
        "salary_min": 48000,
        "salary_max": 60000,
        "salary_is_predicted": "0",
    }
    AMBIENTE = {"ADZUNA_APP_ID": "id-secreto", "ADZUNA_APP_KEY": "chave-secreta"}

    def setUp(self):
        # Os testes nunca tocam no historico real de uso.
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.arquivo = Path(pasta.name) / "uso.csv"
        trocar = patch.object(av, "ADZUNA_ARQUIVO_USO", self.arquivo)
        trocar.start()
        self.addCleanup(trocar.stop)
        sem_gmail = patch.object(av, "coletar_gmail", return_value=[])
        sem_gmail.start()
        self.addCleanup(sem_gmail.stop)

    def test_registra_cada_chamada_sem_chave(self):
        resposta = Mock(status_code=200)
        resposta.json.return_value = {"results": []}
        with patch.dict("os.environ", self.AMBIENTE), \
                patch.object(av.requests, "get", return_value=resposta), \
                patch.object(av.time, "sleep"), patch("builtins.print"):
            av.coletar_adzuna(PERFIL)
            av.coletar_adzuna(PERFIL)
        uso = av.ler_uso_adzuna()
        self.assertEqual(len(uso), 4)
        self.assertEqual(uso[-1]["total_acumulado"], "4")
        self.assertNotIn("secret", self.arquivo.read_text(encoding="utf-8"))

    def test_chamadas_na_semana_ignora_as_antigas(self):
        agora = datetime.now(timezone.utc)
        uso = [{"data_hora_utc": (agora - timedelta(days=dias)).isoformat()}
               for dias in (1, 6, 8)]
        self.assertEqual(av.chamadas_na_semana(uso), 2)

    def test_recusa_vira_aviso_e_nao_trava(self):
        for status in sorted(av.ADZUNA_HTTP_RECUSA):
            with patch.dict("os.environ", self.AMBIENTE), \
                    patch.object(av.requests, "get", return_value=Mock(status_code=status)), \
                    patch("builtins.print"):
                vagas, falhas, avisos = av.coletar_vagas(PERFIL)
            self.assertEqual((vagas, falhas), ([], ["Adzuna"]))
            self.assertEqual(avisos, [av.AVISO_ADZUNA])

    def test_erro_do_servidor_nao_gera_aviso_de_limite(self):
        with patch.dict("os.environ", self.AMBIENTE), \
                patch.object(av.requests, "get", return_value=Mock(status_code=500)), \
                patch("builtins.print"):
            _, falhas, avisos = av.coletar_vagas(PERFIL)
        self.assertEqual((falhas, avisos), (["Adzuna"], []))

    def test_para_antes_de_passar_do_limite_semanal(self):
        with patch.object(av, "ler_uso_adzuna", return_value=[{}]), \
                patch.object(av, "chamadas_na_semana", return_value=999), \
                patch.dict("os.environ", self.AMBIENTE), \
                patch.object(av.requests, "get") as consultar:
            with self.assertRaises(av.FonteRecusouAcesso):
                av.coletar_adzuna(PERFIL)
        consultar.assert_not_called()

    def test_converte_vaga_com_salario_mensal(self):
        convertida = av.adzuna_para_vaga(self.ITEM)
        self.assertEqual(convertida["id"], "adzuna-123")
        self.assertEqual(convertida["salario"], 5000)

    def test_link_sai_sem_parametros(self):
        item = {**self.ITEM, "redirect_url": "https://example.com/1?utm_source=conta"}
        self.assertEqual(av.adzuna_para_vaga(item)["link"], "https://example.com/1")

    def test_usa_salario_minimo_se_nao_houver_maximo(self):
        item = {**self.ITEM, "salary_max": None}
        self.assertEqual(av.adzuna_para_vaga(item)["salario"], 4000)

    def test_salario_estimado_nao_conta(self):
        item = {**self.ITEM, "salary_is_predicted": "1"}
        self.assertIsNone(av.adzuna_para_vaga(item)["salario"])

    def test_campos_ausentes_nao_quebram(self):
        convertida = av.adzuna_para_vaga({"id": "1"})
        self.assertEqual(convertida["titulo"], "")
        self.assertIsNone(convertida["salario"])

    def test_erro_nunca_mostra_a_chave(self):
        ambiente = {"ADZUNA_APP_ID": "id-secreto", "ADZUNA_APP_KEY": "chave-secreta"}
        erros = [
            requests.ConnectionError("https://api?app_key=chave-secreta"),
            Mock(status_code=429),
        ]
        for erro in erros:
            kwargs = {"side_effect": erro} if isinstance(erro, Exception) else {"return_value": erro}
            with patch.dict("os.environ", ambiente), patch.object(av.requests, "get", **kwargs):
                with self.assertRaises(ConnectionError) as contexto:
                    av.coletar_adzuna(PERFIL)
            self.assertNotIn("secret", str(contexto.exception))

    def test_agenda_fica_abaixo_do_limite_semanal(self):
        buscas = len(PERFIL["busca_adzuna"]["buscas"])
        self.assertLess(buscas * av.EXECUCOES_POR_DIA * 7, av.ADZUNA_LIMITE_SEMANAL)

    def test_conta_as_chamadas_no_log(self):
        ambiente = {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "chave"}
        cheia = Mock(status_code=200)
        cheia.json.return_value = {"results": [self.ITEM] * av.ADZUNA_VAGAS_POR_PAGINA}
        with patch.dict("os.environ", ambiente), \
                patch.object(av.requests, "get", return_value=cheia), \
                patch.object(av.time, "sleep"), \
                patch("builtins.print") as imprimir:
            av.coletar_adzuna(PERFIL)
        imprimir.assert_called_once_with(
            "Chamadas a Adzuna nesta execucao: 2 "
            "(ultimos 7 dias: 2 de 1000; total acumulado: 2)."
        )

    def test_fonte_com_falha_nao_derruba_o_resto(self):
        with patch.dict("os.environ", {"ADZUNA_APP_ID": "", "ADZUNA_APP_KEY": ""}), \
                patch.object(av, "load_dotenv"):
            vagas, falhas, avisos = av.coletar_vagas(PERFIL)
        self.assertEqual(vagas, [])
        self.assertEqual(falhas, ["Adzuna"])
        self.assertEqual(avisos, [])


def bloco_alerta(codigo, titulo, linha, *extras):
    """Reproduz a estrutura de uma vaga no HTML do alerta, com dados inventados."""
    link = f'https://www.linkedin.com/comm/jobs/view/{codigo}/?trackingId=abc'
    textos = "".join(f"<p>{extra}</p>" for extra in extras)
    return (
        f'<a href="{link}"><img alt="Logo"></a>'
        f'<a href="{link}"><table><tr><td><a href="{link}">{titulo}</a></td></tr>'
        f"<tr><td><p>{linha}</p>{textos}</td></tr></table></a>"
    )


RODAPE_ALERTA = '<a href="https://www.linkedin.com/comm/jobs/alerts">Gerenciar alertas</a><p>R$ 0</p>'


def email_alerta(html, remetente="Alertas <jobalerts-noreply@linkedin.com>"):
    mensagem = av.EmailMessage()
    mensagem["From"] = remetente
    mensagem["Subject"] = "Alerta"
    mensagem.set_content("texto")
    mensagem.add_alternative(html, subtype="html")
    return mensagem.as_bytes()


class TestExtracaoLinkedin(unittest.TestCase):
    def extrair(self, *blocos):
        return av.extrair_vagas_linkedin("".join(blocos) + RODAPE_ALERTA)

    def test_extrai_os_campos_da_vaga(self):
        vagas = self.extrair(bloco_alerta(
            "111", "Analista de Marketing Digital Pleno", "Empresa A · Curitiba, PR (Híbrido)",
            "R$ 4.000-R$ 5.000 por mês"))
        self.assertEqual(len(vagas), 1)
        nova = vagas[0]
        self.assertEqual(nova["id"], "linkedin-111")
        self.assertEqual(nova["titulo"], "Analista de Marketing Digital Pleno")
        self.assertEqual(nova["empresa"], "Empresa A")
        self.assertEqual(nova["local"], "Curitiba, PR")
        self.assertEqual(nova["descricao"], "Híbrido")
        self.assertEqual(nova["salario"], 5000)

    def test_link_nao_leva_dados_de_rastreio(self):
        vaga_lida = self.extrair(bloco_alerta("111", "Analista", "Empresa · Curitiba, PR"))[0]
        self.assertEqual(vaga_lida["link"], "https://www.linkedin.com/jobs/view/111/")

    def test_varias_vagas_e_campos_opcionais(self):
        vagas = self.extrair(
            bloco_alerta("111", "Vaga 1", "A · Curitiba, PR (Presencial)", "R$ 7,4-R$ 7,4 por mês"),
            bloco_alerta("222", "Vaga 2", "B · Curitiba, PR", "Recrutando agora"),
        )
        self.assertEqual([v["id"] for v in vagas], ["linkedin-111", "linkedin-222"])
        self.assertEqual([v["salario"] for v in vagas], [None, None])
        self.assertEqual(vagas[1]["descricao"], "")

    def test_formato_desconhecido_e_ignorado_sem_travar(self):
        with patch("builtins.print") as imprimir:
            vagas = self.extrair(bloco_alerta("111", "Vaga", "linha sem separador"))
        self.assertEqual(vagas, [])
        self.assertIn("111", imprimir.call_args[0][0])

    def test_salario_do_alerta(self):
        casos = {
            "R$ 4.000-R$ 5.000 por mês": 5000,
            "R$ 2.750,50 por mês": 2750.5,
            "R$ 60.000 por ano": 5000,
            "R$ 7,4-R$ 7,4 por mês": None,
            "R$ 25 por hora": None,
            "Recrutando agora": None,
        }
        for texto, esperado in casos.items():
            self.assertEqual(av.salario_do_alerta(texto), esperado, texto)

    def test_cargo_principal_sem_descricao_entra_no_email(self):
        nova = {**vaga(titulo="Analista de Marketing Digital"), "sem_descricao": True}
        nota, motivos = av.calcular_nota(nova, PERFIL)
        self.assertEqual(nota, 60)
        self.assertIn("nota minima pelo cargo", motivos[-1])
        self.assertIn(av.ALERTA_SEM_DESCRICAO, av.alertas_da_vaga(nova, PERFIL))

    def test_outros_cargos_sem_descricao_nao_ganham_nota(self):
        nova = {**vaga(titulo="Analista de Branding"), "sem_descricao": True}
        self.assertEqual(av.calcular_nota(nova, PERFIL)[0], 30)

    def test_filtros_valem_para_o_linkedin(self):
        vagas = self.extrair(bloco_alerta(
            "111", "Analista de Marketing Digital", "A · São Paulo, SP (Presencial)"))
        self.assertEqual(av.motivo_descarte(vagas[0], PERFIL), "fora da cidade")
        vagas = self.extrair(bloco_alerta(
            "222", "Vaga para Associado: Analista de Marketing Digital", "A · Curitiba, PR"))
        self.assertEqual(av.motivo_descarte(vagas[0], PERFIL), "palavra excluida no titulo")

    def test_remoto_de_outra_cidade_passa(self):
        vagas = self.extrair(bloco_alerta("111", "Analista de Marketing Digital", "A · Brasil (Remoto)"))
        self.assertIsNone(av.motivo_descarte(vagas[0], PERFIL))

    def test_email_de_outro_remetente_nao_e_lido(self):
        html = bloco_alerta("111", "Vaga", "A · Curitiba, PR")
        self.assertIsNone(av.vagas_do_email(email_alerta(html, "Outro <outro@example.com>")))
        self.assertEqual(len(av.vagas_do_email(email_alerta(html))), 1)


def bloco_alerta_adzuna(codigo, titulo, linha, etiquetas=("NOVA",)):
    """Reproduz a estrutura de uma vaga no alerta da Adzuna, com dados inventados."""
    link = f"https://www.adzuna.com.br/search?d=30&amp;topdoc_id={codigo}&amp;utm_source=x"
    textos = "".join(f"<span>{etiqueta}</span>" for etiqueta in etiquetas)
    return (
        f'<a href="{link}"><h3>{titulo}</h3>{textos}<p>{linha}</p></a>'
        f'<a href="{link}">Leia mais »</a>'
        f'<a href="{link}"><img alt="Emprego"></a>'
    )


class TestExtracaoAdzuna(unittest.TestCase):
    RODAPE = '<a href="https://www.adzuna.com.br/my-alerts">Editar alertas</a><p>Rodape</p>'

    def extrair(self, *blocos):
        return av.extrair_vagas_adzuna("".join(blocos) + self.RODAPE)

    def test_extrai_os_campos_da_vaga(self):
        vagas = self.extrair(bloco_alerta_adzuna(
            "111", "Analista de Marketing Digital", "Empresa A - Curitiba",
            ("FEITA PARA VOCÊ", "NOVA")))
        self.assertEqual(len(vagas), 1)
        nova = vagas[0]
        self.assertEqual(nova["id"], "adzuna-111")
        self.assertEqual(nova["titulo"], "Analista de Marketing Digital")
        self.assertEqual(nova["empresa"], "Empresa A")
        self.assertEqual(nova["local"], "Curitiba")
        self.assertEqual(nova["link"], "https://www.adzuna.com.br/details/111")
        self.assertTrue(nova["sem_descricao"])

    def test_empresa_com_hifen_no_nome(self):
        vagas = self.extrair(bloco_alerta_adzuna("111", "Vaga", "Grupo A - B - Curitiba"))
        self.assertEqual((vagas[0]["empresa"], vagas[0]["local"]), ("Grupo A - B", "Curitiba"))

    def test_titulo_com_hifen_nao_vira_empresa(self):
        vagas = self.extrair(bloco_alerta_adzuna(
            "111", "Analista de Marketing Digital - Curitiba - Pr", "Empresa A - Curitiba"))
        self.assertEqual(vagas[0]["empresa"], "Empresa A")

    def test_varias_vagas_nao_se_misturam(self):
        vagas = self.extrair(
            bloco_alerta_adzuna("111", "Vaga 1", "A - Curitiba"),
            bloco_alerta_adzuna("222", "Vaga 2", "B - Araucaria"),
        )
        self.assertEqual([v["titulo"] for v in vagas], ["Vaga 1", "Vaga 2"])

    def test_email_de_boas_vindas_nao_gera_vaga(self):
        html = '<a href="https://www.adzuna.com.br/search?q=x">Buscar</a><p>Bem-vindo</p>'
        self.assertEqual(av.extrair_vagas_adzuna(html), [])

    def test_formato_desconhecido_e_ignorado_sem_travar(self):
        with patch("builtins.print") as imprimir:
            vagas = self.extrair(bloco_alerta_adzuna("111", "Vaga", "linha sem separador"))
        self.assertEqual(vagas, [])
        self.assertIn("111", imprimir.call_args[0][0])

    def test_filtro_de_cidade_vale_para_o_alerta(self):
        vagas = self.extrair(bloco_alerta_adzuna("111", "Analista de Marketing Digital", "A - Araucaria"))
        self.assertEqual(av.motivo_descarte(vagas[0], PERFIL), "fora da cidade")

    def test_remetente_da_adzuna_tem_leitor(self):
        html = bloco_alerta_adzuna("111", "Vaga", "A - Curitiba")
        bruto = email_alerta(html, "Adzuna <no-reply@adzuna.com.br>")
        self.assertEqual(av.vagas_do_email(bruto)[0]["id"], "adzuna-111")

    def test_vaga_da_api_tem_preferencia_sobre_a_do_alerta(self):
        da_api = {**vaga(descricao="Google Ads e Canva"), "id": "adzuna-111"}
        do_alerta = {**vaga(), "id": "adzuna-111", "sem_descricao": True}
        with patch.object(av, "coletar_adzuna", return_value=[da_api]), \
                patch.object(av, "coletar_gmail", return_value=[do_alerta]), \
                patch("builtins.print"):
            vagas, _, _ = av.coletar_vagas(PERFIL)
        self.assertEqual(len(vagas), 1)
        self.assertEqual(vagas[0]["descricao"], "Google Ads e Canva")


def bloco_alerta_indeed(token, *textos):
    """Reproduz a estrutura de uma vaga no alerta do Indeed, com dados inventados."""
    link = f"https://engage.indeed.com/f/a/{token}/AAR9hBA~/uXYZ"
    linhas = "".join(f"<td>{texto}</td>" for texto in textos)
    return f'<a href="{link}"></a><a href="{link}"><table><tr>{linhas}</tr></table></a>'


class TestExtracaoIndeed(unittest.TestCase):
    RODAPE = (
        '<a href="https://engage.indeed.com/f/a/rodape/AAR9hBA~/x">Editar</a><p>Texto</p>'
        '<a href="https://engage.indeed.com/f/a/menos/AAR9hBA~/x">desde ontem</a><p>-</p>'
    )

    def extrair(self, *blocos):
        return av.extrair_vagas_indeed("".join(blocos) + self.RODAPE)

    def test_extrai_os_campos_da_vaga(self):
        vagas = self.extrair(bloco_alerta_indeed(
            "t1", "Analista de Marketing Digital", "Empresa A", "4.2", "- Curitiba, PR",
            "R$ 3.000 – R$ 4.000 por mês", "Candidate-se facilmente",
            "Experiência com Google Ads e copywriting.", "há 2 dias"))
        self.assertEqual(len(vagas), 1)
        nova = vagas[0]
        self.assertTrue(nova["id"].startswith("indeed-"))
        self.assertEqual(nova["titulo"], "Analista de Marketing Digital")
        self.assertEqual(nova["empresa"], "Empresa A")
        self.assertEqual(nova["local"], "Curitiba, PR")
        self.assertEqual(nova["salario"], 4000)
        self.assertEqual(nova["descricao"], "Experiência com Google Ads e copywriting.")
        self.assertIn("/f/a/t1/", nova["link"])

    def test_sem_nota_da_empresa_e_sem_salario(self):
        vaga_lida = self.extrair(bloco_alerta_indeed(
            "t1", "Analista", "Empresa A", "- Curitiba, PR", "Descrição", "há 1 dia"))[0]
        self.assertEqual(vaga_lida["empresa"], "Empresa A")
        self.assertIsNone(vaga_lida["salario"])

    def test_rodape_nao_vira_vaga(self):
        self.assertEqual(self.extrair(), [])

    def test_mesmo_codigo_mesmo_com_link_diferente(self):
        textos = ("Analista", "Empresa A", "- Curitiba, PR")
        primeira = self.extrair(bloco_alerta_indeed("t1", *textos))[0]
        segunda = self.extrair(bloco_alerta_indeed("t2", *textos))[0]
        self.assertEqual(primeira["id"], segunda["id"])

    def test_vagas_diferentes_tem_codigos_diferentes(self):
        vagas = self.extrair(
            bloco_alerta_indeed("t1", "Analista", "Empresa A", "- Curitiba, PR"),
            bloco_alerta_indeed("t2", "Analista", "Empresa B", "- Curitiba, PR"),
        )
        self.assertNotEqual(vagas[0]["id"], vagas[1]["id"])

    def test_competencia_do_trecho_conta_na_nota(self):
        vaga_lida = self.extrair(bloco_alerta_indeed(
            "t1", "Analista de Marketing Digital", "A", "- Curitiba, PR", "Google Ads, Meta Ads e Canva"))[0]
        self.assertEqual(av.calcular_nota(vaga_lida, PERFIL)[0], 65)

    def test_salario_baixo_descarta(self):
        vaga_lida = self.extrair(bloco_alerta_indeed(
            "t1", "Analista", "A", "- Curitiba, PR", "R$ 1.621 – R$ 3.000 por mês"))[0]
        self.assertEqual(av.motivo_descarte(vaga_lida, PERFIL), "salario abaixo do minimo")

    def test_remetente_do_indeed_tem_leitor(self):
        html = bloco_alerta_indeed("t1", "Analista", "A", "- Curitiba, PR")
        bruto = email_alerta(html, "Indeed <donotreply@jobalert.indeed.com>")
        self.assertEqual(len(av.vagas_do_email(bruto)), 1)

    def test_recomendacoes_do_match_nao_sao_lidas(self):
        html = bloco_alerta_indeed("t1", "Analista", "A", "- Curitiba, PR")
        bruto = email_alerta(html, "Indeed <donotreply@match.indeed.com>")
        self.assertIsNone(av.vagas_do_email(bruto))


class TestGmail(unittest.TestCase):
    AMBIENTE = {"GMAIL_USER": "conta@example.com", "GMAIL_APP_PASSWORD": "senha"}

    def conexao(self, resposta=(b"1 2 3",)):
        falsa = Mock()
        falsa.search.return_value = ("OK", list(resposta))
        bruto = email_alerta(bloco_alerta("111", "Vaga", "A · Curitiba, PR"))
        falsa.fetch.return_value = ("OK", [(b"1", bruto), b")"])
        return falsa

    def test_abre_a_caixa_somente_em_modo_leitura(self):
        falsa = self.conexao()
        with patch.dict("os.environ", self.AMBIENTE), \
                patch.object(av.imaplib, "IMAP4_SSL", return_value=falsa), \
                patch("builtins.print") as imprimir:
            vagas = av.coletar_gmail(PERFIL)
        falsa.select.assert_called_once_with("INBOX", readonly=True)
        falsa.store.assert_not_called()
        falsa.logout.assert_called_once()
        self.assertTrue(all("PEEK" in c[0][1] for c in falsa.fetch.call_args_list))
        self.assertEqual(len(vagas), 3)
        self.assertIn("3 e-mails", imprimir.call_args[0][0])

    def test_falha_do_imap_nao_derruba_as_outras_fontes(self):
        falsa = self.conexao()
        falsa.login.side_effect = av.imaplib.IMAP4.error("recusado")
        vaga_adzuna = {**vaga(), "id": "adzuna-1"}
        with patch.dict("os.environ", self.AMBIENTE), \
                patch.object(av.imaplib, "IMAP4_SSL", return_value=falsa), \
                patch.object(av, "coletar_adzuna", return_value=[vaga_adzuna]), \
                patch("builtins.print"):
            vagas, falhas, avisos = av.coletar_vagas(PERFIL)
        self.assertEqual([v["id"] for v in vagas], ["adzuna-1"])
        self.assertEqual((falhas, avisos), (["Gmail"], []))


if __name__ == "__main__":
    unittest.main()
