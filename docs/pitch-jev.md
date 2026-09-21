# Pitch: Jev no backoffice

A API TypeSafe já rodou neste repo (`JEV_MODE=real`). Dataset ainda sintético:
não use o 97% como resultado em caixa de cliente.

Num lote de teste (n=38, não produção), depois de apertar os critérios true/false
do gate e da verificação, a triagem no Jev real ficou em 97% de categoria,
ECE 0,025, ~280 ms.

## Frase de abertura

O Jev não é um chatbot. É o System One no caminho crítico — triagem, gate e
verificação — com probabilidade calibrada. O LLM só escreve o e-mail.

## Números (Jev real, perguntas apertadas, 21/09/2026)

```bash
uv run backoffice eval-shadow --mode real --stage all --suggest-thresholds
```

| Etapa | Pergunta | Jev real | ECE | n |
|---|---|---|---|---|
| Triagem | categoria | **97%** | **0,025** | 38 |
| Triagem | urgência (±1) | 76% | — | 38 |
| Triagem | precisa humano | 76% | — | 38 |
| Gate | appropriate | 75% | 0,150 | 24 |
| Gate | args_complete | 54% | 0,347 | 24 |
| Verify | resolves | **79%** | **0,090** | 24 |
| Verify | unsupported_claims | 83% | 0,137 | 24 |
| Verify | quality ±1 | 83% | — | 24 |

Latência média do Jev: triagem 283 ms, gate 308 ms, verify 306 ms.

Emulador no mesmo lote (medição anterior às perguntas novas; não rerodado — sem
`LLM_API_KEY`): categoria 97% ECE 0,050; gate appropriate 88% / args 96%;
verify resolves 88%. O cassete do CI agora é determinístico (rótulo → p=0,9)
e só serve para regressão de prompt, não para comparar qualidade com o Jev.

## Como ler no palco

- **Triagem:** 97% de categoria, ECE abaixo da meta (0,05). Um erro: `em-131`
  spam rotulado, Jev disse cancelamento.
- **Gate mais baixo depois de apertar a pergunta:** `args_complete` 54% e
  `appropriate` 75% — o Jev ficou mais exigente com id, fato inventado e
  ferramenta errada. Na política isso vira mais aprovação humana (fail-closed),
  não auto-execução. Não apresente como “o Jev errou o gate”.
- **Verificação subiu:** `resolves` 75% → 79% com critério “responde ou admite
  que falta dado”; `unsupported_claims` segue em 83%.
- **Não diga** que 97% vale para a caixa real do cliente. n=38 sintético.

## O que dizer

1. LLM gera; Jev decide (Noul/Choice/Score).
2. HIGH/CRITICAL nunca são automáticos, mesmo com p=0,99.
3. Input ~10× mais barato que gpt-4.1-mini (US$ 0,042 vs 0,40 / 1M); saída do Jev é grátis.
4. Medição no fluxo: `decisions` + `eval-shadow` + rótulo humano no Telegram.

## Próximo número de verdade

```bash
# caixa real: EMAIL_ADAPTER=imap + IMAP_*/SMTP_* no .env, depois:
uv run backoffice doctor
uv run backoffice labels from-mailbox --limit 100
# dois anotadores preenchem data/labels/mailbox.jsonl (cópia A e B)
uv run backoffice eval-shadow --mode real --stage all \
  --emails data/samples/mailbox.json --labels data/labels/mailbox.jsonl
```
