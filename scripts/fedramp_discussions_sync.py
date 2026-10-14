#!/usr/bin/env python3
"""
Sync FedRAMP/community GitHub Discussions into a machine-readable knowledge base.

Outputs (under --out):
  records/<number>.json        normalized thread + extracted entities (+ optional LLM summary)
  markdown/<number>.md         same thread as Markdown with YAML frontmatter (Obsidian / RAG friendly)
  index.jsonl                  one compact line per discussion, rebuilt every run
  chunks.jsonl                 one line per post / top-level comment thread (best unit for embeddings,
                               especially for megathreads like #146 / #147 with 60-100+ comments)
  changes/<YYYY-MM-DD>.jsonl   what was new / updated / removed in each run
  state.sqlite                 sync state (content hashes, summary hashes, high-water mark)

Env (or --env-file):
  GITHUB_TOKEN       required. Discussions are GraphQL-only and GraphQL requires auth.
                     A fine-grained token with no extra permissions works for public repos.
  ANTHROPIC_API_KEY  optional, enables --summarize

Cron example (hourly incremental, weekly full resync to catch anything incremental missed):
  15 * * * *  cd /opt/fedramp-kb && ./venv/bin/python fedramp_discussions_sync.py --env-file .env --out ./kb --summarize --log-file sync.log
  30 3 * * 0  cd /opt/fedramp-kb && ./venv/bin/python fedramp_discussions_sync.py --env-file .env --out ./kb --summarize --full --log-file sync.log

Exit codes: 0 ok (summary failures are retried next run), 1 sync failed, 2 config error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

OWNER, REPO = "FedRAMP", "community"
GQL_URL = "https://api.github.com/graphql"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"
SCHEMA_VERSION = 1
OFFICIAL_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
OFFICIAL_LABEL = "FedRAMP Official"

log = logging.getLogger("fedramp-sync")


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- GraphQL

COMMENT_FRAGMENT = """
fragment C on DiscussionComment {
  id url body createdAt updatedAt isAnswer upvoteCount isMinimized authorAssociation
  author { login }
  replies(first: 50) {
    totalCount
    nodes { id url body createdAt updatedAt upvoteCount isMinimized authorAssociation author { login } }
  }
}
"""

DISCUSSIONS_QUERY = COMMENT_FRAGMENT + """
query($owner: String!, $repo: String!, $after: String, $pageSize: Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $repo) {
    discussions(first: $pageSize, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id number title url body createdAt updatedAt lastEditedAt
        closed closedAt locked isAnswered answerChosenAt upvoteCount authorAssociation
        author { login }
        category { name slug }
        labels(first: 20) { nodes { name } }
        comments(first: 30) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { ...C }
        }
      }
    }
  }
}
"""

MORE_COMMENTS_QUERY = COMMENT_FRAGMENT + """
query($id: ID!, $after: String) {
  rateLimit { cost remaining resetAt }
  node(id: $id) {
    ... on Discussion {
      comments(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ...C }
      }
    }
  }
}
"""


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class GitHub:
    def __init__(self, token: str, timeout: int = 60):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "User-Agent": "fedramp-kb-sync"})
        self.timeout = timeout

    def query(self, query: str, variables: dict, attempts: int = 6) -> dict:
        delay = 5
        for attempt in range(1, attempts + 1):
            try:
                r = self.s.post(GQL_URL, json={"query": query, "variables": variables}, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("GitHub request error (%s), attempt %d/%d", e, attempt, attempts)
                time.sleep(delay)
                delay = min(delay * 2, 300)
                continue

            if r.status_code == 401:
                raise ConfigError("GitHub rejected the token (401). Check GITHUB_TOKEN.")
            if r.status_code in (502, 503, 504):
                # Large nested queries occasionally time out server-side; back off and retry.
                log.warning("GitHub %d, attempt %d/%d", r.status_code, attempt, attempts)
                time.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            if r.status_code in (403, 429):
                wait = self._rate_limit_wait(r)
                if wait is None:
                    r.raise_for_status()
                log.warning("Rate limited; sleeping %ds", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()

            payload = r.json()
            errors = payload.get("errors")
            if errors:
                if any(e.get("type") == "RATE_LIMITED" for e in errors):
                    log.warning("GraphQL RATE_LIMITED; sleeping 60s")
                    time.sleep(60)
                    continue
                raise RuntimeError(f"GraphQL errors: {json.dumps(errors)[:2000]}")
            data = payload["data"]
            self._throttle(data.get("rateLimit"))
            return data
        raise RuntimeError("GitHub API: retries exhausted")

    @staticmethod
    def _rate_limit_wait(r: requests.Response) -> int | None:
        if r.headers.get("retry-after"):
            return int(r.headers["retry-after"]) + 1
        if r.headers.get("x-ratelimit-remaining") == "0" and r.headers.get("x-ratelimit-reset"):
            return max(5, int(r.headers["x-ratelimit-reset"]) - int(time.time()) + 5)
        if "secondary rate limit" in r.text.lower():
            return 60
        return None

    @staticmethod
    def _throttle(rate: dict | None) -> None:
        if not rate or rate.get("remaining", 9999) > 200:
            return
        reset = parse_ts(rate["resetAt"])
        wait = max(5, int((reset - utcnow()).total_seconds()) + 5)
        log.warning("Rate budget low (%s left); sleeping %ds until reset", rate["remaining"], wait)
        time.sleep(wait)


def fetch_remaining_comments(gh: GitHub, node_id: str, after: str) -> list[dict]:
    out = []
    while after:
        data = gh.query(MORE_COMMENTS_QUERY, {"id": node_id, "after": after})
        conn = data["node"]["comments"]
        out.extend(conn["nodes"])
        after = conn["pageInfo"]["endCursor"] if conn["pageInfo"]["hasNextPage"] else None
    return out


def iter_discussions(gh: GitHub, since: dt.datetime | None, page_size: int):
    """Yield raw discussions, most recently updated first. Stops once older than `since`."""
    after = None
    while True:
        data = gh.query(DISCUSSIONS_QUERY, {"owner": OWNER, "repo": REPO, "after": after, "pageSize": page_size})
        if not data.get("repository"):
            raise ConfigError(f"Repository {OWNER}/{REPO} not found or not accessible")
        conn = data["repository"]["discussions"]
        for d in conn["nodes"]:
            if since and parse_ts(d["updatedAt"]) < since:
                return
            pi = d["comments"]["pageInfo"]
            if pi["hasNextPage"]:
                d["comments"]["nodes"].extend(fetch_remaining_comments(gh, d["id"], pi["endCursor"]))
            yield d
        if not conn["pageInfo"]["hasNextPage"]:
            return
        after = conn["pageInfo"]["endCursor"]


# --------------------------------------------------------------------------- normalize + extract

RULE_PREFIXES = r"KSI|FR[A-Z]"  # KSI-..., FRR-..., FRC-..., FRD-... (20x consolidated-rules style IDs)
NIST_FAMILIES = "AC|AT|AU|CA|CM|CP|IA|IR|MA|MP|PE|PL|PM|PS|PT|RA|SA|SC|SI|SR"
PATTERNS = {
    "rule_ids": re.compile(rf"\b(?:{RULE_PREFIXES})-[A-Z]{{2,5}}(?:-[A-Z0-9]{{2,5}}){{0,2}}\b"),
    "rfc_ids": re.compile(r"\bRFC-\d{3,4}\b", re.I),
    "nist_controls": re.compile(rf"\b(?:{NIST_FAMILIES})-\d{{1,2}}(?:\s?\(\d{{1,2}}\))?(?![\w-])"),
    "discussion_refs": re.compile(r"github\.com/FedRAMP/community/discussions/(\d+)"),
    "urls": re.compile(r"https?://[^\s<>\"')\]]+"),
}
TOPICS = {
    "20x": r"\b20x\b",
    "rev5": r"\brev\.?\s?5\b",
    "class_a": r"\bclass a\b", "class_b": r"\bclass b\b", "class_c": r"\bclass c\b", "class_d": r"\bclass d\b",
    "conmon": r"\b(conmon|continuous monitoring)\b",
    "poam": r"\bpoa&?ms?\b",
    "3pao": r"\b(3pao|independent assessor|assessor)s?\b",
    "significant_change": r"\bsignificant change",
    "vulnerability_mgmt": r"\b(vulnerabilit(y|ies)|vdr|cve)\b",
    "trust_center": r"\btrust cent(er|re)\b",
    "marketplace": r"\bmarketplace\b",
    "oscal": r"\boscal\b",
    "machine_readable": r"\b(json|machine[- ]readable|schema)\b",
    "crypto_fips": r"\b(fips|cryptograph\w*|post[- ]quantum|quantum)\b",
    "agency_authorization": r"\b(agency (authorization|sponsor)|sponsor(ship)?)\b",
    "ksi": r"\bksis?\b",
}
TOPIC_RE = {k: re.compile(v, re.I) for k, v in TOPICS.items()}


def _login(node: dict) -> str:
    return (node.get("author") or {}).get("login") or "ghost"


class Officials:
    def __init__(self, extra_logins: set[str]):
        self.extra = {x.lower() for x in extra_logins}

    def is_official(self, node: dict) -> bool:
        login = _login(node).lower()
        return node.get("authorAssociation") in OFFICIAL_ASSOCIATIONS or login in self.extra or login.endswith("-gov")


def _post(node: dict, officials: Officials) -> dict:
    return {
        "id": node["id"],
        "url": node["url"],
        "author": _login(node),
        "author_association": node.get("authorAssociation"),
        "is_official": officials.is_official(node),
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "upvotes": node.get("upvoteCount", 0),
        "minimized": node.get("isMinimized", False),
        "body": node.get("body") or "",
    }


def extract_entities(text: str) -> dict:
    ents = {}
    for key, rx in PATTERNS.items():
        found = rx.findall(text)
        if key == "nist_controls":
            found = [f.replace(" ", "") for f in found]
        if key == "rfc_ids":
            found = [f.upper() for f in found]
        if key == "discussion_refs":
            found = [int(f) for f in found]
        ents[key] = sorted(set(found))
    ents["topics"] = sorted(k for k, rx in TOPIC_RE.items() if rx.search(text))
    return ents


def normalize(d: dict, officials: Officials) -> dict:
    comments = []
    for c in d["comments"]["nodes"]:
        post = _post(c, officials)
        post["is_answer"] = c.get("isAnswer", False)
        post["replies"] = [_post(r, officials) for r in c["replies"]["nodes"]]
        post["replies_total"] = c["replies"]["totalCount"]
        post["replies_truncated"] = c["replies"]["totalCount"] > len(post["replies"])
        comments.append(post)

    labels = sorted(n["name"] for n in d["labels"]["nodes"])
    category = (d.get("category") or {}).get("name")
    rec = {
        "schema_version": SCHEMA_VERSION,
        "source": "github_discussions",
        "repo": f"{OWNER}/{REPO}",
        "id": f"fedramp-community-discussion-{d['number']}",
        "node_id": d["id"],
        "number": d["number"],
        "title": d["title"],
        "url": d["url"],
        "category": category,
        "category_slug": (d.get("category") or {}).get("slug"),
        "labels": labels,
        "is_official_thread": OFFICIAL_LABEL in labels,
        "is_rfc": bool(category and "RFC" in category) or any(l.startswith("RFC") for l in labels),
        "author": _login(d),
        "author_is_official": officials.is_official(d),
        "created_at": d["createdAt"],
        "updated_at": d["updatedAt"],
        "last_edited_at": d.get("lastEditedAt"),
        "closed": d["closed"],
        "closed_at": d.get("closedAt"),
        "locked": d["locked"],
        "is_answered": d.get("isAnswered"),
        "answer_chosen_at": d.get("answerChosenAt"),
        "upvotes": d.get("upvoteCount", 0),
        "body": d.get("body") or "",
        "comment_count": d["comments"]["totalCount"],
        "comments": comments,
    }

    all_posts = [rec] + comments + [r for c in comments for r in c["replies"]]
    rec["answer_url"] = next((c["url"] for c in comments if c["is_answer"]), None)
    rec["participants"] = sorted({p["author"] for p in all_posts})
    rec["official_responses"] = [
        {"url": p["url"], "author": p["author"], "created_at": p["created_at"]}
        for p in comments + [r for c in comments for r in c["replies"]]
        if p["is_official"]
    ]
    rec["entities"] = extract_entities("\n".join([rec["title"]] + [p["body"] for p in all_posts]))
    return rec


def content_hash(rec: dict) -> str:
    """Hash of the substantive content only (vote counts excluded so they don't trigger re-summarizing)."""
    basis = {
        "title": rec["title"], "body": rec["body"], "category": rec["category"], "labels": rec["labels"],
        "closed": rec["closed"], "locked": rec["locked"], "answer": rec["answer_url"],
        "comments": [
            {"id": c["id"], "body": c["body"], "min": c["minimized"],
             "replies": [{"id": r["id"], "body": r["body"]} for r in c["replies"]]}
            for c in rec["comments"]
        ],
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- rendering

def _tag(p: dict) -> str:
    return " [FEDRAMP-OFFICIAL]" if p["is_official"] else ""


def thread_text(rec: dict, max_chars: int = 80_000) -> str:
    parts = [f"# {rec['title']}\nCategory: {rec['category']} | Labels: {', '.join(rec['labels']) or 'none'} | "
             f"Closed: {rec['closed']} | URL: {rec['url']}\n\n"
             f"## Original post by {rec['author']}{' [FEDRAMP-OFFICIAL]' if rec['author_is_official'] else ''}\n{rec['body']}"]
    for c in rec["comments"]:
        if c["minimized"]:
            continue
        block = f"\n\n## Comment by {c['author']}{_tag(c)}{' (MARKED ANSWER)' if c['is_answer'] else ''} ({c['url']})\n{c['body']}"
        for r in c["replies"]:
            if not r["minimized"]:
                block += f"\n\n### Reply by {r['author']}{_tag(r)} ({r['url']})\n{r['body']}"
        parts.append(block)
    text = "".join(parts)
    if len(text) > max_chars:  # keep the original post and the most recent activity
        head, tail = text[: max_chars // 4], text[-(max_chars * 3 // 4):]
        text = head + "\n\n[... middle of thread omitted for length ...]\n\n" + tail
    return text


def frontmatter(meta: dict) -> str:
    # JSON scalars/arrays are valid YAML flow syntax, so this is safe without a YAML dependency.
    return "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()) + "\n---\n"


def render_markdown(rec: dict) -> str:
    s = rec.get("llm_summary") or {}
    meta = {
        "id": rec["id"], "title": rec["title"], "url": rec["url"], "category": rec["category"],
        "labels": rec["labels"], "created": rec["created_at"], "updated": rec["updated_at"],
        "closed": rec["closed"], "is_official_thread": rec["is_official_thread"], "is_rfc": rec["is_rfc"],
        "comment_count": rec["comment_count"], "official_response_count": len(rec["official_responses"]),
        "rule_ids": rec["entities"]["rule_ids"], "rfc_ids": rec["entities"]["rfc_ids"],
        "nist_controls": rec["entities"]["nist_controls"],
        "tags": sorted(set(rec["entities"]["topics"]) | set(s.get("topics", []))),
        "status": s.get("status"), "removed_upstream": bool(rec.get("removed_upstream_at")),
    }
    out = [frontmatter(meta), f"# {rec['title']}\n", f"Source: {rec['url']}\n"]
    if s:
        out.append(f"\n## Summary\n{s.get('summary', '')}\n")
        for key, heading in [("key_points", "Key points"), ("open_questions", "Open questions")]:
            if s.get(key):
                out.append(f"\n### {heading}\n" + "\n".join(f"- {x}" for x in s[key]) + "\n")
        if s.get("fedramp_positions"):
            out.append("\n### FedRAMP positions stated in thread\n" + "\n".join(
                f"- {p.get('statement')} ([source]({p.get('url')}))" for p in s["fedramp_positions"]) + "\n")
    out.append(f"\n## Original post — {rec['author']}{' (FedRAMP)' if rec['author_is_official'] else ''}, "
               f"{rec['created_at']}\n\n{rec['body']}\n")
    for c in rec["comments"]:
        if c["minimized"]:
            continue
        badge = " (FedRAMP)" if c["is_official"] else ""
        ans = " ✅ answer" if c["is_answer"] else ""
        out.append(f"\n## {c['author']}{badge}{ans} — [{c['created_at']}]({c['url']})\n\n{c['body']}\n")
        for r in c["replies"]:
            if not r["minimized"]:
                rb = " (FedRAMP)" if r["is_official"] else ""
                out.append(f"\n> **{r['author']}{rb}** — [{r['created_at']}]({r['url']})\n>\n"
                           + "\n".join("> " + line for line in r["body"].splitlines()) + "\n")
        if c["replies_truncated"]:
            out.append(f"\n> _{c['replies_total'] - len(c['replies'])} more replies on GitHub_\n")
    return "".join(out)


def build_chunks(rec: dict) -> list[dict]:
    base = {"discussion_id": rec["id"], "discussion_number": rec["number"], "discussion_title": rec["title"],
            "category": rec["category"], "labels": rec["labels"], "closed": rec["closed"],
            "removed_upstream": bool(rec.get("removed_upstream_at"))}
    chunks = [{**base, "chunk_id": f"{rec['id']}#post", "kind": "post", "url": rec["url"],
               "author": rec["author"], "is_official": rec["author_is_official"], "created_at": rec["created_at"],
               "text": f"{rec['title']}\n\n{rec['body']}", "entities": extract_entities(rec["title"] + "\n" + rec["body"])}]
    for c in rec["comments"]:
        if c["minimized"]:
            continue
        text = f"Re: {rec['title']}\n\n{c['author']}{_tag(c)}: {c['body']}"
        for r in c["replies"]:
            if not r["minimized"]:
                text += f"\n\n{r['author']}{_tag(r)}: {r['body']}"
        chunks.append({**base, "chunk_id": f"{rec['id']}#{c['id']}", "kind": "comment_thread", "url": c["url"],
                       "author": c["author"], "is_official": c["is_official"],
                       "has_official_reply": c["is_official"] or any(r["is_official"] for r in c["replies"]),
                       "is_answer": c["is_answer"], "created_at": c["created_at"], "text": text,
                       "entities": extract_entities(text)})
    return chunks


# --------------------------------------------------------------------------- LLM summary

SUMMARY_SYSTEM = """You summarize threads from FedRAMP's public GitHub community forum for a compliance knowledge base.
Posts tagged [FEDRAMP-OFFICIAL] were written by FedRAMP staff; everything else is community input.
Return ONLY a JSON object (no prose, no code fences) with exactly these keys:
  "summary": 2-4 sentence plain-English summary,
  "thread_type": one of "announcement", "rfc", "rfc_feedback", "question", "proposal", "issue_report", "other",
  "status": one of "open", "answered", "resolved", "closed", "informational",
  "key_points": array of short strings,
  "fedramp_positions": array of {"statement": str, "url": str} — ONLY statements made by [FEDRAMP-OFFICIAL] posts,
  "open_questions": array of unresolved questions,
  "affected_parties": subset of ["CSP", "3PAO", "Agency", "FedRAMP", "Vendor", "Other"],
  "topics": array of short lowercase tags.
Use only what is in the thread. Do not speculate or add outside knowledge."""


def summarize(rec: dict, api_key: str, model: str, attempts: int = 4) -> dict:
    body = {"model": model, "max_tokens": 2000, "system": SUMMARY_SYSTEM,
            "messages": [{"role": "user", "content": thread_text(rec)}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    delay = 10
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=180)
        except requests.RequestException as e:
            log.warning("Anthropic request error (%s), attempt %d/%d", e, attempt, attempts)
            time.sleep(delay); delay *= 2
            continue
        if r.status_code in (429, 500, 502, 503, 529):
            wait = int(r.headers.get("retry-after", delay))
            log.warning("Anthropic %d; retrying in %ds", r.status_code, wait)
            time.sleep(wait); delay *= 2
            continue
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        return json.loads(text)
    raise RuntimeError("Anthropic API: retries exhausted")


# --------------------------------------------------------------------------- storage

def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS discussions(
        number INTEGER PRIMARY KEY, node_id TEXT, updated_at TEXT,
        content_hash TEXT, summary_hash TEXT, last_seen TEXT, removed_at TEXT)""")
    db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    db.commit()
    return db


def meta_get(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(db, key, value):
    db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def write_record(out: Path, rec: dict) -> None:
    atomic_write(out / "records" / f"{rec['number']}.json", json.dumps(rec, indent=2, ensure_ascii=False))
    atomic_write(out / "markdown" / f"{rec['number']}.md", render_markdown(rec))


def load_record(out: Path, number: int) -> dict | None:
    p = out / "records" / f"{number}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def rebuild_indexes(out: Path) -> int:
    index_lines, chunk_lines = [], []
    for p in sorted((out / "records").glob("*.json"), key=lambda x: int(x.stem)):
        rec = json.loads(p.read_text(encoding="utf-8"))
        s = rec.get("llm_summary") or {}
        index_lines.append(json.dumps({
            "id": rec["id"], "number": rec["number"], "title": rec["title"], "url": rec["url"],
            "category": rec["category"], "labels": rec["labels"], "created_at": rec["created_at"],
            "updated_at": rec["updated_at"], "closed": rec["closed"], "is_official_thread": rec["is_official_thread"],
            "is_rfc": rec["is_rfc"], "comment_count": rec["comment_count"],
            "official_response_count": len(rec["official_responses"]), "entities": rec["entities"],
            "summary": s.get("summary"), "status": s.get("status"), "thread_type": s.get("thread_type"),
            "markdown_path": f"markdown/{rec['number']}.md", "removed_upstream": bool(rec.get("removed_upstream_at")),
        }, ensure_ascii=False))
        chunk_lines.extend(json.dumps(c, ensure_ascii=False) for c in build_chunks(rec))
    atomic_write(out / "index.jsonl", "\n".join(index_lines) + "\n")
    atomic_write(out / "chunks.jsonl", "\n".join(chunk_lines) + "\n")
    return len(index_lines)


def log_changes(out: Path, changes: list[dict]) -> None:
    if not changes:
        return
    path = out / "changes" / f"{utcnow():%Y-%m-%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for c in changes:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- main sync

def sync(args) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ConfigError("GITHUB_TOKEN is not set")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if args.summarize and not api_key:
        raise ConfigError("--summarize requires ANTHROPIC_API_KEY")

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    db = open_db(out / "state.sqlite")
    gh = GitHub(token)
    officials = Officials(set(args.official_login or []))
    run_at = utcnow().isoformat()

    high_water = parse_ts(meta_get(db, "high_water"))
    since = None if (args.full or not high_water) else high_water - dt.timedelta(days=args.lookback_days)
    log.info("Starting %s sync (since=%s)", "full" if since is None else "incremental", since)

    changes, seen, stats = [], set(), {"new": 0, "updated": 0, "unchanged": 0, "removed": 0}
    max_updated = high_water

    for raw in iter_discussions(gh, since, args.page_size):
        rec = normalize(raw, officials)
        seen.add(rec["number"])
        h = content_hash(rec)
        ts = parse_ts(rec["updated_at"])
        max_updated = ts if not max_updated or ts > max_updated else max_updated
        row = db.execute("SELECT content_hash, summary_hash FROM discussions WHERE number=?", (rec["number"],)).fetchone()

        if row and row[0] == h and not args.rebuild:
            stats["unchanged"] += 1
            db.execute("UPDATE discussions SET last_seen=?, updated_at=?, removed_at=NULL WHERE number=?",
                       (run_at, rec["updated_at"], rec["number"]))
            continue

        # Carry over an existing summary if the content it was generated from hasn't changed (--rebuild case).
        if row and row[1] == h:
            old = load_record(out, rec["number"]) or {}
            rec["llm_summary"], rec["llm_model"] = old.get("llm_summary"), old.get("llm_model")
        rec["content_hash"], rec["synced_at"] = h, run_at
        write_record(out, rec)

        kind = "new" if not row else ("updated" if row[0] != h else "rebuilt")
        db.execute("""INSERT INTO discussions(number, node_id, updated_at, content_hash, summary_hash, last_seen, removed_at)
                      VALUES(?, ?, ?, ?, ?, ?, NULL)
                      ON CONFLICT(number) DO UPDATE SET node_id=excluded.node_id, updated_at=excluded.updated_at,
                        content_hash=excluded.content_hash, last_seen=excluded.last_seen, removed_at=NULL""",
                   (rec["number"], rec["node_id"], rec["updated_at"], h, None, run_at))
        db.commit()
        if kind != "rebuilt":
            stats[kind] += 1
            changes.append({"run_at": run_at, "change": kind, "number": rec["number"], "title": rec["title"],
                            "url": rec["url"], "category": rec["category"], "updated_at": rec["updated_at"],
                            "comment_count": rec["comment_count"]})
            log.info("%s #%d %s", kind.upper(), rec["number"], rec["title"])

    # A full scan is the only safe time to detect discussions deleted/moved upstream.
    if since is None:
        for (number,) in db.execute("SELECT number FROM discussions WHERE removed_at IS NULL").fetchall():
            if number not in seen:
                rec = load_record(out, number)
                if rec:
                    rec["removed_upstream_at"] = run_at
                    write_record(out, rec)
                db.execute("UPDATE discussions SET removed_at=? WHERE number=?", (run_at, number))
                stats["removed"] += 1
                changes.append({"run_at": run_at, "change": "removed", "number": number,
                                "title": rec["title"] if rec else None})
                log.info("REMOVED #%d", number)

    if max_updated:
        meta_set(db, "high_water", max_updated.isoformat())
    meta_set(db, "last_success", run_at)
    db.commit()
    log_changes(out, changes)

    # Summaries run after the fetch so an LLM outage never blocks the sync. Anything that fails here
    # keeps summary_hash != content_hash and is retried automatically on the next run.
    sum_ok = sum_fail = 0
    if args.summarize:
        pending = db.execute("""SELECT number, content_hash FROM discussions
                                WHERE removed_at IS NULL AND (summary_hash IS NULL OR summary_hash != content_hash)
                                ORDER BY updated_at DESC LIMIT ?""", (args.max_summaries,)).fetchall()
        for number, h in pending:
            rec = load_record(out, number)
            if not rec:
                continue
            try:
                rec["llm_summary"] = summarize(rec, api_key, args.model)
                rec["llm_model"] = args.model
                write_record(out, rec)
                db.execute("UPDATE discussions SET summary_hash=? WHERE number=?", (h, number))
                db.commit()
                sum_ok += 1
            except Exception as e:  # noqa: BLE001 - keep going; retried next run
                sum_fail += 1
                log.warning("Summary failed for #%d: %s", number, e)

    total = rebuild_indexes(out)
    log.info("Done: %s | summaries ok=%d failed=%d | %d discussions indexed", stats, sum_ok, sum_fail, total)
    return 0


def load_env_file(path: str) -> None:
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ping(url: str | None, suffix: str = "") -> None:
    if url:
        try:
            requests.get(url.rstrip("/") + suffix, timeout=10)
        except requests.RequestException:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="./kb", help="output directory (default ./kb)")
    ap.add_argument("--full", action="store_true", help="scan every discussion (also detects removals)")
    ap.add_argument("--rebuild", action="store_true", help="rewrite all files even if unchanged (after schema/format changes)")
    ap.add_argument("--lookback-days", type=int, default=3, help="incremental overlap window (default 3)")
    ap.add_argument("--page-size", type=int, default=15, help="discussions per GraphQL page (default 15)")
    ap.add_argument("--summarize", action="store_true", help="generate LLM summaries via the Anthropic API")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-summaries", type=int, default=25, help="cap on summaries per run (cost control)")
    ap.add_argument("--official-login", action="append", help="extra GitHub logins to treat as FedRAMP staff")
    ap.add_argument("--env-file", help="load KEY=VALUE pairs (cron doesn't load your shell env)")
    ap.add_argument("--log-file")
    ap.add_argument("--healthcheck-url", help="pinged on success, <url>/fail on failure (e.g. healthchecks.io)")
    args = ap.parse_args()

    handlers = [logging.FileHandler(args.log_file)] if args.log_file else [logging.StreamHandler()]
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")
    if args.env_file:
        load_env_file(args.env_file)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lock = open(out / ".sync.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another sync is already running; exiting")
        return 0

    try:
        code = sync(args)
        ping(args.healthcheck_url)
        return code
    except ConfigError as e:
        log.error("Config error: %s", e)
        ping(args.healthcheck_url, "/fail")
        return 2
    except Exception:  # noqa: BLE001
        log.exception("Sync failed")
        ping(args.healthcheck_url, "/fail")
        return 1


if __name__ == "__main__":
    sys.exit(main())
