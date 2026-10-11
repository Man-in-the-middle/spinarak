#!/usr/bin/env python3
"""Pokemon Cafe availability watcher for GitHub Actions (Spinarak-style).

Architecture copied from the free Spinarak bot (MIT license):
  - runs on GitHub Actions: fresh rotating runner IPs instead of one
    datacenter proxy IP that the site's AWS WAF has flagged
  - headful Chrome via virtual display + Selenium (harder to fingerprint
    than headless)
  - on a newly opened slot: emails you the booking link + screenshot;
    you complete the booking manually (Spinarak never auto-books either)

Adapted for Leo: Tokyo + Osaka, party of 3, every day Nov 29 - Dec 8, 2026,
and the entry/calendar selectors verified against the live site in Oct 2026.

Secrets (repo Settings > Secrets and variables > Actions):
  GMAIL_SENDER     Gmail address that sends the alert
  GMAIL_APP_PW     Gmail app password (myaccount.google.com/apppasswords)
  GMAIL_RECIPIENT  where alerts go (e.g. your hotmail)
"""
import json
import os
import random
import re
import smtplib
import sys
import time
import traceback
from datetime import datetime

from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import chromedriver_autoinstaller
from pyvirtualdisplay import Display
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
HITS_DIR = os.path.join(HERE, "hits")

GUESTS = 3
SENDER = os.environ.get("GMAIL_SENDER", "")
APP_PW = os.environ.get("GMAIL_APP_PW", "")
RECIPIENT = os.environ.get("GMAIL_RECIPIENT", SENDER)

ENTRY = {
    "tokyo": "https://reserve.pokemon-cafe.jp/",
    "osaka": "https://osaka.pokemon-cafe.jp/",
}
STEP1 = {
    "tokyo": "https://reserve.pokemon-cafe.jp/reserve/step1",
    "osaka": "https://osaka.pokemon-cafe.jp/reserve/step1",
}


def targets():
    """Simple scope: both stores, every day Nov 29 - Dec 8, 2026 (inclusive).
    Unreleased dates just report 'unreleased'."""
    dates = ["2026-11-%02d" % d for d in (29, 30)]
    dates += ["2026-12-%02d" % d for d in range(1, 9)]
    return [(store, d) for store in ("tokyo", "osaka") for d in dates]


def log(msg):
    print("[watch] %s" % msg, flush=True)


def sleep(a=2, b=5):
    time.sleep(random.randint(a, b))


def safe_text(el):
    try:
        return (el.text or "").strip()
    except Exception:
        return ""


def is_challenge(driver):
    try:
        return ("auth_confirm" in driver.current_url
                or "not a robot" in driver.page_source.lower())
    except Exception:
        return False


def make_driver():
    display = Display(visible=0, size=(1366, 900))
    display.start()
    chromedriver_autoinstaller.install()
    opts = webdriver.ChromeOptions()
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    return driver, display


def load(driver, url):
    try:
        driver.get(url)
        return True
    except Exception as e:
        log("page load failed (%s): %s" % (url, e))
        return False


def enter_store(driver, store):
    """Entry page -> consent -> reservation flow -> step1 guest page."""
    if not load(driver, ENTRY[store]):
        return False
    sleep()
    if is_challenge(driver):
        log("WAF challenge on %s entry page" % store)
        return False
    for how, val in [(By.CLASS_NAME, "agreeChecked"),
                     (By.CSS_SELECTOR, "input[type=checkbox]")]:
        try:
            cb = driver.find_element(how, val)
            if not cb.is_selected():
                cb.click()
                log("consent checked")
            break
        except NoSuchElementException:
            continue
    sleep()
    clicked = False
    for a in driver.find_elements(By.TAG_NAME, "a"):
        if "reserve/step" in (a.get_attribute("href") or ""):
            try:
                a.click()
                clicked = True
                log("clicked entry link")
                break
            except Exception:
                continue
    if not clicked:
        for el in driver.find_elements(
            By.XPATH,
            "//*[contains(@class,'button') and (contains(text(),'予約する')"
            " or contains(text(),'予約へ') or contains(text(),'Make a Reservation')"
            " or contains(text(),'Reserve'))]",
        ):
            try:
                el.click()
                clicked = True
                log("clicked entry button")
                break
            except Exception:
                continue
    sleep(3, 6)
    if is_challenge(driver):
        log("WAF challenge after entry")
        return False
    if not load(driver, STEP1[store]):
        return False
    sleep()
    if is_challenge(driver):
        log("WAF challenge on step1")
        return False
    src = driver.page_source
    if "Number of Guests" in src or 'name="guest"' in src:
        log("reached guest page (%s)" % store)
        return True
    log("guest page not detected (%s)" % store)
    return False


def select_guests(driver):
    for how, val in [(By.NAME, "guest"), (By.ID, "guest"),
                     (By.NAME, "guest_num"),
                     (By.XPATH, "//select[contains(@name,'guest')]")]:
        try:
            sel = Select(driver.find_element(how, val))
            try:
                sel.select_by_value(str(GUESTS))
            except NoSuchElementException:
                sel.select_by_index(GUESTS)  # index 0 = placeholder
            log("guests set to %d" % GUESTS)
            sleep()
            return
        except NoSuchElementException:
            continue
    log("WARNING: guest selector not found; continuing")


def parse_calendar(driver):
    days, label = [], ""
    try:
        label = safe_text(driver.find_element(
            By.XPATH, "//*[contains(text(),'年') and contains(text(),'月')]"))
    except NoSuchElementException:
        pass
    cells = driver.find_elements(By.CSS_SELECTOR, "li.calendar-day-cell")
    if not cells:
        cells = driver.find_elements(
            By.XPATH, "//li[contains(@class,'calendar-day-cell')]")
    for c in cells:
        txt = safe_text(c)
        m = re.search(r"(\d{1,2})", txt)
        if not m:
            continue
        full = ("満席" in txt) or ("(Full)" in txt) or ("full" in txt.lower())
        days.append({"day": int(m.group(1)), "available": not full})
    return label, days


def goto_month(driver, year, month, max_clicks=3):
    for _ in range(max_clicks):
        label, _ = parse_calendar(driver)
        m = re.search(r"(\d{4}).*?(\d{1,2})\s*月", label)
        if m and int(m.group(1)) == year and int(m.group(2)) == month:
            return True
        clicked = False
        for how, val in [(By.XPATH, "//*[contains(text(),'次の月を見る')]"),
                         (By.CLASS_NAME, "calendar-pager")]:
            try:
                driver.find_element(how, val).click()
                clicked = True
                break
            except NoSuchElementException:
                continue
        if not clicked:
            log("next-month pager not found")
            return False
        sleep()
    label, _ = parse_calendar(driver)
    m = re.search(r"(\d{4}).*?(\d{1,2})\s*月", label)
    return bool(m and int(m.group(1)) == year and int(m.group(2)) == month)


def check_store(driver, store, dates):
    out = {}
    if not enter_store(driver, store):
        return {d: "error" for d in dates}
    select_guests(driver)
    months = sorted({(int(d[:4]), int(d[5:7])) for d in dates})
    for year, month in months:
        md = [d for d in dates if d.startswith("%d-%02d" % (year, month))]
        if not goto_month(driver, year, month):
            for d in md:
                out[d] = "unreleased"
            continue
        _, days = parse_calendar(driver)
        by_day = {x["day"]: x for x in days}
        for d in md:
            cell = by_day.get(int(d[8:10]))
            if cell is None:
                out[d] = "unreleased"
            elif cell["available"]:
                out[d] = "available"
            else:
                out[d] = "full"
    return out


def send_alert(hits, shot_path):
    msg = MIMEMultipart()
    msg["Subject"] = "🎫 Pokemon Cafe opening: " + ", ".join(
        "%s %s" % h for h in hits)
    msg["From"] = SENDER
    msg["To"] = RECIPIENT
    lines = ["Pokemon Cafe has tables open right now:", ""]
    for store, d in hits:
        lines.append("- %s %s -> book now: %s" % (store.title(), d, STEP1[store]))
    lines += ["", "These go fast — book immediately."]
    msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))
    if shot_path and os.path.exists(shot_path):
        with open(shot_path, "rb") as f:
            msg.attach(MIMEImage(f.read(), name=os.path.basename(shot_path)))
    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(SENDER, APP_PW)
    server.sendmail(SENDER, [RECIPIENT], msg.as_string())
    server.quit()
    log("alert email sent to %s" % RECIPIENT)


def main():
    try:
        prev = json.load(open(STATE_PATH)).get("slots", {})
    except Exception:
        prev = {}
    os.makedirs(HITS_DIR, exist_ok=True)
    log("targets: %s" % targets())
    driver, display = make_driver()
    slots = {}
    try:
        for store in ("tokyo", "osaka"):
            dates = [d for s, d in targets() if s == store]
            if not dates:
                continue
            log("checking %s: %s" % (store, dates))
            for d, st in check_store(driver, store, dates).items():
                slots["%s|%s" % (store, d)] = st
                log("  %s -> %s" % (d, st))
    except Exception:
        log("run failed:\n%s" % traceback.format_exc(limit=5))
    hits = [(k.split("|")[0], k.split("|")[1]) for k, v in slots.items()
            if v == "available" and prev.get(k) != "available"]
    shot_path = None
    if hits:
        shot_path = os.path.join(
            HITS_DIR, "hit-%s.png" % datetime.utcnow().strftime("%Y%m%d-%H%M%S"))
        try:
            driver.save_screenshot(shot_path)
            log("screenshot: %s" % shot_path)
        except Exception as e:
            log("screenshot failed: %s" % e)
            shot_path = None
    try:
        driver.quit()
    except Exception:
        pass
    try:
        display.stop()
    except Exception:
        pass
    if hits:
        if SENDER and APP_PW:
            try:
                send_alert(hits, shot_path)
            except Exception as e:
                log("email failed: %s" % e)
        else:
            log("hits found but Gmail secrets not set; skipping email")
    json.dump({"slots": slots,
               "checked_at": datetime.utcnow().isoformat() + "Z"},
              open(STATE_PATH, "w"), indent=2)
    print(json.dumps({"newly_available": hits, "slots": slots}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
