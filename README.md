# Alerta de Vagas

Script em Python que procura vagas de emprego, dá uma nota de 0 a 100 conforme um perfil
e envia um único e-mail com as vagas mais aderentes.

**O projeto só avisa.** Ele não faz login em sites de vagas nem se candidata a nada.

## O que ele faz

- Coleta vagas da API da [Adzuna](https://developer.adzuna.com/) e dos alertas que chegam ao
  Gmail (LinkedIn, Adzuna e Indeed), lidos por IMAP em modo somente leitura.
- Descarta vagas por palavras, senioridade, cidade, salário e tipo de contrato.
- Dá uma nota por cargo, competências, modelo de trabalho, nível e lacunas.
- Envia 1 e-mail com a lista principal e uma seção compacta "Talvez interesse".
- Guarda as vagas vistas em CSV, para não repetir o alerta.
- Se uma fonte falhar, registra no log e segue com as outras.

## Como usar

1. Instale o Python 3.14 e as bibliotecas:

   ```bash
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. Copie `.env.example` para `.env` e preencha. O Gmail exige uma
   [senha de app](https://support.google.com/accounts/answer/185833).
3. Copie `perfil.exemplo.yaml` para `perfil.yaml` e troque pelo seu perfil.
4. Rode os comandos abaixo:

| Comando | O que faz |
|---|---|
| `python alerta_vagas.py --validar` | Mostra a prévia do e-mail, sem enviar |
| `python alerta_vagas.py --teste-email` | Envia 1 e-mail de exemplo |
| `python alerta_vagas.py` | Busca as vagas, envia o alerta e grava o histórico |
| `python -m unittest` | Roda os testes, com dados inventados e sem internet |

## Rodar na nuvem

O arquivo `exemplos/workflow_alerta_vagas.yml` agenda o script no GitHub Actions.
Use-o **somente em um repositório privado**: o histórico de vagas e os logs revelam a sua
busca de emprego. As chaves ficam em GitHub Secrets.

## Dados pessoais

`.env`, `perfil.yaml` e os arquivos de `dados/` ficam fora do Git. Nunca coloque senhas,
chaves ou dados pessoais no código ou em um commit.

## Limitações

- Os limites do plano gratuito da Adzuna devem ser conferidos na sua conta.
- A leitura dos alertas depende do formato dos e-mails, que pode mudar sem aviso.
- Os e-mails de alerta trazem pouca ou nenhuma descrição. Confira modelo de trabalho e tipo
  de contrato na página da vaga.
