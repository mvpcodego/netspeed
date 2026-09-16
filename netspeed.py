#!/usr/bin/env python3
"""Замер скорости скачивания: N последовательных запросов к одному адресу.

Зависимостей нет — только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, asdict

CHUNK = 64 * 1024
BITS_IN_BYTE = 8
MB = 1_000_000  # мегабит/мегабайт в десятичном смысле, как у провайдеров
MIB = 1024 * 1024


@dataclass
class Attempt:
    """Результат одного запроса."""

    ok: bool
    status: int | None = None
    bytes_read: int = 0
    connect_s: float = 0.0  # DNS + TCP + TLS + отправка запроса, до первого байта
    transfer_s: float = 0.0  # только чтение тела
    total_s: float = 0.0
    content_length: int | None = None
    error: str | None = None

    @property
    def transfer_mbit_s(self) -> float:
        if not self.ok or self.transfer_s <= 0:
            return 0.0
        return self.bytes_read * BITS_IN_BYTE / self.transfer_s / MB

    @property
    def effective_mbit_s(self) -> float:
        """Скорость с учётом установки соединения — то, что чувствует пользователь."""
        if not self.ok or self.total_s <= 0:
            return 0.0
        return self.bytes_read * BITS_IN_BYTE / self.total_s / MB


def build_request(url: str, bust_cache: bool, keep_alive: bool) -> urllib.request.Request:
    """Собирает запрос так, чтобы замер не соврал.

    Кэш: браузерный и прокси-кэш отдадут файл мгновенно со второго запроса,
    и скорость получится фантастической. Отсюда уникальный параметр в адресе
    и заголовки запрета кэша.

    Сжатие: с gzip прочитанный объём не равен размеру файла на диске,
    и скорость завышается. Просим отдавать без сжатия.
    """
    target = url
    if bust_cache:
        sep = "&" if "?" in url else "?"
        target = f"{url}{sep}_cb={uuid.uuid4().hex}"

    headers = {
        "User-Agent": "netspeed/1.0 (+https://github.com/)",
        "Accept": "*/*",
        "Accept-Encoding": "identity",  # без сжатия, иначе объём не совпадёт
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
        "Connection": "keep-alive" if keep_alive else "close",
    }
    return urllib.request.Request(target, headers=headers, method="GET")


def download_once(url: str, timeout: float, bust_cache: bool, keep_alive: bool) -> Attempt:
    """Один запрос с раздельным замером соединения и передачи."""
    req = build_request(url, bust_cache, keep_alive)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Ответ получен: соединение установлено, заголовки пришли.
            first_byte_at = time.perf_counter()
            raw_len = resp.headers.get("Content-Length")
            content_length = int(raw_len) if raw_len and raw_len.isdigit() else None

            read = 0
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                read += len(chunk)
            finished = time.perf_counter()

            return Attempt(
                ok=True,
                status=resp.status,
                bytes_read=read,
                connect_s=first_byte_at - started,
                transfer_s=finished - first_byte_at,
                total_s=finished - started,
                content_length=content_length,
            )
    except urllib.error.HTTPError as exc:
        return Attempt(ok=False, status=exc.code, total_s=time.perf_counter() - started,
                       error=f"HTTP {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        return Attempt(ok=False, total_s=time.perf_counter() - started,
                       error=f"сеть: {exc.reason}")
    except (socket.timeout, TimeoutError):
        return Attempt(ok=False, total_s=time.perf_counter() - started,
                       error=f"таймаут {timeout} с")
    except ssl.SSLError as exc:
        return Attempt(ok=False, total_s=time.perf_counter() - started, error=f"TLS: {exc}")


def summarize(attempts: list[Attempt]) -> dict:
    """Сводка по успешным запросам."""
    ok = [a for a in attempts if a.ok]
    if not ok:
        return {"ok_count": 0, "failed_count": len(attempts)}

    transfer_speeds = [a.transfer_mbit_s for a in ok]
    effective_speeds = [a.effective_mbit_s for a in ok]
    total_times = [a.total_s for a in ok]
    connect_times = [a.connect_s for a in ok]
    total_bytes = sum(a.bytes_read for a in ok)

    # Медиана рядом со средним не для красоты: одна просадка канала
    # сдвигает среднее, а медиану — почти нет. Расхождение между ними
    # само по себе признак нестабильного соединения.
    return {
        "ok_count": len(ok),
        "failed_count": len(attempts) - len(ok),
        "total_bytes": total_bytes,
        "total_mb": total_bytes / MB,
        "total_mib": total_bytes / MIB,
        "avg_request_s": statistics.fmean(total_times),
        "median_request_s": statistics.median(total_times),
        "min_request_s": min(total_times),
        "max_request_s": max(total_times),
        "avg_connect_s": statistics.fmean(connect_times),
        "avg_transfer_mbit_s": statistics.fmean(transfer_speeds),
        "median_transfer_mbit_s": statistics.median(transfer_speeds),
        "avg_effective_mbit_s": statistics.fmean(effective_speeds),
        "avg_transfer_mib_s": statistics.fmean(transfer_speeds) / BITS_IN_BYTE * MB / MIB,
    }


def check_consistency(attempts: list[Attempt]) -> list[str]:
    """Проверки, без которых замер может выглядеть правильным и быть неверным."""
    warnings: list[str] = []
    ok = [a for a in attempts if a.ok]
    if not ok:
        return warnings

    sizes = {a.bytes_read for a in ok}
    if len(sizes) > 1:
        warnings.append(
            f"объём ответа менялся между запросами: {sorted(sizes)} байт — "
            "возможно, адрес отдаёт разный контент"
        )

    for i, a in enumerate(ok, 1):
        if a.content_length is not None and a.content_length != a.bytes_read:
            warnings.append(
                f"запрос {i}: Content-Length {a.content_length} != прочитано {a.bytes_read}"
            )

    smallest = min(a.bytes_read for a in ok)
    if smallest < 512 * 1024:
        warnings.append(
            f"файл маленький ({smallest / 1024:.0f} КБ) — на таком объёме замер "
            "определяется задержкой, а не пропускной способностью; возьмите файл от 5 МБ"
        )

    fastest = max(a.transfer_mbit_s for a in ok)
    slowest = min(a.transfer_mbit_s for a in ok if a.transfer_mbit_s > 0)
    if slowest and fastest / slowest > 3:
        warnings.append(
            f"разброс скорости больше трёх раз ({slowest:.1f}–{fastest:.1f} Мбит/с) — "
            "либо канал нестабилен, либо часть ответов пришла из кэша"
        )
    return warnings


def print_report(url: str, attempts: list[Attempt], summary: dict, warnings: list[str]) -> None:
    print(f"\nАдрес: {url}")
    print(f"Запросов: {len(attempts)} последовательно\n")

    print(f"{'#':>3}  {'статус':>6}  {'объём':>10}  {'соединение':>11}  "
          f"{'передача':>9}  {'итого':>8}  {'Мбит/с':>8}")
    print("-" * 72)
    for i, a in enumerate(attempts, 1):
        if a.ok:
            print(f"{i:>3}  {a.status:>6}  {a.bytes_read / MB:>7.2f} МБ  "
                  f"{a.connect_s * 1000:>8.0f} мс  {a.transfer_s:>7.2f} с  "
                  f"{a.total_s:>6.2f} с  {a.transfer_mbit_s:>8.1f}")
        else:
            print(f"{i:>3}  {'—':>6}  {'—':>10}  {'—':>11}  {'—':>9}  "
                  f"{a.total_s:>6.2f} с  ошибка: {a.error}")

    if summary.get("ok_count", 0) == 0:
        print("\nНи один запрос не прошёл — замерять нечего.")
        return

    print("\nИТОГО")
    print(f"  успешно / всего:        {summary['ok_count']} / {len(attempts)}")
    print(f"  скачано суммарно:       {summary['total_mb']:.2f} МБ "
          f"({summary['total_mib']:.2f} МиБ)")
    print(f"  среднее время запроса:  {summary['avg_request_s']:.3f} с "
          f"(медиана {summary['median_request_s']:.3f} с, "
          f"от {summary['min_request_s']:.3f} до {summary['max_request_s']:.3f} с)")
    print(f"  из них на соединение:   {summary['avg_connect_s'] * 1000:.0f} мс в среднем")
    print()
    print(f"  СКОРОСТЬ ПЕРЕДАЧИ:      {summary['avg_transfer_mbit_s']:.1f} Мбит/с "
          f"({summary['avg_transfer_mib_s']:.2f} МиБ/с)")
    print(f"  медиана:                {summary['median_transfer_mbit_s']:.1f} Мбит/с")
    print(f"  с учётом соединения:    {summary['avg_effective_mbit_s']:.1f} Мбит/с")
    print()
    print("  Первое число — пропускная способность канала (делим объём на время чтения тела).")
    print("  Второе — то, что чувствует пользователь: в него входят DNS, TCP и TLS.")

    if warnings:
        print("\nНА ЧТО ОБРАТИТЬ ВНИМАНИЕ")
        for w in warnings:
            print(f"  · {w}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Замер скорости скачивания: N последовательных запросов к одному адресу.",
        epilog="Пример: python netspeed.py https://speed.hetzner.de/10MB.bin -n 10",
    )
    parser.add_argument("url", help="адрес файла (лучше от 5 МБ)")
    parser.add_argument("-n", "--requests", type=int, default=10,
                        help="сколько запросов сделать (по умолчанию 10)")
    parser.add_argument("-t", "--timeout", type=float, default=30.0,
                        help="таймаут одного запроса в секундах (по умолчанию 30)")
    parser.add_argument("--allow-cache", action="store_true",
                        help="не обходить кэш (по умолчанию обходим: иначе замер соврёт)")
    parser.add_argument("--close-connection", action="store_true",
                        help="закрывать соединение после каждого запроса")
    parser.add_argument("--json", action="store_true", help="вывести результат как JSON")
    args = parser.parse_args(argv)

    if args.requests < 1:
        parser.error("число запросов должно быть не меньше 1")
    if not args.url.lower().startswith(("http://", "https://")):
        parser.error("адрес должен начинаться с http:// или https://")

    attempts: list[Attempt] = []
    for i in range(1, args.requests + 1):
        if not args.json:
            print(f"запрос {i}/{args.requests}...", end="\r", flush=True)
        attempts.append(download_once(
            args.url,
            timeout=args.timeout,
            bust_cache=not args.allow_cache,
            keep_alive=not args.close_connection,
        ))

    summary = summarize(attempts)
    warnings = check_consistency(attempts)

    if args.json:
        print(json.dumps({
            "url": args.url,
            "summary": summary,
            "warnings": warnings,
            "attempts": [asdict(a) for a in attempts],
        }, ensure_ascii=False, indent=2))
    else:
        print(" " * 40, end="\r")
        print_report(args.url, attempts, summary, warnings)

    return 0 if summary.get("ok_count", 0) else 1


if __name__ == "__main__":
    sys.exit(main())
