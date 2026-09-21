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

1. **Triagem** (uma chamada, cinco perguntas): categoria (Choice), urgência (Score 1-5), precisa de
   humano (Noul), dado sensível (Noul), prompt injection (Noul). Spam com confiança alta é descartado;
   pedido de humano (`NEEDS_HUMAN_ESCALATE`) ou dado sensível (`SENSITIVE_ESCALATE`) escala.
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

### Conversa, políticas e anexos

- **Threads**: um e-mail novo é ligado à conversa certa pelos cabeçalhos `In-Reply-To` e
  `References` (inclusive quando responde à nossa mensagem, cujo `Message-ID` fica guardado) ou,
  sem cabeçalhos, por remetente e assunto normalizado dentro de `THREAD_WINDOW_DAYS`. O histórico
  (mensagens do cliente, nossas respostas, itens em aberto) entra na triagem, no prompt do agente e
  na verificação.
- **Base de conhecimento** (`KB_DIR`, arquivos `.md` por política): a ferramenta `kb_search` faz
  uma busca lexical, manda os candidatos ao Jev numa única chamada com um Score de relevância por
  trecho, e só os relevantes chegam ao LLM. O prompt exige consultar a base antes de afirmar
  qualquer prazo ou regra, e a verificação usa os trechos como base para "afirmações sem base".
- **Anexos**: PDF (pypdf), texto e imagens (transcrição pelo LLM com visão) viram texto no estado
  do item, ao lado do corpo do e-mail, e passam pela mesma pseudonimização. Anexos acima de 5 MB
  são registrados sem conteúdo.

### Orçamento de tokens, configuração por cliente e avaliação das três decisões

- **Orçamento de tokens** (`JEV_STATE_BUDGET_TOKENS`, padrão 24 mil): antes de qualquer chamada ao
  Jev, o estado é encolhido em passos previsíveis, do menos para o mais importante (histórico,
  anexos, resultados de ferramentas, corpo do e-mail), até caber. O que foi cortado fica nas notas
  do item. Sem isso, uma thread longa estourava o limite de 32 mil tokens da API.
- **Tenant** (`TENANT_FILE`, padrão `tenants/default.toml`): taxonomia, níveis de urgência e
  qualidade, prompt do agente, nome da empresa e assinatura. A versão do tenant é gravada em cada
  decisão e chamada de modelo, e o relatório de calibração separa por versão, então uma mudança de
  prompt ou de taxonomia nunca se mistura com a anterior.
- **Gate e verificação em sombra**: `eval-shadow --stage gate` usa `gate_labeled.jsonl` (chamadas
  de ferramenta com rótulos de adequada e argumentos completos); `--stage verify` usa
  `verify_labeled.jsonl` (rascunhos com rótulos de resolve, afirmações sem base e qualidade).
  `--stage all` roda as três decisões. Os conjuntos são sintéticos, com positivos e negativos.

### Verificação por afirmação, regressão determinística e observabilidade

- **Fatos verificados um a um**: o agente termina a resposta com uma linha `---FATOS---` e a lista
  dos fatos que afirma (pedido, valores, datas, prazos, promessas). O cliente não vê a lista. Na
  verificação, cada fato vira uma pergunta sim/não ao Jev na mesma chamada. Um fato sem base
  reprova o rascunho e o feedback de regeneração nomeia exatamente qual, com a probabilidade.
  `backoffice show` lista os fatos com o veredito.
- **Cassete de replay**: `eval-shadow --record data/cassettes/eval.json` grava as respostas do
  LLM (emulador) indexadas pelo hash das mensagens; `--replay` responde só pelo cassete, sem rede
  nem chave. O CI roda a avaliação completa em replay com mínimos (`--min-triage`, `--min-gate`,
  `--min-verify`) e falha se a lógica regredir. Mudou prompt, taxonomia ou dataset? Regenere o
  cassete com `PYTHONPATH=src uv run python scripts/rebuild_cassette.py` (respostas alinhadas
  ao rótulo, sem LLM) ou grave de novo com `--record` se quiser o texto do emulador.
- **Logs e métricas**: `LOG_FORMAT=json` emite um evento por linha (triagem, gate, aprovação,
  envio, escalada, erro, alerta) com `item_id` e campos. `backoffice metrics` mostra profundidade
  da fila, erros, escaladas, aprovações pendentes e sua idade, latência média e p95 por modelo e
  custo na janela; `--prometheus` e `metrics-server` expõem no formato do Prometheus;
  `--push-cloudwatch` publica no CloudWatch (extra `aws`). O worker avisa no Telegram, com
  cooldown, quando uma aprovação envelhece, a fila passa do limite ou a taxa de erro sobe.

### Retenção, concorrência e emulador mais barato

- **Retenção (LGPD)**: itens encerrados são redigidos após `RETENTION_REDACT_DAYS` (corpo,
  remetente, anexos, mensagens, rascunho e fatos somem; ficam status, triagem, números da
  verificação e notas, que a calibração e o custo usam) e apagados após `RETENTION_DELETE_DAYS`,
  com decisões, chamadas e aprovações. Itens em aberto nunca são tocados. O worker roda o expurgo
  a cada ciclo; `backoffice purge --dry-run` lista o que seria feito.
- **Concorrência**: `WORKER_CONCURRENCY` processa vários itens em paralelo dentro de um worker,
  com a mesma reserva atômica da fila. `JEV_MAX_RPM` limita as requisições ao Jev real entre as
  threads, abaixo do teto de 1.200 por minuto da API.
- **Emulador mais barato**: `EMULATOR_MODEL` usa outro modelo do mesmo provedor só para o
  emulador do Jev (por exemplo, `gpt-4.1-nano`), com preço próprio em `EMULATOR_PRICE_*` para o
  relatório de custo. O agente continua no modelo principal.

### Rotulagem, calibração e limiares

- **Taxonomia** em `docs/TAXONOMIA.md`: a categoria é a ação operacional pedida; tom e ameaça vão
  para `needs_human` e `urgency`. Guia para rotuladores com regras de desempate.
- **Dois anotadores**: `backoffice labels export` gera o lote; `labels agreement` mede acordo e
  kappa de Cohen por campo; `labels merge` consolida onde concordam e separa conflitos para
  adjudicação. Os 30 e-mails de `emails_eval.json` são sintéticos, servem para exercitar as
  ferramentas, não para tirar conclusões.
- **Rótulo pelo Telegram**: toda notificação final (respondido, escalado, descartado) traz os
  botões "Categoria ok" e "Corrigir categoria". O rótulo vai para a tabela `decisions`;
  `/calibracao` no bot ou `backoffice calibration` mostram acurácia e ECE por pergunta e por modelo,
  separando Jev real de emulador.
- **Limiares por categoria**: `CONFIDENCE_AUTO_BY_CATEGORY` e `CONFIDENCE_REVIEW_BY_CATEGORY`
  (JSON no `.env`) sobrescrevem os globais. `eval-shadow --suggest-thresholds` calcula, por
  categoria, o menor limiar com precisão acima do alvo e imprime a linha pronta para o `.env`;
  `None` significa manter a categoria em revisão humana até haver mais dados.

### Custo, fila e rastreamento

- **Custo e latência reais**: toda chamada a modelo (LLM do agente, emulador, Jev real) vai para a
  tabela `model_calls` com tokens e latência. `backoffice costs` mostra custo por modelo, por item,
  média e p95, e o "e se": quanto os tokens que hoje passam pelo emulador custariam no Jev real.
  Preços em `LLM_PRICE_*_PER_M` e `JEV_PRICE_INPUT_PER_M`.
- **Fila com reserva atômica**: `DB_URL` aceita SQLite (dev) ou Postgres. Cada worker reserva o
  próximo item com `FOR UPDATE SKIP LOCKED` (Postgres) ou transação imediata (SQLite), então vários
  workers e o poller do Telegram convivem sem processar o mesmo item duas vezes. Itens em erro
  voltam depois de `RETRY_DELAY_S`. O `docker-compose.yml` sobe Postgres e dois workers.
- **Rastreamento por item**: `TRACING=langsmith` (com `LANGSMITH_API_KEY`) ou `TRACING=langfuse`
  (extra `langfuse`). Cada execução do grafo vira um trace nomeado pelo item, com nós, chamadas do
  LLM, ferramentas e as consultas ao Jev como spans.

### Proteções

- **Pseudonimização** (`JEV_ANONYMIZE`): antes de sair para o Jev, e-mails, CPF, CNPJ, cartões,
  telefones e os nomes conhecidos do cliente viram tokens estáveis por item (`<email_1>`, `<nome_1>`).
  Números de pedido, nota e rastreio ficam. O e-mail enviado ao cliente não é afetado.
- **Jev fora do ar**: na triagem e na verificação, cai para o emulador (`JEV_FALLBACK_EMULATED`) e
  anota isso no item; sem fallback, o item escala para um humano. No gate, a ação cai para aprovação
  humana. Erros de outra natureza deixam o item em `error`, reprocessado até `MAX_ATTEMPTS` e então
  `failed`, com aviso no Telegram.
- **Prompt injection**: a triagem pergunta ao Jev se o e-mail tenta instruir o assistente (ignorar
  regras, agir em nome de outro cliente, revelar dados). Acima de `INJECTION_ESCALATE` o item escala
  e o conteúdo nunca chega ao LLM. O prompt do agente também trata o e-mail como dado, não instrução.
- **Identidade nas ferramentas**: CRM/ERP só operam no remetente do item. Pedido ou fatura de outro
  cliente volta como não encontrado (sem vazar dados). Encaminhamento só para endereços em
  `EMAIL_FORWARD_ALLOWLIST`.
- **Dado sensível**: noul `sensitive` acima de `SENSITIVE_ESCALATE` escala sem passar pelo LLM.
  `NEEDS_HUMAN_ESCALATE` é o limiar próprio de “precisa de humano”, separado de `CONFIDENCE_AUTO`.
- **IMAP por UID**: ingestão e `mark_processed` usam UID SEARCH/FETCH/STORE, então compactar a caixa
  não renumera `item_id` nem marca a mensagem errada.
- **Operadores do Telegram**: só o chat em `TELEGRAM_CHAT_ID` é aceito e, com `TELEGRAM_OPERATORS`
  (ids de usuário separados por vírgula), só esses usuários aprovam, rejeitam ou rotulam. Quem
  decidiu fica registrado em `approvals.decided_by` e `decisions.human_label_by`.
- **Contrato dos adapters**: `backoffice contracts` roda a mesma suíte contra mocks ou sistemas
  reais (leituras por padrão; `--allow-writes` inclui criar e cancelar pedido). Ao implementar um
  CRM ou ERP real, o grafo não muda; o contrato garante que o adapter se comporta como o mock.
- **CI**: `.github/workflows/ci.yml` roda ruff, a suíte (inclusive o teste de Postgres) e os
  contratos a cada push e pull request.
- **Efeitos externos no máximo uma vez**: o envio grava um marcador `sending` em disco antes de chamar
  o SMTP e `sent_at` depois; uma aprovação em aplicação fica `applying`. Se o processo morrer no meio,
  a retomada não repete o efeito: o item vai para um humano confirmar no sistema de destino.

## Rodando

Requer Python 3.12 ou 3.13 e [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env        # preencha LLM_* (ou exporte OPENAI_API_KEY)
uv sync --python 3.12
uv run pytest               # 60 testes, tudo com mocks e LLM roteirizado (+1 no Postgres com TEST_DB_URL)
uv run backoffice contracts --allow-writes   # suíte de contrato dos adapters configurados

uv run backoffice demo      # ponta a ponta com os 6 e-mails de exemplo e mocks
uv run backoffice items     # lista os itens e o status final
uv run backoffice show email:em-001
uv run backoffice eval-shadow --mode emulated --suggest-thresholds   # triagem x rótulos: acurácia, ECE, limiares
uv run backoffice eval-shadow --mode emulated --stage all            # triagem + gate + verificação
uv run backoffice calibration                   # decisões com rótulo humano, por pergunta e modelo
uv run backoffice costs                         # custo e latência por modelo, "e se" do Jev real
uv run backoffice metrics                       # fila, erros, aprovações pendentes, latência, alertas
uv run backoffice eval-shadow --mode emulated --stage all --replay data/cassettes/eval.json   # sem rede
uv run backoffice labels export --out data/labels/lote1.jsonl        # lote para dois anotadores
uv run backoffice labels from-mailbox --limit 100                    # últimas N da caixa (mock ou IMAP) → lote
uv run backoffice doctor                                             # confere Jev/IMAP/Telegram sem vazar segredo
```

Operação contínua:

```bash
uv run backoffice worker --watch     # ingere e-mails (IMAP) e processa
uv run backoffice telegram           # aprovações por botão e comandos /pendentes /status /aprovar /rejeitar
docker compose up                    # Postgres + 2 workers + poller do Telegram
```

### Trocar de mock para sistema real

| Variável | Valores | Observação |
|---|---|---|
| `LLM_PROVIDER` / `LLM_MODEL` | `openai`, `anthropic`, `bedrock_converse`, `google_genai`, `ollama` | `openai` + `LLM_BASE_URL` cobre Ollama, Groq, vLLM, OpenRouter |
| `JEV_MODE` | `emulated`, `real` | `real` exige `TYPESAFE_API_KEY` |
| `DB_URL` | `sqlite:///...`, `postgresql+psycopg://...` | Postgres exige `uv sync --extra postgres` |
| `TRACING` | `none`, `langsmith`, `langfuse` | Langfuse exige `uv sync --extra langfuse` |
| `EMAIL_ADAPTER` | `mock`, `imap` | IMAP/SMTP genérico (Gmail com senha de app, Outlook) |
| `TELEGRAM_ADAPTER` | `mock`, `bot` | `bot` exige token e `TELEGRAM_CHAT_ID` autorizado |
| `CRM_ADAPTER` / `ERP_ADAPTER` | `mock` ou `modulo:Classe` | implemente o Protocol em `adapters/crm.py` / `erp.py`; o grafo não muda. Rode `backoffice contracts` |
| `EMAIL_FORWARD_ALLOWLIST` | e-mails ou `@dominio.com` | vazio recusa encaminhamento automático |

## Layout

```
src/backoffice_agents/
  config.py        variáveis de ambiente
  llm.py           factory de LLM
  jev/             modelos Noul/Choice/Score, cliente HTTP real, emulador
  policy.py        faixas de confiança, níveis de risco, decisão do gate
  privacy.py       pseudonimização do estado enviado ao Jev
  labeling.py      lote para anotadores, kappa de Cohen, consolidação, export da caixa
  calibration.py   acurácia e ECE das decisões com rótulo humano
  decisions.py     as perguntas feitas ao Jev em cada etapa
  health.py        `backoffice doctor`: Jev, IMAP, Telegram, sem vazar segredo
  tools.py         ferramentas do agente com nível de risco declarado
  agent_loop.py    loop de tool calling com gate e parada para aprovação
  graph/           grafo LangGraph (triagem, ação, verificação, aprovação, envio)
  adapters/        e-mail (mock, IMAP/SMTP por UID), CRM/ERP (mock ou modulo:Classe), Telegram (mock, Bot API)
  storage.py       SQLAlchemy (SQLite/Postgres): fila de itens com reserva atômica, decisões, aprovações, chamadas a modelo
  costs.py         custo e latência por modelo, "e se" do Jev real
  tracing.py       LangSmith / Langfuse por item, spans do Jev
  contracts.py     suíte de contrato dos adapters (mocks e reais)
  budget.py        orçamento de tokens do estado enviado ao Jev
  tenant.py        configuração por cliente (tenants/*.toml)
  claims.py        separa o texto ao cliente da lista de fatos afirmados
  replay.py        gravação e replay das respostas do LLM (regressão no CI)
  obs.py           logs estruturados
  metrics.py       métricas da fila, Prometheus, CloudWatch e alertas
  retention.py     expurgo por retenção em duas fases
  ratelimit.py     limitador de requisições por minuto ao Jev
data/cassettes/    cassete de replay da avaliação
  threads.py       ligação de e-mails à conversa e histórico para o agente
  knowledge.py     base de conhecimento com o Jev pontuando trechos
  attachments.py   texto de PDF, texto e imagens
data/kb/           políticas de exemplo (trocas, prazos, pagamento, garantia)
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
