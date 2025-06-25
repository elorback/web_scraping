import re
import time
import requests
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
    ".biz", ".me", ".ai", ".dev", ".online", ".app", ".club", ".uk"
)

visited_lock = Lock()
emails_lock = Lock()
visited = set()
found_emails = set()
robot_parser = None
USER_AGENT = "MyEmailScraperBot"

def setup_robot_parser(base_url):
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    print(robots_url)

    rp = RobotFileParser()
    try:
        headers = {"User-Agent": USER_AGENT}
        res = requests.get(robots_url, headers=headers, timeout=10)
        res.raise_for_status()
        rp.parse(res.text.splitlines())
        rp.set_url(robots_url)
        print(f"[*] Loaded robots.txt from {robots_url}")
        return rp
    except Exception as e:
        print(f"[!] Could not load robots.txt: {e}")
        return None


def is_allowed(url):
    if not robot_parser:
        # If no robots.txt, be cautious and allow
        return True

    return robot_parser.can_fetch(USER_AGENT, url)

def is_xml(content):
    return content.strip().startswith('<?xml')

def extract_urls_from_xml(content):
    soup = BeautifulSoup(content, "xml")
    urls = []

    sitemap_tags = soup.find_all("sitemap")
    if sitemap_tags:
        for sitemap in sitemap_tags:
            loc = sitemap.find("loc")
            if loc and loc.text:
                urls.append(loc.text.strip())
        return urls

    url_tags = soup.find_all("url")
    for url_tag in url_tags:
        loc = url_tag.find("loc")
        if loc and loc.text:
            urls.append(loc.text.strip())

    return urls

def collect_sitemap_links(url, visited_sitemaps=None):
    if visited_sitemaps is None:
        visited_sitemaps = set()
    if url in visited_sitemaps:
        return []
    visited_sitemaps.add(url)
    try:
        res = requests.get(url, timeout=10, headers={"User-Agent": USER_AGENT})
        res.raise_for_status()
        content = res.text
        if is_xml(content):
            urls = extract_urls_from_xml(content)
            # If nested sitemaps, recurse
            if "<sitemapindex" in content:
                all_urls = []
                for sitemap_url in urls:
                    all_urls.extend(collect_sitemap_links(sitemap_url, visited_sitemaps))
                return all_urls
            else:
                return urls
        else:
            print(f"[!] Expected XML sitemap but got non-XML content at {url}")
            return []
    except Exception as e:
        print(f"[!] Failed to load sitemap {url}: {e}")
        return []

def find_sitemap_urls(base_url):
    # Common sitemap paths to try:
    candidates = [
        "/sitemap_index.xml",
        "/sitemap.xml",
        "/sitemap-index.xml",
        "/sitemap/sitemap-index.xml",
        "/sitemap/sitemap.xml"
    ]
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    for path in candidates:
        test_url = base + path
        try:
            res = requests.head(test_url, timeout=5, headers={"User-Agent": USER_AGENT})
            if res.status_code == 200:
                print(f"[*] Found sitemap at {test_url}")
                return test_url
        except:
            pass
    # If none found, fallback to base URL (may or may not work)
    print("[!] No sitemap found at common locations, trying base URL")
    return base_url

def extract_emails(text):
    return set(re.findall(EMAIL_REGEX, text))

def filter_emails_by_tld(emails):
    return {email for email in emails if any(email.lower().endswith(tld) for tld in VALID_EMAIL_TLDS)}

def create_webdriver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument(f"user-agent={USER_AGENT}")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(service=Service(), options=options)

def scrape_page(url, delay, base_netloc, max_pages):
    pairs={}
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
        soup = BeautifulSoup(page_source, "html.parser")
        new_urls = []
        for a in soup.find_all("a", href=True):
            new_url = urljoin(url, a['href'])
            parsed = urlparse(new_url)
            if parsed.scheme in ["http", "https"] and parsed.netloc == base_netloc:
                with visited_lock:
                    if new_url not in visited:
                        new_urls.append(new_url)
        if emails:
            with emails_lock:
                print(url)
                for email in emails:
                    print(email)
                    pairs.update({url: email})
                found_emails.update(emails)
        if len(pairs) != 0:
            print(pairs)
        return new_urls, emails
    except Exception as e:
        print(f"[!] Error scraping {url}: {e}")
        return [], []
    finally:
        driver.quit()

def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} https://example.com")
        sys.exit(1)
    base_url = sys.argv[1].rstrip("/")
    global robot_parser
    robot_parser = setup_robot_parser(base_url)

    sitemap_url = find_sitemap_urls(base_url)
    initial_urls = collect_sitemap_links(sitemap_url)
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
                url = urls_to_visit.pop()
                futures[executor.submit(scrape_page, url, delay, base_netloc, max_pages)] = url
            done, _ = as_completed(futures), None
            for future in done:
                try:
                    new_urls, _ = future.result()
                    for new_url in new_urls:
                        with visited_lock:
                            if new_url not in visited and len(visited) < max_pages:
                                urls_to_visit.add(new_url)
                except Exception as e:
                    print(f"[!] Error in worker thread: {e}")
                futures.pop(future)
                break

    print("\n=== Scraping Complete ===")
    for email in sorted(found_emails):
        print(email)

if __name__ == "__main__":
    main()
