# Architecture

This document explains how a task moves through the system and why each piece is there. The short version is in the README.

## The pieces

```
              ┌────────────────────────── worker (generic) ───────────────────────────┐
 task ──> API/CLI ──> RunManager ──> Agent graph ──> Toolbox ──> BrowserSession ──┐    │
              │            │            │  intake        │        (Playwright)     │    │
              │      events/SSE         │  act  <─ guard  │                         ▼    │
              │      replies            │  verify         ├──> file reader      Gate ───┼──> sandbox company
              │                         │  report         └──> HTTP client ──────┘     │   (separate process)
              └──────────── config/environment.yaml (apps, secrets, policy) ───────────┘
```

The sandbox runs as its own process on its own port. The agent reaches it over real HTTP through a real browser, exactly like it would reach any internal tool. The API server also reverse-proxies `/acme/*` so one public port can show both.

## Lifecycle of a run

### 1. Intake

One model call turns the request into a task contract:

```json
{
  "goal": "The newest invoice from Globex is recorded as a bill in the ERP and the user is told",
  "facts_needed": [{"key": "amount", "description": "total payable incl. tax"}, ...],
  "success_criteria": [{"id": "s1", "text": "exactly one bill exists with that invoice number, amount and due date"}],
  "checklist": ["find the newest invoice", "extract the values", "check it isn't already entered", ...],
  "assumptions": ["latest means most recent invoice date"],
  "blocking_questions": []
}
```

Why: it makes the agent commit up front to what "done" means. That is what verification checks against later, and it is hard to move the goalposts once they are written down. `facts_needed` also tells the agent which values to record, and with which keys, so they can be checked against their source.

Blocking questions are only for things that can't be found in the systems. Ambiguity that looking can resolve goes into assumptions. Mid-tier models tend to over-ask, so the prompt pushes the other way.

### 2. Act loop

Each step the prompt is rebuilt from state:

```
TASK / GOAL / user clarifications
CHECKLIST            [x] c1 ...  [ ] c2 ...
FACTS NEEDED         amount (saved), due_date (MISSING)
WORKING MEMORY       amount = 4107.41 [source: files/INV-2889.pdf]
SUPERVISOR NOTES     (from the guard, if any)
RECENT STEPS         one line per step: tool(args) -> ok/FAILED: what happened
BUDGET               step 9 of 45
LATEST OBSERVATION   the full result of the last action
```

The model calls exactly one tool. Every tool takes a `reason`, which shows up in the trace and the UI.

Stateless prompting keeps the prompt around 2k tokens for the whole run, avoids replaying provider-specific chat history (Gemini's thought signatures, for example), and makes every decision reproducible from the saved state.

After a memory or plan update the observation would be tiny, so the last page or document stays in view. That way the agent doesn't need to reopen a PDF just to record its second value.

### 3. Tools

| Tool | Notes |
|---|---|
| `browser_goto(url)` | Full URL or path. `back` goes back. GETs retry once automatically. |
| `browser_click(element)` | `[n]` from the latest view, or visible text. Refuses with a clear message if an overlay covers the element. |
| `browser_fill_form(fields, submit)` | Keys are labels (fuzzy) or `[n]`. Handles selects and checkboxes. Clicks the form's own submit button. |
| `browser_read(part, find)` | Paging and search on long pages. |
| `read_file(path)` | PDFs (pypdf), CSV, text from the run workspace. Downloads land there automatically. |
| `http_request(method, url, body, headers)` | Shares the browser's cookies. Same gate as the browser. |
| `remember(facts, source, quote)` | Rejected unless `quote` appears in something the agent observed. |
| `update_plan(done, add, note)` | Tick milestones, add new ones, write a diagnosis. |
| `ask_user(question, kind, options)` | Pauses the run until the user answers. |
| `notify_user(message)` | Fire-and-forget message to the user. |
| `finish(status, summary, result)` | A claim. Verification decides whether it holds. |

### 4. The page view

`worker/tools/distill.js` runs inside the page and returns something like:

```
URL: http://127.0.0.1:8100/acme/erp/bills/new | Title: New bill - AcmeERP | HTTP 200
# Enter a bill
[6] select "Vendor" selected="-- choose vendor --" options=[Cyberdyne Components | Massive Dynamic | ...]
[7] textbox "Memo optional" value=""
[8] textbox "Invoice number exactly as printed on the supplier invoice" value=""
[9] textbox "Amount numbers only, e.g. 1234.56" value=""
[10] textbox "Invoice date YYYY-MM-DD" value=""
*[13] button "Save bill" (BLOCKED: covered by overlay "What's new in AcmeERP 4.2")
```

- Labels include the hint text next to a field, which is how the agent learns the date format.
- A `*` marks elements that are new since the last view on the same page.
- `BLOCKED: covered by overlay` comes from `elementFromPoint`, so the agent knows to dismiss the dialog instead of clicking into it for 30 seconds.
- Text from pages and files is wrapped in an "untrusted content" envelope.
- Secret values are scrubbed and replaced with their placeholders.

### 5. Guard (code, every step)

- An ambiguous write failure (5xx or network error on a POST) produces a note: check whether it landed before retrying.
- The same tool with the same arguments 3 times in the last 6 steps produces a note: stop repeating, diagnose, change approach.
- Every 3rd consecutive failure produces a note: re-read, check hints, try another path, write a diagnosis first.
- No progress for 10 steps (same page, no new facts, no plan update) produces a note: step back.
- 4 stalls produce a note: ask the user for help or finish as blocked.
- Budgets: max steps and max minutes. Hitting one ends the run as incomplete, and it goes through verification and reporting like any other run.

This follows Magentic-One's stall counter, but in code instead of an extra model call per step.

### 6. Gate (code, every request)

Browser traffic goes through `context.route("**/*")`, and HTTP tool calls call the same `Gate` directly.

- **Origins and paths.** Only origins in the manifest are allowed. `denied_paths` blocks pages like the sandbox admin, which holds the ground truth.
- **Policy rules.** These come from the manifest, for example:
  ```yaml
  - id: high_value_entry
    match: {url: "*/acme/erp/*bills*"}
    when: {field: "amount|total|payable", gte: 10000}
    action: require_approval
  ```
  Form fields are mapped from their HTML names to visible labels before matching. That matters here because the ERP randomizes field names per seed. An approval covers that rule plus those values only: approving 12,480.50 does not approve 50,000.
- **Approval flow.** A blocked navigation is answered with HTTP 204, so the browser stays on the filled form. The run pauses with the exact action. On approve, the grant is stored and the agent is told to resubmit. On deny, any identical attempt is refused from then on.
- **Duplicate guard.** Every write is recorded with a fingerprint (method, path, normalized fields). An identical write after a success or an unclear failure is refused until the agent has read from that same app again. This is the "did it land?" check, forced in code.
- **Read-only mode** during verification. **Dry run** records writes without sending them.

### 7. Verify

This runs when the agent calls `finish` with status completed, or after the budget runs out.

1. **Source fidelity.** Facts are grouped by the observation their quote came from. A fresh model call gets the raw source text and the field descriptions, but not the recorded values. Code compares the results with `normalize.same` (money, several date formats, ambiguous numeric dates read both ways, text).
2. **Probe plan.** A fresh model call gets the contract, the facts and the manifest, but not the worker's steps. It writes checks such as:
   ```json
   {"criterion": "s1", "type": "api_record",
    "url": ".../erp/api/bills?invoice_number={{fact:invoice_number}}",
    "expect": {"amount": "{{fact:amount}}", "due_date": "{{fact:due_date}}"}, "count": "exactly_one"}
   ```
   Code fills in the fact references, runs a GET and matches records. Other check types: `page_text`, `user_notified`, and `judge` as a last resort.
3. **Judge.** This is a read-only mini agent. A "pass" counts only if its evidence quote appears in something it observed during its own investigation.
4. **Ledger.** Writes, their outcomes, unresolved failures, and identical successful writes.

The outcome is one of `verified`, `partially_verified`, `unverified` (a check couldn't run), `failed`, or `needs_attention` (the worker finished as blocked). On failure, the problems go back to the act loop as a supervisor note, with at most `MAX_REPAIR_ROUNDS` (2) rounds.

### 8. Report

The report is built in code from state, so it can't contradict verification. It contains:

- the headline and the worker's summary
- per-criterion verdicts and source checks
- facts with their quotes
- every write
- approvals and questions
- assumptions
- screenshots of key steps
- stats: steps, failed and recovered actions, repair rounds, LLM calls, tokens, duration

## The sandbox

| App | What's in it |
|---|---|
| Mail | About 25 emails, with real PDF attachments in 3 different layouts. There is a reminder about an older invoice that arrives after the newest one, an onboarding email with a registration form PDF, a fraud email with a prompt injection, and noise. Search, compose and send. JSON API. |
| ERP | Login with sessions. Vendors (masked bank details, bank change form), bills list with search and pagination, bill entry form with strict validation (date format per seed, plain-number amounts, due after invoice date, duplicate supplier invoice numbers rejected). Labels, field order and field names are randomized per seed. JSON API with bearer token or session. |
| Supplier portal | Login-gated invoice list with PDF downloads, for a vendor who stopped emailing invoices. |
| Drive | AP policy PDF (approval threshold, bank change rules, no duplicates), finance contacts CSV, a budget CSV. |
| Admin | World editor (seed and chaos toggles) and ground truth. Blocked for the agent. |

Chaos modes:

- `transient_503`: not saved
- `ambiguous_502`: saved, then reports an error
- `lying_ui`: shows success, saves nothing
- `modal`: overlay blocks the page
- `session_expiry`
- `slow`

## Swapping the environment

To point the worker at different software, write a new manifest:

- apps: name, URL, a one-line description, and an optional login and API notes
- secrets: env var names and allowed origins
- network: allowed origins and denied paths
- policy: rules

Then set `WORKER_ENVIRONMENT=path/to/it.yaml`. Nothing under `worker/` changes, and `tests/test_generalization.py` fails the build if domain words leak into agent code.
