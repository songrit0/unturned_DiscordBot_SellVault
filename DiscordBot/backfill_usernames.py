"""
Backfill `sv_links.discord_username` / `discord_global_name` for rows that were inserted
by the SellVault plugin (which only knows the discord_id, not the username).

Two entry points share `run_once`:
  - CLI: `python -m backfill_usernames [--dry-run] [--limit N]`
  - bot.py background loop: `discord.ext.tasks.loop(...)` calls `run_once(limit=PER_TICK_LIMIT)`

Discord API:
  GET https://discord.com/api/v10/users/{id}  Authorization: Bot <token>
    200 -> {"username": "...", "global_name": "..."|null, ...}
    404 -> user account is deleted; we write the sentinel '<deleted>' so the row is
           not picked up again.
    429 -> respect Retry-After (seconds, possibly fractional) and retry the same id once;
           a second 429 raises, the caller swallows so the next tick / next CLI run
           can try again.
"""
import argparse
import asyncio
import dataclasses
import logging

import aiohttp

import config
import db


log = logging.getLogger("unturned-bot.backfill")

DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "UnturnedShopBot (backfill, v10)"
REQ_PACING_SEC = 1.0          # 1 req/sec — well under Discord's 50/s global limit
PER_TICK_LIMIT = 100          # bot-loop cap per tick; CLI overrides via --limit
PROGRESS_EVERY = 50           # log a progress line every N examined rows
DELETED_SENTINEL = "<deleted>"


@dataclasses.dataclass
class BackfillStats:
    examined: int = 0
    updated: int = 0
    deleted: int = 0
    rate_limited_hits: int = 0
    errors: int = 0

    def as_log_dict(self, dry_run: bool) -> dict:
        return {
            "examined": self.examined,
            "updated": self.updated,
            "deleted": self.deleted,
            "rate_limited_hits": self.rate_limited_hits,
            "errors": self.errors,
            "dry_run": dry_run,
        }


class _Rate429(Exception):
    """Raised when we hit a second 429 in a row for the same id."""


async def _fetch_user(session: aiohttp.ClientSession, token: str, discord_id: int):
    """
    Returns (status, payload):
      (200, {"username": ..., "global_name": ...})
      (404, None)
    Raises _Rate429 on persistent 429. Raises for other unexpected statuses.
    """
    url = f"{DISCORD_API}/users/{discord_id}"
    headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    for attempt in (1, 2):
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                return 200, data
            if resp.status == 404:
                return 404, None
            if resp.status == 429:
                retry_after = 1.0
                try:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                except (TypeError, ValueError):
                    pass
                log.warning("429 from Discord for id=%s; retry-after=%ss (attempt %d)",
                            discord_id, retry_after, attempt)
                if attempt == 2:
                    raise _Rate429(f"persistent 429 for {discord_id}")
                await asyncio.sleep(retry_after)
                continue
            # 5xx and other unexpected — surface as error
            body = await resp.text()
            raise RuntimeError(f"Discord API {resp.status} for id={discord_id}: {body[:200]}")


async def run_once(token: str, limit: int, dry_run: bool = False,
                   logger: logging.Logger = log) -> BackfillStats:
    """
    Process up to `limit` rows where discord_username IS NULL.

    Idempotent: a re-run picks up where the last one left off because successful
    rows now have a non-NULL value (real username or the '<deleted>' sentinel).
    """
    stats = BackfillStats()
    if not token:
        logger.warning("backfill: no DISCORD_TOKEN configured; skipping")
        return stats
    if limit <= 0:
        return stats

    ids = await asyncio.to_thread(db.select_link_ids_missing_username, limit)
    if not ids:
        return stats

    logger.info("backfill: starting (dry_run=%s, picked=%d, limit=%d)",
                dry_run, len(ids), limit)

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for did in ids:
            stats.examined += 1
            try:
                if dry_run:
                    logger.debug("backfill[dry]: would fetch id=%s", did)
                else:
                    status, payload = await _fetch_user(session, token, did)
                    if status == 200 and payload is not None:
                        username = payload.get("username")
                        global_name = payload.get("global_name")
                        await asyncio.to_thread(db.update_link_username,
                                                did, username, global_name)
                        stats.updated += 1
                    elif status == 404:
                        await asyncio.to_thread(db.update_link_username,
                                                did, DELETED_SENTINEL, None)
                        stats.deleted += 1
            except _Rate429:
                stats.rate_limited_hits += 1
                logger.warning("backfill: hit persistent 429 on id=%s; will retry next pass", did)
                # Don't UPDATE — leave NULL so next pass tries again.
            except Exception:
                stats.errors += 1
                logger.exception("backfill: error on id=%s", did)

            if stats.examined % PROGRESS_EVERY == 0:
                logger.info("backfill: progress %s", stats.as_log_dict(dry_run))
            await asyncio.sleep(REQ_PACING_SEC)

    logger.info("backfill: done %s", stats.as_log_dict(dry_run))
    return stats


async def _run_until_drained(token: str, dry_run: bool, batch: int) -> BackfillStats:
    """CLI helper: keep calling run_once until no rows remain."""
    total = BackfillStats()
    while True:
        stats = await run_once(token, limit=batch, dry_run=dry_run)
        total.examined += stats.examined
        total.updated += stats.updated
        total.deleted += stats.deleted
        total.rate_limited_hits += stats.rate_limited_hits
        total.errors += stats.errors
        if stats.examined == 0:
            break
    return total


def _cli():
    p = argparse.ArgumentParser(description="Backfill sv_links Discord usernames.")
    p.add_argument("--dry-run", action="store_true",
                   help="Log selected rows but don't hit Discord or UPDATE the DB.")
    p.add_argument("--limit", type=int, default=0,
                   help="Max rows to examine in one CLI invocation (0 = all, in batches of --batch).")
    p.add_argument("--batch", type=int, default=500,
                   help="Batch size per SELECT (default 500).")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    remaining = db.count_links_missing_username()
    log.info("backfill: %d rows have discord_username IS NULL", remaining)
    if remaining == 0:
        return

    token = config.DISCORD_TOKEN
    if not token and not args.dry_run:
        raise SystemExit("DISCORD_TOKEN missing in .env (or use --dry-run)")

    if args.limit > 0:
        asyncio.run(run_once(token, limit=args.limit, dry_run=args.dry_run))
    else:
        asyncio.run(_run_until_drained(token, dry_run=args.dry_run, batch=args.batch))


if __name__ == "__main__":
    _cli()
