"""レポートランナー: JST 7:00 / 19:00 にレポート生成してDiscord送信（複数PNG対応）"""

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp

from src.reports.generator import generate

JST = timezone(timedelta(hours=9))
WEBHOOK_URL = os.environ.get('REPORT_WEBHOOK_URL') or os.environ.get('DISCORD_WEBHOOK_URL', '')
REPORT_HOURS_JST = [int(h) for h in os.environ.get('REPORT_HOURS_JST', '7,19').split(',')]
OUT_DIR = Path('/app/data/reports')


def _next_fire() -> datetime:
    now = datetime.now(JST)
    today_candidates = [now.replace(hour=h, minute=0, second=0, microsecond=0)
                        for h in REPORT_HOURS_JST]
    future = [t for t in today_candidates if t > now]
    if future:
        return min(future)
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=min(REPORT_HOURS_JST), minute=0, second=0, microsecond=0)


def _previous_fire(fire_time: datetime) -> datetime:
    candidates = []
    for days_back in (0, 1):
        base = fire_time - timedelta(days=days_back)
        for h in REPORT_HOURS_JST:
            t = base.replace(hour=h, minute=0, second=0, microsecond=0)
            if t < fire_time:
                candidates.append(t)
    return max(candidates) if candidates else fire_time - timedelta(hours=12)


def _build_embed(summary: dict, fire_time: datetime) -> dict:
    total = summary['total_pnl']
    color = 0x2e7d32 if total >= 0 else 0xc62828
    sign = '+' if total >= 0 else ''
    fields = []
    for i, r in enumerate(summary['top3'], 1):
        medal = ['🥇', '🥈', '🥉'][i - 1]
        fields.append({
            'name': f"{medal} {r['strategy']}/{r['symbol']}",
            'value': f"${r['balance']:,.2f} ({r['pnl_pct']:+.2f}%) | "
                     f"{r['closes']}取引 勝率{r['win_rate']:.0f}%",
            'inline': False,
        })
    return {
        'title': f"📊 Hyperliquid Bot レポート [{fire_time.strftime('%m/%d %H:%M')} JST]",
        'description': (
            f"**期間**: {summary['period_label']}\n"
            f"**合計**: ${summary['total_balance']:,.2f} "
            f"({sign}{total:.2f} / {sign}{summary['total_pct']:.2f}%)\n"
            f"**稼働戦略**: {summary['active_strategies']}/{summary['total_strategies']}\n"
            f"① 相場概況 / ② マルチTFチャート / ③ 戦略診断 の3枚構成です。"
        ),
        'color': color,
        'fields': fields,
    }


async def _post(png_paths: list, embed: dict) -> None:
    if not WEBHOOK_URL:
        print('No webhook configured')
        return
    # Discord webhookは1リクエスト最大10ファイル。3枚なので1回でOK。
    form = aiohttp.FormData()
    attachments = [{'id': i, 'filename': Path(p).name} for i, p in enumerate(png_paths)]
    form.add_field('payload_json',
                   json.dumps({'embeds': [embed], 'attachments': attachments}),
                   content_type='application/json')
    for i, p in enumerate(png_paths):
        path = Path(p)
        form.add_field(f'files[{i}]', path.read_bytes(),
                       filename=path.name, content_type='image/png')
    async with aiohttp.ClientSession() as session:
        for attempt in range(3):
            async with session.post(WEBHOOK_URL, data=form) as resp:
                if resp.status in (200, 204):
                    print(f'Posted report: status={resp.status} files={len(png_paths)}')
                    return
                if resp.status == 429:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                body = await resp.text()
                print(f'Discord POST failed: {resp.status} {body[:200]}')
                return


async def _run_once(fire_time: datetime) -> None:
    since = _previous_fire(fire_time)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base_path = OUT_DIR / f"report_{fire_time.strftime('%Y%m%d_%H%M')}.png"
    summary = generate(since, fire_time, str(base_path))
    embed = _build_embed(summary, fire_time)
    await _post(summary['png_paths'], embed)


async def main() -> None:
    print(f'Reporter started. Hours: {REPORT_HOURS_JST} JST')
    while True:
        nxt = _next_fire()
        wait = (nxt - datetime.now(JST)).total_seconds()
        print(f'Next report at {nxt.isoformat()} (in {wait:.0f}s)')
        await asyncio.sleep(max(wait, 5))
        try:
            await _run_once(nxt)
        except Exception as e:
            print(f'Report failed: {e}')
            import traceback
            traceback.print_exc()
        await asyncio.sleep(60)


if __name__ == '__main__':
    asyncio.run(main())
