"""Scheduler for automated news scraping using APScheduler."""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.services.udn_service import UdnScraper
from src.services.ctee_service import CteeScraper
from src.services.yahoo_service import YahooScraper
from src.core.stock_tagger import tag_all_untagged
from src.core.database import get_connection

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def run_scrape_job():
    """Execute full scrape cycle: all sources + stock tagging."""
    logger.info("Scheduled scrape job started")
    results = {}

    for name, scraper_cls in [("udn", UdnScraper), ("ctee", CteeScraper), ("yahoo", YahooScraper)]:
        try:
            scraper = scraper_cls()
            count = scraper.scrape_all() if name != "udn" else scraper.scrape_all(max_per_category=15)
            results[name] = count
            logger.info(f"  {name}: {count} new articles")
        except Exception as e:
            results[name] = 0
            logger.error(f"  {name} failed: {e}")

    # Tag new articles
    tagged = tag_all_untagged()
    logger.info(f"  Tagged: {tagged} articles")

    # Log to scrape_log
    conn = get_connection()
    try:
        for src, count in results.items():
            conn.execute(
                "INSERT INTO scrape_log (source, status, news_count) VALUES (?, 'success', ?)",
                (src, count),
            )
        conn.commit()
    finally:
        conn.close()

    return results


def start_scheduler():
    """Start the background scheduler with cron jobs."""
    # Scrape at 08:00, 12:00, 18:00 daily (Taiwan time)
    scheduler.add_job(
        run_scrape_job,
        CronTrigger(hour="8,12,18", minute=0),
        id="scrape_all",
        name="Scrape all news sources",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started (08:00, 12:00, 18:00 daily)")


def stop_scheduler():
    """Shutdown the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)


def get_scheduler_status():
    """Get current scheduler status and next run times."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
        })
    return {"running": scheduler.running, "jobs": jobs}
