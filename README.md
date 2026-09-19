# backoffice-agents

Piloto de agentes de backoffice em que **o LLM gera e raciocina** e **o Jev decide, roteia e verifica**.
Entrada por e-mail, ferramentas de CRM e ERP, operador humano pelo Telegram.

[Jev](https://typesafe.ai) é o modelo *System One* da TypeSafe AI: não gera texto, responde perguntas
tipadas (sim/não, escolha, nota) com probabilidades calibradas, em 70 a 500 ms, a US$ 0,042 por milhão de
tokens de entrada. Enquanto a chave não chega (early access), o projeto roda com um **emulador** que
usa o próprio LLM no mesmo formato, marcado como não calibrado.

## Fluxo

```
e-mail ──> triagem (Jev) ──> agente LLM + ferramentas ──> verificação (Jev) ──> envio
               │                    │ gate (Jev + política)          │
               │                    ▼                                ▼
               └──> escalar    aprovação humana (Telegram)   revisão humana (Telegram)
```

1. **Triagem** (uma chamada, quatro perguntas): categoria (Choice), urgência (Score 1-5), precisa de
   humano (Noul), dado sensível (Noul). Spam com confiança alta é descartado; pedido de humano escala.
2. **Faixa de confiança** da categoria define o caminho: `auto` (>= 0,85) segue sozinho, `review`
   (>= 0,55) manda a resposta para um humano aprovar, abaixo disso escala.
3. **Agente** (LLM com tool calling) consulta CRM e ERP e redige a resposta. Antes de **cada** ferramenta
   o gate decide: leitura executa; escrita reversível passa pelo Jev (adequada? argumentos completos?);
   alto risco e irreversível **sempre** pedem aprovação humana, independentemente de probabilidade.
4. **Verificação** do rascunho (Jev): resolve o pedido? qualidade? afirma algo sem base nos dados?
   Reprovou: regenera uma vez com o feedback; reprovou de novo: escala.
5. **Envio** pelo adapter de e-mail, registro no CRM, aviso no Telegram.

Toda resposta do Jev (probabilidade, confiança, latência, se é calibrada) fica na tabela `decisions`
com espaço para o rótulo humano, que é o que permite medir calibração ao longo do piloto.

### Proteções

- **Pseudonimização** (`JEV_ANONYMIZE`): antes de sair para o Jev, e-mails, CPF, CNPJ, cartões,
  telefones e os nomes conhecidos do cliente viram tokens estáveis por item (`<email_1>`, `<nome_1>`).
  Números de pedido, nota e rastreio ficam. O e-mail enviado ao cliente não é afetado.
- **Jev fora do ar**: na triagem e na verificação, cai para o emulador (`JEV_FALLBACK_EMULATED`) e
  anota isso no item; sem fallback, o item escala para um humano. No gate, a ação cai para aprovação
  humana. Erros de outra natureza deixam o item em `error`, reprocessado até `MAX_ATTEMPTS` e então
  `failed`, com aviso no Telegram.
- **Efeitos externos no máximo uma vez**: o envio grava um marcador `sending` em disco antes de chamar
  o SMTP e `sent_at` depois; uma aprovação em aplicação fica `applying`. Se o processo morrer no meio,
  a retomada não repete o efeito: o item vai para um humano confirmar no sistema de destino.

## Rodando

Requer Python 3.12 e [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env        # preencha LLM_* (ou exporte OPENAI_API_KEY)
uv sync --python 3.12
uv run pytest               # 31 testes, tudo com mocks e LLM roteirizado

uv run backoffice demo      # ponta a ponta com os 6 e-mails de exemplo e mocks
uv run backoffice items     # lista os itens e o status final
uv run backoffice show email:em-001
uv run backoffice eval-shadow --mode emulated   # triagem x rótulos humanos: acurácia, ECE, latência
```

Operação contínua:

```bash
uv run backoffice worker --watch     # ingere e-mails (IMAP) e processa
uv run backoffice telegram           # aprovações por botão e comandos /pendentes /status /aprovar /rejeitar
docker compose up                    # os dois serviços acima
```

### Trocar de mock para sistema real

| Variável | Valores | Observação |
|---|---|---|
| `LLM_PROVIDER` / `LLM_MODEL` | `openai`, `anthropic`, `bedrock_converse`, `google_genai`, `ollama` | `openai` + `LLM_BASE_URL` cobre Ollama, Groq, vLLM, OpenRouter |
| `JEV_MODE` | `emulated`, `real` | `real` exige `TYPESAFE_API_KEY` |
| `EMAIL_ADAPTER` | `mock`, `imap` | IMAP/SMTP genérico (Gmail com senha de app, Outlook) |
| `TELEGRAM_ADAPTER` | `mock`, `bot` | `bot` exige token e `TELEGRAM_CHAT_ID` autorizado |
| `CRM_ADAPTER` / `ERP_ADAPTER` | `mock` | interfaces em `adapters/crm.py` e `adapters/erp.py`; implemente a classe e registre em `adapters/__init__.py` |

## Layout

```
src/backoffice_agents/
  config.py        variáveis de ambiente
  llm.py           factory de LLM
  jev/             modelos Noul/Choice/Score, cliente HTTP real, emulador
  policy.py        faixas de confiança, níveis de risco, decisão do gate
  privacy.py       pseudonimização do estado enviado ao Jev
  decisions.py     as perguntas feitas ao Jev em cada etapa
  tools.py         ferramentas do agente com nível de risco declarado
  agent_loop.py    loop de tool calling com gate e parada para aprovação
  graph/           grafo LangGraph (triagem, ação, verificação, aprovação, envio)
  adapters/        e-mail (mock, IMAP/SMTP), CRM (mock), ERP (mock), Telegram (mock, Bot API)
  storage.py       SQLite: itens, decisões do Jev, aprovações
  runner.py        monta tudo, processa e retoma itens
  channels/        poller do Telegram
  eval_shadow.py   avaliação em sombra (acurácia, ECE, latência)
  cli.py           comandos
```

## Plano do piloto

| Fase | Entrega | Critério de saída |
|---|---|---|
| 0 Acesso e dados | chave do Jev (waitlist ou Vercel AI Gateway), 300 a 500 e-mails rotulados, política de PII | dataset em português pronto |
| 1 Sombra | `eval-shadow --mode both` no dataset | acurácia do Jev >= LLM; ECE < 0,05 |
| 2 Roteamento por confiança | `JEV_MODE=real` com os limiares achados na fase 1 | custo e p95 caem sem perder acurácia |
| 3 Gates e verificação | gate e verificação em produção | zero ação irreversível sem aprovação |

## Riscos conhecidos

- **Dados nos EUA.** O Jev só existe como API hospedada nos EUA. Zero retenção de dados só no tier
  enterprise. A pseudonimização cobre identificadores diretos; o corpo do e-mail ainda pode conter
  dados sensíveis em texto livre, então negocie o tier enterprise para dados sob LGPD.
- **Português.** A TypeSafe assume inglês como idioma principal. As instruções das perguntas estão em
  inglês e o estado em português; a fase 1 mede se isso basta.
- **Early access.** A API teve instabilidade no lançamento. Veja "Proteções" acima: fallback para o
  emulador, escalada para humano e reprocessamento limitado.
