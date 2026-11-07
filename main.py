import argparse
from datetime import date, datetime, timezone
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
import re
from pathlib import Path
import threading
import time
import urllib.parse

from requests.exceptions import RequestException


BASE_DIR = Path(__file__).resolve().parent
CACHE_SECONDS = 15 * 60
MAX_QUERY_LENGTH = 200
MAX_LOCATION_LENGTH = 100
PAGE_SIZE = 10
RESULTS_PER_SITE = 30
MAX_PAGE = 100
EXPERIENCE_LEVELS = {"", "fresher", "1", "2", "3"}
JOB_SITES = ["indeed", "linkedin", "glassdoor", "zip_recruiter"]

_cache_lock = threading.Lock()
_search_cache = {}


def clean_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return ", ".join(filter(None, (clean_value(item) for item in value)))
    text = str(value).strip()
    return "" if text in {"", "nan", "NaT", "<NA>"} else text


def minimum_required_experience(description):
    years = re.findall(
        r"\b(\d+(?:\.\d+)?)\s*(?:\+|[-–]\s*\d+(?:\.\d+)?)?\s*(?:years?|yrs?)\b",
        description,
        re.IGNORECASE,
    )
    months = re.findall(
        r"\b(\d+(?:\.\d+)?)\s*(?:\+|[-–]\s*\d+(?:\.\d+)?)?\s*months?\b",
        description,
        re.IGNORECASE,
    )
    requirements = [float(value) for value in years]
    requirements.extend(float(value) / 12 for value in months)
    if re.search(
        r"\b(?:no|zero)\s+(?:prior\s+)?experience\b|\bentry[- ]level\b",
        description,
        re.IGNORECASE,
    ):
        requirements.append(0)
    return min(requirements) if requirements else None


def normalize_email(value):
    candidates = value if isinstance(value, (list, tuple, set)) else [value]
    emails = []
    for candidate in candidates:
        text = clean_value(candidate)
        for email in re.findall(
            r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
            text,
            flags=re.IGNORECASE,
        ):
            address = parseaddr(email)[1].lower()
            if address and address not in emails:
                emails.append(address)
    return emails


def normalize_job(record):
    listing_url = clean_value(record.get("job_url"))
    direct_url = clean_value(record.get("job_url_direct"))
    url = next(
        (
            candidate
            for candidate in (direct_url, listing_url)
            if urllib.parse.urlparse(candidate).scheme == "https"
            and urllib.parse.urlparse(candidate).hostname
        ),
        "",
    )
    if not url:
        return None

    title = clean_value(record.get("title")) or "Untitled role"
    company = clean_value(record.get("company")) or "Company not listed"
    location = clean_value(record.get("location")) or "Not specified"
    description = clean_value(record.get("description"))
    experience_range = clean_value(record.get("experience_range"))
    experience_text = " ".join(
        filter(None, (description, experience_range, clean_value(record.get("job_level"))))
    )
    minimum_experience = minimum_required_experience(experience_text)
    if experience_range and minimum_experience is None:
        experience_match = re.search(r"\d+(?:\.\d+)?", experience_range)
        if experience_match:
            minimum_experience = float(experience_match.group())

    salary_parts = [
        clean_value(record.get("currency")),
        clean_value(record.get("min_amount")),
        clean_value(record.get("max_amount")),
        clean_value(record.get("interval")),
    ]
    salary = ""
    if salary_parts[1] or salary_parts[2]:
        amount = " - ".join(part for part in salary_parts[1:3] if part)
        salary = " ".join(part for part in (salary_parts[0], amount, salary_parts[3]) if part)

    source = clean_value(record.get("site")).replace("_", " ").title()
    return {
        "title": title,
        "company": company,
        "location": location,
        "category": clean_value(record.get("job_function")),
        "job_type": clean_value(record.get("job_type")),
        "salary": salary,
        "publication_date": clean_value(record.get("date_posted")),
        "url": url,
        "source": source or "Job board",
        "experience_required": minimum_experience,
        "emails": normalize_email(record.get("emails")),
        "_description": description,
    }


def scrape_jobs(query, location):
    try:
        from jobspy import scrape_jobs as jobspy_scrape_jobs
    except ImportError as error:
        raise RuntimeError(
            "Job search dependency is missing. Install it with: pip install -r requirements.txt"
        ) from error

    try:
        results = jobspy_scrape_jobs(
            site_name=JOB_SITES,
            search_term=query,
            location=location or None,
            results_wanted=RESULTS_PER_SITE,
            description_format="plain",
            fetch_description=False,
            verbose=0,
        )
    except (OSError, TimeoutError, ValueError, RequestException) as error:
        raise RuntimeError(f"Job-board search failed: {error}") from error
    if results is None:
        raise RuntimeError("The job search sources returned no response.")

    errors = results.attrs.get("errors", {})
    source_errors = [
        {"source": str(source).replace("_", " ").title(), "message": str(message)}
        for source, message in errors.items()
    ] if isinstance(errors, dict) else []
    records = results.to_dict(orient="records")
    jobs = []
    seen = set()
    for record in records:
        job = normalize_job(record)
        if not job:
            continue
        key = job["url"].casefold()
        if key in seen:
            continue
        seen.add(key)
        jobs.append(job)

    return jobs, source_errors


def get_search_snapshot(query, location):
    key = (query.casefold(), location.casefold())
    now = time.monotonic()
    with _cache_lock:
        cached = _search_cache.get(key)
        if cached and now < cached["expires_at"]:
            return cached["jobs"], cached["source_errors"]

        jobs, source_errors = scrape_jobs(query, location)
        if not jobs and source_errors:
            raise RuntimeError("All job-board searches failed.")
        _search_cache[key] = {
            "jobs": jobs,
            "source_errors": source_errors,
            "expires_at": now + CACHE_SECONDS,
        }
        expired_keys = [
            cache_key
            for cache_key, entry in _search_cache.items()
            if now >= entry["expires_at"]
        ]
        for expired_key in expired_keys:
            del _search_cache[expired_key]
        return jobs, source_errors


def search_jobs(jobs, query, experience):
    terms = list(dict.fromkeys(re.findall(r"[\w+#.-]+", query.casefold())))
    matches = []
    for job in jobs:
        if experience and job.get("experience_required") is not None:
            candidate_years = 0 if experience == "fresher" else int(experience)
            if job["experience_required"] > candidate_years:
                continue

        title = job["title"].casefold()
        searchable = " ".join(
            (
                title,
                job["company"].casefold(),
                job["location"].casefold(),
                job["category"].casefold(),
                job["job_type"].casefold(),
                job["_description"].casefold(),
            )
        )
        matched_terms = [term for term in terms if term in searchable]
        if not matched_terms:
            continue
        score = sum(3 if term in title else 1 for term in matched_terms)
        matches.append((score, job))

    matches.sort(
        key=lambda item: (
            item[0],
            item[1].get("publication_date", ""),
        ),
        reverse=True,
    )
    return [
        {key: value for key, value in job.items() if not key.startswith("_")}
        for _, job in matches
    ]


class JobSearchHandler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path == "/api/jobs":
            self.handle_job_search(parsed_url.query)
            return

        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        file_info = static_files.get(parsed_url.path)
        if file_info is None:
            self.send_error(404, "Not found")
            return
        filename, content_type = file_info
        body = (BASE_DIR / "static" / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def handle_job_search(self, raw_query):
        parameters = urllib.parse.parse_qs(raw_query)
        query = parameters.get("q", [""])[0].strip()
        location = parameters.get("location", [""])[0].strip()
        experience = parameters.get("experience", [""])[0].strip().casefold()
        try:
            page = int(parameters.get("page", ["1"])[0])
        except ValueError:
            self.send_json(400, {"error": "Page must be a positive number."})
            return

        if not query:
            self.send_json(400, {"error": "Enter a job title or keyword to search."})
            return
        if len(query) > MAX_QUERY_LENGTH:
            self.send_json(400, {"error": f"Search queries must be {MAX_QUERY_LENGTH} characters or fewer."})
            return
        if len(location) > MAX_LOCATION_LENGTH:
            self.send_json(400, {"error": f"Location must be {MAX_LOCATION_LENGTH} characters or fewer."})
            return
        if experience not in EXPERIENCE_LEVELS:
            self.send_json(400, {"error": "Choose a valid experience level."})
            return
        if page < 1 or page > MAX_PAGE:
            self.send_json(400, {"error": f"Page must be between 1 and {MAX_PAGE}."})
            return

        try:
            snapshot, source_errors = get_search_snapshot(query, location)
        except RuntimeError as error:
            logging.getLogger(__name__).error("Job-board search failed: %s", error)
            self.send_json(
                502,
                {"error": "Job boards are temporarily unavailable. Please try again later."},
            )
            return

        jobs = search_jobs(snapshot, query, experience)
        total = len(jobs)
        total_pages = math.ceil(total / PAGE_SIZE)
        start = (page - 1) * PAGE_SIZE
        self.send_json(
            200,
            {
                "query": query,
                "filters": {"location": location, "experience": experience},
                "total": total,
                "page": page,
                "page_size": PAGE_SIZE,
                "total_pages": total_pages,
                "has_previous": page > 1,
                "has_next": page < total_pages,
                "jobs": jobs[start:start + PAGE_SIZE],
                "source_errors": source_errors,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def log_message(self, format_string, *args):
        super().log_message(format_string, *args)


def main():
    parser = argparse.ArgumentParser(
        description="Run a job search website powered by Indeed, LinkedIn, Glassdoor, and ZipRecruiter."
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="server host")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="server port")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), JobSearchHandler)
    print(f"Job search is running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping job search server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
