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

#email regex patters
EMAIL_REGEX = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
#valid email suffixes and filters out junk
VALID_EMAIL_TLDS = (".com", ".org", ".net", ".edu", ".gov", ".io", ".tech", ".co", ".us", ".info", ".biz", ".me", ".ai", ".dev", ".online", ".app", ".club", ".uk",'design')

#shared state
visited_lock = Lock()
emails_lock = Lock()
visited = set()
found_emails = set()
robot_parsers = {}
USER_AGENT = "MyEmailScraperBot"

#loads and parses robots.txt rules
def setup_robot_parser(base_url):
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    print(robots_url)
    try:
        rp.set_url(robots_url)
        rp.read()
        print(f"[*] Loaded robots.txt from {robots_url}")
        return rp
    except Exception as e:
        print(f"[!] Could not load robots.txt: {e}")
        return None
# checks rules for robots.txt
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
            dummy.parse("")  # block everything
            robot_parsers[netloc] = dummy
    return robot_parsers[netloc].can_fetch(USER_AGENT, url)

#checks if xml
def is_xml(content):
    return content.strip().startswith('<?xml')

#extracts urls from xml
def extract_urls_from_xml(content):
    soup = BeautifulSoup(content, "xml")
    return [loc.text.strip() for loc in soup.find_all("loc")]

#collects links from urls
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

        # Parse as XML with BeautifulSoup
        soup = BeautifulSoup(content, "xml")

        # If it's a sitemap index (contains <sitemap> tags)
        sitemap_tags = soup.find_all("sitemap")
        if sitemap_tags:
            all_urls = []
            for sitemap in sitemap_tags:
                loc = sitemap.find("loc")
                if loc:
                    sitemap_url = loc.text.strip()
                    all_urls.extend(collect_sitemap_links(sitemap_url, visited_sitemaps))
            return all_urls
        
        # Otherwise treat as normal sitemap with <url> entries
        url_tags = soup.find_all("url")
        urls = []
        for url_tag in url_tags:
            loc = url_tag.find("loc")
            if loc and loc.text:
                urls.append(loc.text.strip())
        return urls

    except Exception as e:
        print(f"[!] Failed to load sitemap {url}: {e}")
        return []

#extracts emails from text
def extract_emails(text):
    return set(re.findall(EMAIL_REGEX, text))

#filters emails by top level domain
def filter_emails_by_tld(emails):
    return {email for email in emails if any(email.lower().endswith(tld) for tld in VALID_EMAIL_TLDS)}

#creates chrome driver
def create_webdriver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument(f"user-agent={USER_AGENT}")
    return webdriver.Chrome(service=Service(), options=options)

#core logic for scraping page
def scrape_page(url, delay, base_netloc, max_pages):
    global visited, found_emails
    pairs={}
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
                found_emails.update(emails)
                pairs.update({url: emails})
        if len(pairs) != 0:
            print(pairs)
        return new_urls, emails
    except Exception as e:
        print(f"[!] Error scraping {url}: {e}")
        return [], []
    finally:
        driver.quit()
        
#core logic
def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} https://example.com")
        sys.exit(1)
    raw_input=sys.argv[1]
    cleaned_url = re.sub(r'^(.*?:/)?', '', raw_input)
    url='https://'
    if url not in sys.argv[1]:
        url += cleaned_url
        
    print(url)
    base_url = url.rstrip("/")
    sitemap_url = base_url + "/sitemap.xml"
    global robot_parser
    robot_parser = setup_robot_parser(base_url)
    initial_urls = collect_sitemap_links(sitemap_url)
    if not initial_urls:
        print("[!] No URLs found in sitemap.xml, exiting.")
        sys.exit(1)
    base_netloc = urlparse(base_url).netloc
    urls_to_visit = set(initial_urls)
    max_pages = 100
    delay = 2.0
    max_workers = 10
    
    
    #creates threads to traverse the sitemap more efficiently
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
