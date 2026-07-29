"""DDNS sidecar: keep the configured record pointed at this network.

Deliberately conservative — it only calls the provider when the detected
address actually differs from the last one we published. DNS APIs are rate
limited, and a loop that rewrites an unchanged record is how people get their
tokens throttled.
"""

import asyncio
from datetime import UTC, datetime

import structlog

from framefound.db.engine import session_factory
from framefound.ddns import settings_store as store
from framefound.ddns.providers import DnsError, DnsRecord, build_provider, detect_public_ip
from framefound.logging import configure_logging

log = structlog.get_logger()

IDLE_POLL_SECONDS = 60.0


async def run_once() -> None:
    factory = session_factory()
    async with factory() as db:
        config = await store.load_config(db)
        if not (config.public_access_enabled and config.ddns_configured):
            return
        state = await store.load_state(db)
        now = datetime.now(UTC).isoformat()
        state.last_checked_at = now

        try:
            provider = build_provider(config.ddns_provider, config.token(), config.ddns_zone)
            changed = False
            for family, enabled, record_type, previous in (
                ("ipv4", config.ddns_ipv4, "A", state.last_ipv4),
                ("ipv6", config.ddns_ipv6, "AAAA", state.last_ipv6),
            ):
                if not enabled:
                    continue
                address = await detect_public_ip(ipv6=family == "ipv6")
                if address is None:
                    log.warning("ddns.ip_undetectable", family=family)
                    continue
                if address == previous:
                    continue  # nothing to publish
                await provider.upsert(
                    DnsRecord(
                        name=config.ddns_record or config.domain,
                        ip=address,
                        record_type=record_type,
                        proxied=config.ddns_proxied,
                    )
                )
                if family == "ipv4":
                    state.last_ipv4 = address
                else:
                    state.last_ipv6 = address
                state.history.append(f"{now} {record_type} -> {address}")
                changed = True

            if changed:
                state.last_updated_at = now
            state.last_error = ""
            state.consecutive_failures = 0
        except DnsError as err:
            state.last_error = str(err)
            state.consecutive_failures += 1
            log.warning("ddns.update_failed", reason=str(err))
        except Exception as err:  # never let the sidecar die
            state.last_error = "Unexpected error while updating DNS"
            state.consecutive_failures += 1
            log.error("ddns.unexpected", exc_info=err)

        await store.save_state(db, state)


async def main() -> None:
    configure_logging()
    log.info("ddns.started")
    while True:
        interval = IDLE_POLL_SECONDS
        try:
            await run_once()
            factory = session_factory()
            async with factory() as db:
                config = await store.load_config(db)
                if config.public_access_enabled and config.ddns_configured:
                    interval = max(60, config.ddns_interval_minutes * 60)
        except Exception:
            log.error("ddns.loop_error", exc_info=True)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
