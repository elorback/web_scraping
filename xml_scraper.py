import re
import time
import gzip
import requests
from io import BytesIO
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import sys

EMAIL_REGEX = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
VALID_EMAIL_TLDS = (
    ".com", ".org", ".net", ".edu", ".gov", ".io", ".tech", ".co", ".us", ".info",
    ".biz", ".me", ".ai", ".dev", ".online", ".app", ".club", ".uk", ".design"
)

visited_lock = Lock()
emails_lock = Lock()
visited = set()
found_emails = set()
robot_parsers = {}
USER_AGENT = "MyEmailScraperBot"

def normalize_url(raw_input):
    cleaned = re.sub(r'^[a-zA-Z]+[:/]+', '', raw_input.strip())
    return f"https://{cleaned}"

def extract_sitemaps_from_robots(base_url):
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        res = requests.get(robots_url, timeout=10, headers={"User-Agent": USER_AGENT})
        res.raise_for_status()
        sitemaps = []
        for line in res.text.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemaps.append(line.split(":", 1)[1].strip())
        if sitemaps:
            print(f"[*] Found sitemaps in robots.txt: {sitemaps}")
        return sitemaps
    except Exception as e:
        print(f"[!] Error loading robots.txt: {e}")
        return []

def fetch_sitemap_content(url):
    try:
        print(f"[*] Fetching sitemap: {url}")
        res = requests.get(url, timeout=10, headers={"User-Agent": USER_AGENT})
        res.raise_for_status()
        if url.endswith(".gz"):
            with gzip.open(BytesIO(res.content), "rb") as f:
                return f.read()
        return res.text
    except Exception as e:
        print(f"[!] Failed to load sitemap {url}: {e}")
        return None

def collect_sitemap_links(url, visited_sitemaps=None):
    if visited_sitemaps is None:
        visited_sitemaps = set()
    if url in visited_sitemaps:
        return []
    visited_sitemaps.add(url)

    content = fetch_sitemap_content(url)
    if content is None:
        return []

    try:
        soup = BeautifulSoup(content, "lxml-xml")

        # Handle nested sitemap index
        sitemap_tags = soup.find_all("sitemap")
        if sitemap_tags:
            all_urls = []
            for sitemap in sitemap_tags:
                loc = sitemap.find("loc")
                if loc and loc.text:
                    all_urls.extend(collect_sitemap_links(loc.text.strip(), visited_sitemaps))
            return all_urls

        # Handle actual URL list
        url_tags = soup.find_all("url")
        urls = [loc.find("loc").text.strip() for loc in url_tags if loc.find("loc")]
        print(f"[*] Found {len(urls)} URLs in sitemap: {url}")
        return urls
    except Exception as e:
        print(f"[!] Failed to parse sitemap: {e}")
        return []

def is_allowed(url):
    netloc = urlparse(url).netloc
    if netloc not in robot_parsers:
        rp = RobotFileParser()
        try:
            rp.set_url(f"https://{netloc}/robots.txt")
            rp.read()
            robot_parsers[netloc] = rp
            print(f"[*] Loaded robots.txt from https://{netloc}/robots.txt")
        except Exception as e:
            print(f"[!] Could not load robots.txt for {netloc}: {e}")
            dummy = RobotFileParser()
            dummy.parse("")  # disallow everything
            robot_parsers[netloc] = dummy
    return robot_parsers[netloc].can_fetch(USER_AGENT, url)

def extract_emails(text):
    return set(re.findall(EMAIL_REGEX, text))

def filter_emails_by_tld(emails):
    return {email for email in emails if any(email.lower().endswith(tld) for tld in VALID_EMAIL_TLDS)}

def create_webdriver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument(f"user-agent={USER_AGENT}")
    return webdriver.Chrome(service=Service(), options=options)

def scrape_page(url, delay, base_netloc, max_pages):
    global visited, found_emails
    with visited_lock:
        if url in visited or len(visited) >= max_pages:
            return [], []
        visited.add(url)

    if not is_allowed(url):
        print(f"[!] Disallowed by robots.txt: {url}")
        return [], []

    print(f"[*] Scraping: {url}")
    driver = create_webdriver()
    try:
        driver.get(url)
        time.sleep(delay)
        page_source = driver.page_source
        emails = filter_emails_by_tld(extract_emails(page_source))

        new_urls = []
        soup = BeautifulSoup(page_source, "html.parser")
        for a in soup.find_all("a", href=True):
            new_url = urljoin(url, a['href'])
            parsed = urlparse(new_url)
            if parsed.scheme in ["http", "https"] and parsed.netloc == base_netloc:
                with visited_lock:
                    if new_url not in visited:
                        new_urls.append(new_url)

        if emails:
            with emails_lock:
                found_emails.update(emails)
                print({url: list(emails)})

        return new_urls, emails
    except Exception as e:
        print(f"[!] Error scraping {url}: {e}")
        return [], []
    finally:
        driver.quit()

def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} https://example.com or example.com")
        sys.exit(1)

    base_url = normalize_url(sys.argv[1])
    print(f"[+] Normalized URL: {base_url}")

    sitemaps = extract_sitemaps_from_robots(base_url)
    if not sitemaps:
        sitemaps = [base_url.rstrip("/") + "/sitemap.xml"]

    initial_urls = []
    for sitemap in sitemaps:
        initial_urls.extend(collect_sitemap_links(sitemap))

    if not initial_urls:
        print("[!] No URLs found in sitemap, exiting.")
        sys.exit(1)

    base_netloc = urlparse(base_url).netloc
    urls_to_visit = set(initial_urls)
    max_pages = 100
    delay = 2.0
    max_workers = 10

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = dict()
        while urls_to_visit and len(visited) < max_pages:
            while urls_to_visit and len(futures) < max_workers and len(visited) < max_pages:
                next_url = urls_to_visit.pop()
                futures[executor.submit(scrape_page, next_url, delay, base_netloc, max_pages)] = next_url

            for future in list(as_completed(futures)):
                try:
                    new_urls, _ = future.result()
                    with visited_lock:
                        for new_url in new_urls:
                            if new_url not in visited and len(visited) < max_pages:
                                urls_to_visit.add(new_url)
                except Exception as e:
                    print(f"[!] Error in thread: {e}")
                finally:
                    futures.pop(future)

    print("\n=== Scraping Complete ===")
    for email in sorted(found_emails):
        print(email)

if __name__ == "__main__":
    main()
