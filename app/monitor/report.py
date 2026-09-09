"""Генерация отчёта из собранных данных мониторинга.

    python -m app.monitor.report                    # отчёт за сегодня
    python -m app.monitor.report --date 2026-05-23  # за конкретный день
    python -m app.monitor.report --dir ./mon        # из другой папки
    python -m app.monitor.report --json              # вывод в JSON

Анализирует events.jsonl и показывает:
  - Timeline шагов пайплайна (старт/конец, длительность)
  - Ошибки и их частоту
  - Статистику по генерациям (outsee, chatgpt)
  - Ссылки на скриншоты в моменты ошибок
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path


def load_events(events_dir: Path, date_str: str) -> list[dict]:
    """Загружает все события за указанную дату."""
    fpath = events_dir / f"events_{date_str}.jsonl"
    if not fpath.exists():
        return []
    events = []
    with open(fpath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def analyze(events: list[dict]) -> dict:
    """Строит сводку из списка событий."""
    step_durations: dict[str, list[float]] = defaultdict(list)
    errors: list[dict] = []
    screenshots: list[dict] = []
    event_counts = Counter()
    project_steps: dict[int, list[dict]] = defaultdict(list)

    for ev in events:
        event_type = ev.get("event", "")
        event_counts[event_type] += 1

        pid = ev.get("project_id")
        if pid is not None:
            project_steps[pid].append(ev)

        detail = ev.get("detail", {})

        if event_type.endswith("_end") and "duration_s" in detail:
            base = event_type.removesuffix("_end")
            step_durations[base].append(detail["duration_s"])

        if "error_type" in detail or "error_msg" in detail:
            errors.append({
                "ts": ev.get("ts"),
                "event": event_type,
                "project_id": pid,
                "error_type": detail.get("error_type", ""),
                "error_msg": detail.get("error_msg", "")[:200],
                "screenshot": ev.get("screenshot"),
            })

        if ev.get("screenshot"):
            screenshots.append({
                "ts": ev.get("ts"),
                "file": ev.get("screenshot"),
                "event": event_type,
                "tab": detail.get("tab"),
                "url": detail.get("url", "")[:100],
            })

    timing_summary = {}
    for step, durations in sorted(step_durations.items()):
        timing_summary[step] = {
            "count": len(durations),
            "total_s": round(sum(durations), 1),
            "avg_s": round(sum(durations) / len(durations), 1) if durations else 0,
            "min_s": round(min(durations), 1) if durations else 0,
            "max_s": round(max(durations), 1) if durations else 0,
        }

    return {
        "total_events": len(events),
        "event_counts": dict(event_counts.most_common()),
        "timing": timing_summary,
        "errors": errors,
        "screenshots_count": len(screenshots),
        "projects_seen": sorted(project_steps.keys()),
    }


def print_report(analysis: dict, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(analysis, ensure_ascii=False, indent=2, default=str))
        return

    print("=" * 60)
    print("  ОТЧЁТ МОНИТОРИНГА video-pipeline")
    print("=" * 60)

    print(f"\nВсего событий: {analysis['total_events']}")
    print(f"Скриншотов: {analysis['screenshots_count']}")
    print(f"Проекты: {analysis['projects_seen'] or 'нет'}")

    print("\n--- Тайминги ---")
    timing = analysis.get("timing", {})
    if timing:
        print(f"{'Действие':<35} {'Кол-во':>6} {'Сумма':>8} {'Средн':>8} {'Мин':>8} {'Макс':>8}")
        print("-" * 75)
        for step, stats in timing.items():
            print(
                f"{step:<35} {stats['count']:>6} "
                f"{stats['total_s']:>7.1f}s {stats['avg_s']:>7.1f}s "
                f"{stats['min_s']:>7.1f}s {stats['max_s']:>7.1f}s"
            )
    else:
        print("  (нет данных)")

    print("\n--- Ошибки ---")
    errors = analysis.get("errors", [])
    if errors:
        for e in errors[:20]:
            print(
                f"  [{e.get('ts', '?')[:19]}] "
                f"#{e.get('project_id', '?')} "
                f"{e.get('error_type', '?')}: "
                f"{e.get('error_msg', '')[:100]}"
            )
        if len(errors) > 20:
            print(f"  ... и ещё {len(errors) - 20} ошибок")
    else:
        print("  (нет ошибок)")

    print("\n--- Частота событий (топ-15) ---")
    counts = analysis.get("event_counts", {})
    for ev, cnt in list(counts.items())[:15]:
        print(f"  {ev:<40} {cnt:>5}")

    print()


def render_html_report(analysis: dict, *, title: str = "Отчёт мониторинга video-pipeline") -> str:
    """Генерирует автономный HTML-отчёт со стилями и таблицами."""
    import html as _html

    total_events = analysis.get("total_events", 0)
    screenshots_count = analysis.get("screenshots_count", 0)
    projects_seen = analysis.get("projects_seen", [])
    timing = analysis.get("timing", {})
    errors = analysis.get("errors", [])
    event_counts = analysis.get("event_counts", {})

    timing_rows = []
    for step, stats in sorted(timing.items()):
        timing_rows.append(
            f"<tr>"
            f"<td><code>{_html.escape(step)}</code></td>"
            f"<td>{stats.get('count', 0)}</td>"
            f"<td>{stats.get('total_s', 0):.1f}s</td>"
            f"<td>{stats.get('avg_s', 0):.1f}s</td>"
            f"<td>{stats.get('min_s', 0):.1f}s</td>"
            f"<td>{stats.get('max_s', 0):.1f}s</td>"
            f"</tr>"
        )
    timing_tbody = "\n".join(timing_rows) if timing_rows else "<tr><td colspan='6' class='empty'>Нет данных о таймингах</td></tr>"

    error_rows = []
    for e in errors[:50]:
        ts = _html.escape(str(e.get("ts") or "?")[:19])
        pid = _html.escape(str(e.get("project_id") or "—"))
        etype = _html.escape(str(e.get("error_type") or "error"))
        emsg = _html.escape(str(e.get("error_msg") or ""))
        shot = e.get("screenshot")
        shot_html = f"<a href='{_html.escape(shot)}' target='_blank'>скриншот</a>" if shot else "—"
        error_rows.append(
            f"<tr>"
            f"<td class='mono'>{ts}</td>"
            f"<td>#{pid}</td>"
            f"<td><span class='badge badge-err'>{etype}</span></td>"
            f"<td class='msg'>{emsg}</td>"
            f"<td>{shot_html}</td>"
            f"</tr>"
        )
    errors_tbody = "\n".join(error_rows) if error_rows else "<tr><td colspan='5' class='empty' style='color:#4ade80;'>Ошибок не обнаружено!</td></tr>"

    events_rows = []
    for ev, cnt in list(event_counts.items())[:20]:
        events_rows.append(
            f"<tr><td><code>{_html.escape(ev)}</code></td><td>{cnt}</td></tr>"
        )
    events_tbody = "\n".join(events_rows) if events_rows else "<tr><td colspan='2' class='empty'>Нет событий</td></tr>"

    projects_display = ", ".join(f"#{p}" for p in projects_seen) if projects_seen else "нет"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html.escape(title)}</title>
<style>
  :root {{
    --bg: #0f172a;
    --surface: #1e293b;
    --border: #334155;
    --text: #f8fafc;
    --muted: #94a3b8;
    --primary: #38bdf8;
    --accent: #818cf8;
    --err: #f87171;
    --err-bg: rgba(248, 113, 113, 0.15);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px;
    line-height: 1.5;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  header {{ margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 16px; }}
  h1 {{ font-size: 24px; font-weight: 700; color: var(--text); }}
  .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
  .card-label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
  .card-value {{ font-size: 28px; font-weight: 700; color: var(--primary); margin-top: 4px; }}
  .card-value.err {{ color: var(--err); }}
  section {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; }}
  h2 {{ font-size: 18px; font-weight: 600; margin-bottom: 16px; color: var(--primary); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }}
  th {{ background: rgba(0,0,0,0.2); color: var(--muted); font-weight: 600; padding: 10px 12px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); }}
  tr:hover td {{ background: rgba(255,255,255,0.02); }}
  code {{ font-family: ui-monospace, SFMono-Regular, monospace; background: rgba(0,0,0,0.3); padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px; color: var(--muted); }}
  .msg {{ max-width: 450px; word-break: break-word; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .badge-err {{ background: var(--err-bg); color: var(--err); border: 1px solid rgba(248, 113, 113, 0.3); }}
  .empty {{ text-align: center; color: var(--muted); padding: 20px; }}
  a {{ color: var(--primary); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>{_html.escape(title)}</h1>
    <p style="color: var(--muted); font-size: 13px; margin-top: 4px;">Проекты: {_html.escape(projects_display)}</p>
  </header>

  <div class="metrics">
    <div class="card">
      <div class="card-label">Всего событий</div>
      <div class="card-value">{total_events}</div>
    </div>
    <div class="card">
      <div class="card-label">Скриншотов</div>
      <div class="card-value">{screenshots_count}</div>
    </div>
    <div class="card">
      <div class="card-label">Ошибок</div>
      <div class="card-value {'err' if errors else ''}">{len(errors)}</div>
    </div>
    <div class="card">
      <div class="card-label">Уникальных проектов</div>
      <div class="card-value">{len(projects_seen)}</div>
    </div>
  </div>

  <section>
    <h2>⏱ Тайминги шагов</h2>
    <table>
      <thead>
        <tr>
          <th>Действие</th>
          <th>Кол-во</th>
          <th>Сумма</th>
          <th>Среднее</th>
          <th>Мин</th>
          <th>Макс</th>
        </tr>
      </thead>
      <tbody>
        {timing_tbody}
      </tbody>
    </table>
  </section>

  <section>
    <h2>⚠️ Ошибки ({len(errors)})</h2>
    <table>
      <thead>
        <tr>
          <th>Время</th>
          <th>Проект</th>
          <th>Тип</th>
          <th>Сообщение</th>
          <th>Скриншот</th>
        </tr>
      </thead>
      <tbody>
        {errors_tbody}
      </tbody>
    </table>
  </section>

  <section>
    <h2>📊 События (топ-20)</h2>
    <table>
      <thead>
        <tr>
          <th>Событие</th>
          <th>Количество</th>
        </tr>
      </thead>
      <tbody>
        {events_tbody}
      </tbody>
    </table>
  </section>
</div>
</body>
</html>"""


def main() -> None:
    p = argparse.ArgumentParser(
        prog="python -m app.monitor.report",
        description="Анализ данных мониторинга video-pipeline",
    )
    p.add_argument(
        "--dir",
        default="data/monitor",
        help="папка с данными мониторинга",
    )
    p.add_argument(
        "--date",
        default=None,
        help="дата (YYYY-MM-DD), дефолт — сегодня",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="вывод в формате JSON",
    )
    p.add_argument(
        "--html",
        nargs="?",
        const="",
        default=None,
        help="сохранить HTML-отчёт (путь к файлу или дефолт в папку мониторинга)",
    )
    args = p.parse_args()

    monitor_dir = Path(args.dir)
    events_dir = monitor_dir / "events"

    if not events_dir.exists():
        print(f"Папка {events_dir} не найдена. Запускал ли ты мониторинг?")
        sys.exit(1)

    date_str = args.date or datetime.now(tz=UTC).strftime("%Y-%m-%d")

    events = load_events(events_dir, date_str)
    if not events:
        print(f"Нет событий за {date_str} в {events_dir}")
        sys.exit(0)

    analysis = analyze(events)
    print_report(analysis, as_json=args.json)

    if args.html is not None:
        html_path = Path(args.html) if args.html else monitor_dir / f"report_{date_str}.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_html_report(analysis), encoding="utf-8")
        print(f"HTML-отчёт сохранён: {html_path}")


if __name__ == "__main__":
    main()
