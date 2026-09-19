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
    table = Table("etapa", "pergunta", "tipo", "resposta", "conf", "calibrado", "ms")
    for d in runner.store.list_decisions(item_id):
        answer = d["answer"]
        short = answer.get("choice") or (f"{answer['score']:.2f}" if "score" in answer else f"{answer['noul']:.2f}")
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
        rprint(f"#{a['id']} [{a['kind']}] item {a['item_id']}: {json.dumps(a['action'], ensure_ascii=False)[:120]}")


@app.command()
def approve(approval_id: int, reject: bool = typer.Option(False, help="Rejeita em vez de aprovar")) -> None:
    """Aprova (ou rejeita) uma aprovação pendente pela linha de comando e retoma o item."""
    from .channels.telegram_bot import _decide
    rprint(_decide(_runner(), approval_id, not reject, "cli"))


@app.command("eval-shadow")
def eval_shadow(labels: str = typer.Option("data/samples/labeled.jsonl"),
                mode: str = typer.Option("configured", help="configured | real | emulated | both")) -> None:
    """Roda a triagem em sombra contra rótulos humanos e imprime acurácia, ECE e latência."""
    load_dotenv()
    from .eval_shadow import load_dataset, run_shadow
    from .jev.client import RealJevClient
    from .jev.emulated import EmulatedJevClient
    from .llm import build_llm

    settings = get_settings()
    dataset = load_dataset(settings.samples_path, labels)
    clients = []
    if mode in {"real", "both"} or (mode == "configured" and settings.jev_mode == "real"):
        clients.append(("jev-real", RealJevClient(settings.typesafe_api_key or "", settings.jev_model,
                                                  settings.jev_base_url, settings.jev_timeout_s)))
    if mode in {"emulated", "both"} or (mode == "configured" and settings.jev_mode == "emulated"):
        clients.append((f"emulado:{settings.llm_model}", EmulatedJevClient(build_llm(settings))))

    for label_, client in clients:
        result = run_shadow(client, dataset, label_)
        rprint(f"\n[bold]{label_}[/bold]  n={result.n}")
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
