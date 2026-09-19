from __future__ import annotations

import json
import time

import typer
from dotenv import load_dotenv
from rich import print as rprint
from rich.table import Table

from .config import get_settings

app = typer.Typer(help="Piloto de agentes de backoffice com LLM + Jev", no_args_is_help=True)


def _runner():
    load_dotenv()
    from .runner import Runner
    return Runner.from_settings(get_settings())


@app.command()
def ingest() -> None:
    """Lê e-mails não lidos (mock ou IMAP) e cria itens de trabalho."""
    runner = _runner()
    ids = runner.ingest_emails()
    rprint(f"[green]{len(ids)} item(ns) novo(s)[/green]: {ids}")


@app.command()
def worker(watch: bool = typer.Option(False, help="Fica em loop ingerindo e processando"),
           interval: float = typer.Option(30.0, help="Segundos entre ciclos no modo watch")) -> None:
    """Processa itens novos e retoma os que receberam aprovação."""
    runner = _runner()
    while True:
        runner.ingest_emails()
        for item_id, status in runner.run_pending():
            rprint(f"  {item_id} -> [bold]{status}[/bold]")
        if not watch:
            break
        time.sleep(interval)


@app.command()
def telegram() -> None:
    """Sobe o poller do Telegram (aprovações e comandos). Requer TELEGRAM_ADAPTER=bot."""
    from .channels.telegram_bot import poll_forever
    poll_forever(_runner())


@app.command()
def items(status: str = typer.Option(None, help="Filtra por status")) -> None:
    """Lista itens de trabalho."""
    runner = _runner()
    table = Table("id", "status", "assunto", "categoria", "tier")
    for item in runner.store.list_items(status):
        state = item["state"] or {}
        table.add_row(item["id"], item["status"], item["payload"].get("subject", "")[:50],
                      str(state.get("triage", {}).get("category", "")), str(state.get("tier", "")))
    rprint(table)


@app.command()
def show(item_id: str) -> None:
    """Mostra o estado completo de um item (triagem, notas, rascunho, decisões do Jev)."""
    runner = _runner()
    item = runner.store.get_item(item_id)
    if item is None:
        raise typer.BadParameter(f"item {item_id} não existe")
    state = item["state"] or {}
    rprint(f"[bold]{item['id']}[/bold] status={item['status']} tier={state.get('tier')}")
    rprint("triagem:", state.get("triage"))
    rprint("verificação:", state.get("verification"))
    for note in state.get("notes", []):
        rprint(f"  • {note}")
    if state.get("draft_reply"):
        rprint("\n[bold]Rascunho:[/bold]\n" + state["draft_reply"])
    for claim in (state.get("verification") or {}).get("claims", []):
        mark = "[green]ok[/green]" if claim["supported"] >= 0.5 else "[red]SEM BASE[/red]"
        rprint(f"  fato {mark} ({claim['supported']:.2f}): {claim['text']}")
    table = Table("etapa", "pergunta", "tipo", "resposta", "conf", "calibrado", "ms")
    for d in runner.store.list_decisions(item_id):
        answer = d["answer"]
        if "choice" in answer:
            short = answer["choice"]
        elif "score" in answer:
            short = f"{answer['score']:.2f}"
        else:
            short = f"{answer['noul']:.2f}"
        table.add_row(d["stage"], d["question_id"], d["question_type"], str(short),
                      f"{d['confidence']:.2f}", "sim" if d["calibrated"] else "não", f"{d['latency_ms']:.0f}")
    rprint(table)


@app.command()
def label(item_id: str, question_id: str, value: str,
          stage: str = typer.Option("triage")) -> None:
    """Registra o rótulo humano de uma decisão (para medir calibração no tempo)."""
    runner = _runner()
    n = runner.store.set_human_label(item_id, stage, question_id, value)
    rprint(f"{n} decisão(ões) rotulada(s)")


@app.command()
def approvals() -> None:
    """Lista aprovações pendentes."""
    runner = _runner()
    for a in runner.store.list_approvals("pending"):
        action = json.dumps(a["action"], ensure_ascii=False)[:120]
        rprint(f"#{a['id']} [{a['kind']}] item {a['item_id']}: {action}")


@app.command()
def approve(approval_id: int, reject: bool = typer.Option(False, help="Rejeita em vez de aprovar")) -> None:
    """Aprova (ou rejeita) uma aprovação pendente pela linha de comando e retoma o item."""
    from .channels.telegram_bot import _decide
    rprint(_decide(_runner(), approval_id, not reject, "cli"))


DEFAULT_EMAILS = ["data/samples/emails.json", "data/samples/emails_eval.json"]


@app.command("eval-shadow")
def eval_shadow(labels: str = typer.Option("data/samples/labeled.jsonl"),
                emails: list[str] = typer.Option(DEFAULT_EMAILS, help="arquivos JSON de e-mails"),
                mode: str = typer.Option("configured", help="configured | real | emulated | both"),
                suggest_thresholds: bool = typer.Option(False, help="sugere limiares por categoria"),
                target_precision: float = typer.Option(0.95, help="precisão alvo dos limiares"),
                stage: str = typer.Option("triage", help="triage | gate | verify | all"),
                gate_labels: str = typer.Option("data/samples/gate_labeled.jsonl"),
                verify_labels: str = typer.Option("data/samples/verify_labeled.jsonl"),
                record: str = typer.Option(None, help="grava as respostas do LLM neste cassete JSON"),
                replay: str = typer.Option(None, help="responde só pelo cassete (sem rede nem chave)"),
                min_triage: float = typer.Option(None, help="falha se a acurácia da categoria ficar abaixo"),
                min_gate: float = typer.Option(None, help="falha se alguma pergunta do gate ficar abaixo"),
                min_verify: float = typer.Option(None, help="mínimo por pergunta da verificação")
                ) -> None:
    """Avaliação em sombra contra rótulos humanos: triagem, gate e verificação."""
    load_dotenv()
    from .eval_shadow import load_dataset, load_jsonl, run_gate_shadow, run_shadow, run_verify_shadow
    from .eval_shadow import suggest_thresholds as _suggest
    from .jev.client import RealJevClient
    from .jev.emulated import EmulatedJevClient
    from .llm import build_llm
    from .replay import ReplayChatModel
    from .tenant import load_tenant

    settings = get_settings()
    tenant = load_tenant(settings.tenant_file)
    dataset = load_dataset(emails, labels)
    clients = []
    if mode in {"real", "both"} or (mode == "configured" and settings.jev_mode == "real"):
        clients.append(("jev-real", RealJevClient(settings.typesafe_api_key or "", settings.jev_model,
                                                  settings.jev_base_url, settings.jev_timeout_s)))
    if mode in {"emulated", "both"} or (mode == "configured" and settings.jev_mode == "emulated"):
        if replay:
            llm, label_llm = ReplayChatModel(cassette_path=replay, mode="replay"), f"replay:{replay}"
        elif record:
            llm = ReplayChatModel(cassette_path=record, mode="record", inner=build_llm(settings))
            label_llm = f"emulado:{settings.llm_model} (gravando)"
        else:
            llm, label_llm = build_llm(settings), f"emulado:{settings.llm_model}"
        clients.append((label_llm, EmulatedJevClient(llm)))

    failures: list[str] = []
    for label_, client in clients:
        if stage in {"gate", "all"}:
            gate_result = run_gate_shadow(client, load_jsonl(gate_labels), settings.jev_anonymize)
            _print_stage(gate_result, label_)
            if min_gate is not None:
                failures += [f"gate/{k} {m['accuracy']:.0%} < {min_gate:.0%}"
                             for k, m in gate_result.metrics.items() if m.get("accuracy", 1) < min_gate]
        if stage in {"verify", "all"}:
            verify_result = run_verify_shadow(client, load_jsonl(verify_labels), settings.jev_anonymize,
                                              tenant)
            _print_stage(verify_result, label_)
            if min_verify is not None:
                failures += [f"verify/{k} {m['accuracy']:.0%} < {min_verify:.0%}"
                             for k, m in verify_result.metrics.items() if m.get("accuracy", 1) < min_verify]
        if stage not in {"triage", "all"}:
            continue
        result = run_shadow(client, dataset, label_, anonymize=settings.jev_anonymize, tenant=tenant)
        if min_triage is not None and result.category_accuracy < min_triage:
            failures.append(f"triage/categoria {result.category_accuracy:.0%} < {min_triage:.0%}")
        rprint(f"\n[bold]{label_}[/bold]  triagem n={result.n}")
        rprint(f"  categoria: {result.category_accuracy:.0%}  ECE={result.ece:.3f}")
        rprint(f"  urgência (±1): {result.urgency_accuracy:.0%}")
        rprint(f"  precisa humano: {result.needs_human_accuracy:.0%}")
        rprint(f"  latência média: {result.mean_latency_ms:.0f} ms")
        table = Table("id", "esperado", "previsto", "conf", "urg", "humano", "ms")
        for row in result.rows:
            style = "" if row["expected"] == row["predicted"] else "red"
            table.add_row(*(str(row[k]) for k in ("id", "expected", "predicted", "confidence", "urgency",
                                                    "needs_human", "latency_ms")), style=style)
        rprint(table)
        if suggest_thresholds:
            suggested = _suggest(result.rows, target_precision)
            rprint(f"\n[bold]Limiares sugeridos[/bold] (precisão >= {target_precision:.0%}; "
                   "None = manter em revisão humana):")
            for category, threshold in suggested.items():
                rprint(f"  {category}: {threshold}")
            usable = {k: v for k, v in suggested.items() if v is not None}
            rprint("\nPara o .env:\nCONFIDENCE_AUTO_BY_CATEGORY=" + json.dumps(usable, ensure_ascii=False))
    if failures:
        rprint("[red]Abaixo do mínimo:[/red] " + "; ".join(failures))
        raise typer.Exit(code=1)


def _print_stage(result, label_: str) -> None:
    rprint(f"\n[bold]{label_}[/bold]  {result.stage} n={result.n}  "
           f"latência média {result.mean_latency_ms:.0f} ms")
    for key, m in result.metrics.items():
        if "accuracy" in m:
            rprint(f"  {key}: acurácia {m['accuracy']:.0%}  ECE={m['ece']:.3f}")
        else:
            rprint(f"  {key}: dentro de ±1 em {m['within_one']:.0%}")
    columns = [k for k in result.rows[0] if k != "id"] if result.rows else []
    table = Table("id", *columns)
    for row in result.rows:
        wrong = any(k.endswith("_label") and ((row[k[:-6]] >= 0.5) != row[k]) for k in columns
                    if isinstance(row[k], bool))
        table.add_row(row["id"], *(str(row[k]) for k in columns), style="red" if wrong else "")
    rprint(table)


labels_app = typer.Typer(help="Rotulagem por dois anotadores (ver docs/TAXONOMIA.md)")
app.add_typer(labels_app, name="labels")


@labels_app.command("export")
def labels_export(out: str = typer.Option(..., help="JSONL de saída para os anotadores"),
                  emails: list[str] = typer.Option(DEFAULT_EMAILS),
                  from_db: bool = typer.Option(False, help="inclui e-mails já ingeridos no banco")) -> None:
    """Exporta um lote com os campos de rótulo vazios."""
    from pathlib import Path

    from .eval_shadow import load_emails
    from .labeling import export_batch

    rows = load_emails(emails)
    if from_db:
        runner = _runner()
        rows += [dict(item["payload"], id=item["id"]) for item in runner.store.list_items()
                 if item["source"] == "email"]
    rprint(f"{export_batch(rows, Path(out))} e-mail(s) exportado(s) para {out}")


@labels_app.command("agreement")
def labels_agreement(a: str, b: str) -> None:
    """Concordância entre dois anotadores: acordo exato, kappa de Cohen e discordâncias."""
    from pathlib import Path

    from .labeling import agreement, load_labels

    report = agreement(load_labels(Path(a)), load_labels(Path(b)))
    table = Table("campo", "n", "acordo", "kappa", "±1")
    for f in report.fields:
        table.add_row(f.field, str(f.n), f"{f.exact:.0%}", f"{f.kappa:.2f}",
                      f"{f.within_one:.0%}" if f.within_one is not None else "")
    rprint(table)
    for d in report.disagreements:
        rprint(f"  {d['id']} {d['field']}: A={d['a']} B={d['b']}")
    if report.only_in_a or report.only_in_b:
        rprint(f"só em A: {report.only_in_a}  só em B: {report.only_in_b}")


@labels_app.command("merge")
def labels_merge(a: str, b: str, out: str = typer.Option(..., help="JSONL consolidado (formato labeled)"),
                 conflicts: str = typer.Option("data/labels/conflitos.jsonl")) -> None:
    """Consolida os rótulos em que os dois concordam; o resto vai para adjudicação."""
    from pathlib import Path

    from .labeling import load_labels, merge, write_jsonl

    merged, conflicted = merge(load_labels(Path(a)), load_labels(Path(b)))
    write_jsonl(merged, Path(out))
    write_jsonl(conflicted, Path(conflicts))
    rprint(f"{len(merged)} consolidado(s) em {out}; {len(conflicted)} conflito(s) em {conflicts}")


@app.command()
def contracts(allow_writes: bool = typer.Option(False, help="executa também os checks de escrita"),
              known_email: str = typer.Option("mariana.souza@lojaazul.com.br"),
              known_order: str = typer.Option("PED-78231"),
              known_sku: str = typer.Option("SKU-1001")) -> None:
    """Suíte de contrato contra os adapters configurados (mocks ou sistemas reais)."""
    from .adapters import build_adapters
    from .contracts import check_all

    load_dotenv()
    # só os adapters: não precisa de LLM nem de chave de API (roda no CI sem segredos)
    results = check_all(build_adapters(get_settings()), known_email, known_order, known_sku, allow_writes)
    failed_total = 0
    for r in results:
        rprint(f"[bold]{r.adapter}[/bold]: {len(r.passed)} ok, {len(r.failed)} falhas, "
               f"{len(r.skipped)} pulados")
        for name, reason in r.failed:
            rprint(f"  [red]✗ {name}[/red]: {reason}")
        failed_total += len(r.failed)
    raise typer.Exit(code=1 if failed_total else 0)


@app.command()
def costs(item_id: str = typer.Option(None, help="restringe a um item")) -> None:
    """Custo e latência reais por modelo, com o 'e se' do Jev real no lugar do emulador."""
    from .costs import cost_report

    runner = _runner()
    report = cost_report(runner.store.list_model_calls(item_id), runner.settings)
    if report.items == 0:
        rprint("Nenhuma chamada de modelo registrada ainda.")
        return
    table = Table("tipo", "modelo", "chamadas", "tokens in", "tokens out", "US$", "média ms", "p95 ms")
    for u in report.by_model:
        table.add_row(u.kind, u.model, str(u.calls), f"{u.input_tokens:,}", f"{u.output_tokens:,}",
                      f"{u.cost_usd:.4f}", f"{u.mean_latency_ms:.0f}", f"{u.p95_latency_ms:.0f}")
    rprint(table)
    rprint(f"itens: {report.items}  total: US$ {report.total_cost_usd:.4f}  "
           f"por item: US$ {report.cost_per_item_usd:.4f}  "
           f"(LLM {report.llm_cost_usd:.4f} + Jev {report.jev_cost_usd:.4f})")
    if report.emulated_input_tokens:
        rprint(f"[bold]E se[/bold] as {report.emulated_input_tokens:,} tokens de entrada do emulador "
               f"fossem ao Jev real: US$ {report.what_if_real_jev_usd:.4f} "
               f"em vez de US$ {report.emulated_cost_usd:.4f} "
               f"({report.emulated_cost_usd / max(report.what_if_real_jev_usd, 1e-9):.0f}x mais barato, "
               "saída grátis; latência de 70 a 500 ms segundo a TypeSafe)")


@app.command()
def metrics(window: int = typer.Option(60, help="janela em minutos"),
            as_json: bool = typer.Option(False, "--json"),
            prometheus: bool = typer.Option(False, help="saída no formato de exposição do Prometheus"),
            push_cloudwatch: bool = typer.Option(False, help="envia ao CloudWatch (extra 'aws')")) -> None:
    """Métricas da fila: profundidade, erros, aprovações pendentes, latência e custo na janela."""
    from .metrics import alerts, collect_metrics, to_prometheus
    from .metrics import push_cloudwatch as _push

    runner = _runner()
    data = collect_metrics(runner.store, runner.settings, window)
    if push_cloudwatch:
        n = _push(data, runner.settings.metrics_namespace, runner.settings.aws_region)
        rprint(f"{n} métricas enviadas ao CloudWatch ({runner.settings.metrics_namespace})")
        return
    if prometheus:
        print(to_prometheus(data), end="")
        return
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    rprint(f"fila: {data['queue_depth']}  por status: {data['by_status']}")
    rprint(f"últimos {window} min: processados {data['processed']}, erros {data['errors']} "
           f"({data['error_rate']:.0%}), escalados {data['escalated']} ({data['escalation_rate']:.0%})")
    rprint(f"aprovações pendentes: {data['pending_approvals']} "
           f"(mais antiga há {data['oldest_pending_approval_min']:.0f} min)")
    for kind, lat in data["latency"].items():
        rprint(f"{kind}: {lat['count']} chamadas, média {lat['mean_ms']:.0f} ms, p95 {lat['p95_ms']:.0f} ms")
    rprint(f"custo na janela: US$ {data['cost_usd']:.4f} (por item US$ {data['cost_per_item_usd']:.4f})")
    for text in alerts(data, runner.settings):
        rprint(f"[red]ALERTA[/red] {text}")


@app.command("metrics-server")
def metrics_server(port: int = typer.Option(9100), window: int = typer.Option(60)) -> None:
    """Servidor HTTP com /metrics (Prometheus) e /healthz."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from .metrics import collect_metrics, to_prometheus

    runner = _runner()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                body = b"ok"
            elif self.path == "/metrics":
                body = to_prometheus(collect_metrics(runner.store, runner.settings, window)).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            return

    rprint(f"métricas em http://0.0.0.0:{port}/metrics")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


@app.command()
def calibration() -> None:
    """Calibração das decisões com rótulo humano, por pergunta e por modelo."""
    from .calibration import calibration_report

    runner = _runner()
    rows = calibration_report(runner.store.list_decisions())
    if not rows:
        rprint("Nenhuma decisão rotulada ainda. Use os botões no Telegram ou `backoffice label`.")
        return
    table = Table("pergunta", "modelo", "n", "acurácia", "ECE", "conf. média")
    for r in rows:
        table.add_row(r.question_id, r.model, str(r.n), f"{r.accuracy:.0%}", f"{r.ece:.3f}",
                      f"{r.mean_confidence:.2f}")
    rprint(table)


@app.command()
def demo() -> None:
    """Ponta a ponta com mocks: ingere os e-mails de exemplo, processa e retoma aprovações."""
    runner = _runner()
    ids = runner.ingest_emails()
    rprint(f"[green]{len(ids)} e-mail(s) ingerido(s)[/green]")
    for item_id, status in runner.run_pending():
        rprint(f"  {item_id} -> [bold]{status}[/bold]")
    rprint("\nEnvios registrados no mock de e-mail:")
    for sent in getattr(runner.adapters.email, "sent", []):
        rprint(f"  [{sent.kind}] para {sent.to}: {sent.subject}")


if __name__ == "__main__":
    app()
