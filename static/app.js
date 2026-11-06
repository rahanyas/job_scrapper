const form = document.querySelector("#search-form");
const input = document.querySelector("#query");
const locationInput = document.querySelector("#location");
const experienceSelect = document.querySelector("#experience");
const results = document.querySelector("#results");
const resultCount = document.querySelector("#result-count");
const resultsHeading = document.querySelector("#results-heading");
const submitButton = form.querySelector('button[type="submit"]');
let currentSearch = null;

function showMessage(title, message) {
  const state = document.createElement("div");
  state.className = "empty-state";

  const heading = document.createElement("h3");
  heading.textContent = title;
  const description = document.createElement("p");
  description.textContent = message;
  state.append(heading, description);
  results.replaceChildren(state);
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
}

function createTag(text, className = "") {
  const tag = document.createElement("span");
  tag.className = `job-tag ${className}`.trim();
  tag.textContent = text;
  return tag;
}

function createJobCard(job) {
  const card = document.createElement("article");
  card.className = "job-card";

  const avatar = document.createElement("div");
  avatar.className = "company-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = (job.company || "?").trim().charAt(0) || "?";

  const main = document.createElement("div");
  main.className = "job-main";
  const title = document.createElement("h3");
  title.className = "job-title";
  title.textContent = job.title;
  const company = document.createElement("p");
  company.className = "job-company";
  company.textContent = job.company;
  main.append(title, company);

  const meta = document.createElement("div");
  meta.className = "job-meta";
  meta.append(createTag(job.location || "Remote", "location"));
  if (job.source) meta.append(createTag(job.source, "source-tag"));
  if (job.experience_required === null) {
    meta.append(createTag("Experience not specified"));
  } else if (job.experience_required === 0) {
    meta.append(createTag("Entry level"));
  } else {
    meta.append(createTag(`${job.experience_required}+ years`));
  }
  if (job.job_type) meta.append(createTag(job.job_type.replaceAll("_", " ")));
  if (job.category) meta.append(createTag(job.category));
  if (job.salary) meta.append(createTag(job.salary));

  const actions = document.createElement("div");
  actions.className = "job-actions";
  const link = document.createElement("a");
  link.className = "apply-link";
  link.href = job.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.append(document.createTextNode("Apply now"));

  const arrow = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  arrow.setAttribute("viewBox", "0 0 24 24");
  arrow.setAttribute("aria-hidden", "true");
  const arrowPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
  arrowPath.setAttribute("d", "M7 17 17 7M7 7h10v10");
  arrow.append(arrowPath);
  link.append(arrow);
  actions.append(link);

  for (const email of job.emails || []) {
    const contactLink = document.createElement("a");
    contactLink.className = "contact-link";
    contactLink.href = `mailto:${email}`;
    contactLink.textContent = email;
    actions.append(contactLink);
  }

  const dateText = formatDate(job.publication_date);
  if (dateText) {
    const date = document.createElement("span");
    date.className = "posted-date";
    date.textContent = `Posted ${dateText}`;
    actions.append(date);
  }

  card.append(avatar, main, meta, actions);
  return card;
}

function appendSourceWarning(sourceErrors, container) {
  if (!sourceErrors || sourceErrors.length === 0) return;
  const warning = document.createElement("p");
  warning.className = "search-hint source-warning";
  const unavailable = sourceErrors.map((item) => item.source).join(", ");
  warning.textContent = `Some job boards could not be reached (${unavailable}). Results may be incomplete.`;
  container.append(warning);
}

function updateResultsHeading() {
  const filters = [
    currentSearch.location,
    currentSearch.experience === "fresher"
      ? "Fresher"
      : currentSearch.experience
        ? `${currentSearch.experience}+ years`
        : "",
  ].filter(Boolean);
  resultsHeading.textContent = `Results for “${currentSearch.query}”${filters.length ? ` · ${filters.join(" · ")}` : ""}`;
}

function appendPagination(data, container) {
  if (data.total_pages <= 1) return;

  const pagination = document.createElement("nav");
  pagination.className = "pagination";
  pagination.setAttribute("aria-label", "Job results pages");

  const previous = document.createElement("button");
  previous.type = "button";
  previous.className = "page-button";
  previous.textContent = "Previous";
  previous.disabled = !data.has_previous;
  previous.dataset.page = String(data.page - 1);

  const pageLabel = document.createElement("span");
  pageLabel.className = "page-label";
  pageLabel.textContent = `Page ${data.page} of ${data.total_pages}`;
  pageLabel.setAttribute("aria-live", "polite");

  const next = document.createElement("button");
  next.type = "button";
  next.className = "page-button";
  next.textContent = "Next";
  next.disabled = !data.has_next;
  next.dataset.page = String(data.page + 1);

  pagination.append(previous, pageLabel, next);
  container.append(pagination);
}

async function loadResults(page) {
  if (!currentSearch) return;
  submitButton.disabled = true;
  submitButton.querySelector("span").textContent = "Searching...";
  resultCount.textContent = "Loading listings...";
  updateResultsHeading();
  showMessage("Searching jobs", "Looking for matching remote opportunities...");

  try {
    const searchParams = new URLSearchParams({
      q: currentSearch.query,
      page: String(page),
    });
    if (currentSearch.location) searchParams.set("location", currentSearch.location);
    if (currentSearch.experience) searchParams.set("experience", currentSearch.experience);
    const response = await fetch(`/api/jobs?${searchParams}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load job listings.");

    resultCount.textContent = `${data.total} ${data.total === 1 ? "job" : "jobs"} found`;
    if (data.jobs.length === 0) {
      showMessage("No matching jobs yet", "Try a different title, skill, or keyword.");
      appendSourceWarning(data.source_errors, results);
      return;
    }

    const fragment = document.createDocumentFragment();
    data.jobs.forEach((job) => fragment.append(createJobCard(job)));
    appendPagination(data, fragment);
    appendSourceWarning(data.source_errors, fragment);
    results.replaceChildren(fragment);
  } catch (error) {
    resultCount.textContent = "";
    showMessage("Couldn’t load jobs", error.message);
    appendSourceWarning([], results);
  } finally {
    submitButton.disabled = false;
    submitButton.querySelector("span").textContent = "Search jobs";
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = input.value.trim();
  if (!query) {
    input.focus();
    return;
  }
  currentSearch = {
    query,
    location: locationInput.value.trim(),
    experience: experienceSelect.value,
  };
  await loadResults(1);
});

results.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-page]");
  if (!button || button.disabled) return;
  await loadResults(Number(button.dataset.page));
});
