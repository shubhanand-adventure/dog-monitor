"""
Muddy Paws Rescue – new dog monitor.

Loads the Adoptable Dogs page in a headless browser (the dog list is rendered
by JavaScript, so a plain HTTP request won't see it), extracts the listing,
compares it with the last run, and emails you when something changes.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://www.muddypawsrescue.org/adoptable-dogs"
STATE_FILE = Path("state.json")
SNAPSHOT_FILE = Path("snapshot.txt")  # human-readable copy of what was seen

# The dog grid sits between the filter controls and the FAQ on the page.
START_MARKER = "Flex Adoption Candidate"
END_MARKER = "Dog FAQ"

# Filter labels that appear in the listing area but aren't dogs.
IGNORE_LINES = {
    "Age", "Gender", "All Ages", "All Genders", "Male", "Female",
    "2-6 months", "7-12 months", "1-5 years", "6 years+",
    START_MARKER,
}

# Embedded widgets that aren't the dog list. reCAPTCHA (inside the GoFundMe
# donation widget) contains a random token that changes on every page load,
# which caused a false alert on every run.
SKIP_FRAME_KEYWORDS = (
    "recaptcha", "google.com", "gstatic", "gofundme", "doubleclick",
    "facebook", "youtube", "vimeo", "instagram", "tiktok", "stripe",
)

# Real dog listing lines are short. Anything longer is code/tokens, not a dog.
MAX_LINE_LENGTH = 300


def is_ignored_frame(frame):
    url = (frame.url or "").lower()
    return any(k in url for k in SKIP_FRAME_KEYWORDS)


# "true" = email on any change (including dogs being adopted/removed).
# "false" = only email when something new appears.
NOTIFY_ON_REMOVALS = os.getenv("NOTIFY_ON_REMOVALS", "false").lower() == "true"


def fetch_listing():
    """Return (text_lines, links) for the dog listing section."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        )
        page.goto(URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(5_000)  # give the dog widget time to render

        # Scroll down so any lazy-loaded dogs render.
        for _ in range(15):
            page.mouse.wheel(0, 2_500)
            page.wait_for_timeout(400)
        page.wait_for_timeout(2_000)

        body_text = page.inner_text("body")
        start = body_text.find(START_MARKER)
        end = body_text.find(END_MARKER, start if start != -1 else 0)
        if start != -1 and end != -1:
            section = body_text[start:end]
        else:
            print("Warning: page markers not found, tracking whole page text.")
            section = body_text

        # If the dogs live in an embedded iframe, read those too.
        for frame in page.frames[1:]:
            if is_ignored_frame(frame):
                continue
            try:
                section += "\n" + frame.inner_text("body")
            except Exception:
                pass

        # Every link on the page and in frames. Navigation links never change,
        # so a new link almost always means a new dog profile.
        links = set()
        for frame in page.frames:
            if frame is not page.main_frame and is_ignored_frame(frame):
                continue
            try:
                hrefs = frame.eval_on_selector_all(
                    "a[href]", "els => els.map(e => e.href)"
                )
                links.update(h.split("#")[0] for h in hrefs if h.startswith("http"))
            except Exception:
                pass

        browser.close()

    lines = []
    for raw in section.splitlines():
        line = " ".join(raw.split())
        if (
            not line
            or line in IGNORE_LINES
            or line in lines
            or len(line) > MAX_LINE_LENGTH
            or "recaptcha" in line.lower()
        ):
            continue
        lines.append(line)

    links = {l for l in links if not any(k in l.lower() for k in ("recaptcha", "gstatic"))}
    return lines, sorted(links)


def send_email(subject, body):
    sender = os.environ["EMAIL_FROM"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(body)

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as smtp:
        smtp.login(sender, os.environ["EMAIL_PASSWORD"])
        smtp.send_message(msg)
    print(f"Email sent: {subject}")


def main():
    lines, links = fetch_listing()

    # Safety check: if the page failed to load, don't treat it as
    # "every dog was adopted" and then "every dog is new" next time.
    if len(lines) < 3:
        print(f"Only {len(lines)} lines found; page probably didn't load. Skipping.")
        sys.exit(0)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    SNAPSHOT_FILE.write_text(f"Checked {now}\n\n" + "\n".join(lines) + "\n")

    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"lines": lines, "links": links}, indent=2))
        send_email(
            "🐶 Dog monitor is running",
            "Monitoring started. This is what's on the page right now:\n\n"
            + "\n".join(lines)
            + f"\n\n{URL}",
        )
        return

    old = json.loads(STATE_FILE.read_text())
    old_lines, old_links = set(old["lines"]), set(old["links"])

    added_lines = [l for l in lines if l not in old_lines]
    removed_lines = [l for l in old["lines"] if l not in set(lines)]
    added_links = [l for l in links if l not in old_links]

    STATE_FILE.write_text(json.dumps({"lines": lines, "links": links}, indent=2))

    has_new = bool(added_lines or added_links)
    if not has_new and not (NOTIFY_ON_REMOVALS and removed_lines):
        print("No relevant changes.")
        return

    parts = [f"The Adoptable Dogs page changed ({now}).", URL, ""]
    if added_lines:
        parts += ["NEW ON THE PAGE:"] + [f"  + {l}" for l in added_lines] + [""]
    if added_links:
        parts += ["NEW LINKS (likely dog profiles):"] + [f"  {l}" for l in added_links] + [""]
    if removed_lines:
        parts += ["NO LONGER LISTED:"] + [f"  - {l}" for l in removed_lines] + [""]

    subject = "🐶 New dog(s) listed at Muddy Paws!" if has_new else "Muddy Paws page changed"
    send_email(subject, "\n".join(parts))


if __name__ == "__main__":
    main()
