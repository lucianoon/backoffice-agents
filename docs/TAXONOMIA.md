# Taxonomia de triagem e guia de rotulagem

Três campos por e-mail. Cada um responde a UMA pergunta. Rotule os três de forma independente.

## 1. `category`: qual ação operacional o cliente pede?

Regra de ouro: **a categoria é a ação concreta pedida, não o tom**. Um cliente furioso que pede a
troca de um produto quebrado é `suporte_tecnico`, e a raiva vai para `needs_human` e `urgency`.

| Categoria | Use quando | Não use quando |
|---|---|---|
| `status_pedido` | pergunta onde está um pedido já feito: rastreio, prazo, atraso | ainda não comprou (`comercial_vendas`) |
| `financeiro_cobranca` | 2ª via de boleto, fatura, pagamento não reconhecido, estorno de valor cobrado, nota fiscal | quer cancelar antes de pagar (`cancelamento`) |
| `comercial_vendas` | cotação, preço, desconto, disponibilidade ou prazo para compra futura | já tem pedido e quer saber do prazo (`status_pedido`) |
| `cancelamento` | cancelar pedido, assinatura ou contrato ainda não entregue/encerrado | já recebeu e quer devolver por defeito (`suporte_tecnico`) |
| `suporte_tecnico` | produto com defeito, avaria, item errado, garantia, troca, devolução, dúvida de uso | |
| `reclamacao_atendimento` | reclama do atendimento em si (sem resposta, demora, grosseria, quer falar com gerente) **e não pede mais nada concreto** | há qualquer pedido operacional junto: rotule o pedido |
| `spam_irrelevante` | marketing, phishing, newsletter, assunto sem relação com a empresa | |
| `outro` | pedido legítimo que não cabe acima (parceria, imprensa, RH, LGPD) | quando é só dúvida de qual categoria: escolha a mais próxima |

Desempate quando há dois pedidos: rotule o que exige ação primeiro (cancelar antes de 2ª via;
troca antes de rastreio). Registre o segundo no campo de observação, se houver.

## 2. `urgency`: quão urgente, de 1 a 5?

| Nível | Critério |
|---|---|
| 1 | informativo, pode esperar dias |
| 2 | rotina, responder em até 2 dias úteis |
| 3 | cliente espera resposta hoje |
| 4 | prazo em até 24 h ou dinheiro em risco (boleto vencendo, cancelar antes de despachar) |
| 5 | ameaça legal/Procon, contato repetido sem resposta, cliente sem serviço |

## 3. `needs_human`: um humano precisa assumir?

`true` quando houver qualquer um: raiva explícita ou ameaça legal; negociação (desconto, condição
especial); pedido irreversível (cancelar, estornar); dado inconsistente ou ambíguo; assunto fora do
que as ferramentas cobrem. `false` quando uma consulta e uma resposta padrão resolvem.

## Processo

1. Exporte o lote: `backoffice labels export --out data/labels/lote1.jsonl`.
2. Dois rotuladores preenchem cópias independentes (`lote1_a.jsonl`, `lote1_b.jsonl`).
3. Meça a concordância: `backoffice labels agreement lote1_a.jsonl lote1_b.jsonl`.
   Meta: kappa de Cohen >= 0,7 em `category`. Abaixo disso, a taxonomia ou o guia estão ambíguos:
   revise este documento antes de continuar.
4. Consolide: `backoffice labels merge lote1_a.jsonl lote1_b.jsonl --out data/samples/labeled.jsonl
   --conflicts data/labels/lote1_conflitos.jsonl`. Um terceiro adjudica os conflitos.
5. Rode `backoffice eval-shadow --mode both --suggest-thresholds`.

Tamanho alvo para a fase 1: 300 a 500 e-mails reais, com pelo menos 20 por categoria.
