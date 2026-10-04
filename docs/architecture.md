# Architecture

This document explains how a task moves through the system and why each piece is there. The short version is in the README.

## The pieces

```
 task -> console / API / CLI -> RunManager -> LangGraph graph ----> Toolbox -> BrowserSession (Playwright) --+
                                   |           intake               |         file reader                    |
                             events (SSE)      act <-> human         |         HTTP client -------------------+--> Gate --> sandbox company
                             replies           verify -> investigator (Deep Agents, read only)                     (separate process or embedded)
                                               report
                  config/environment.yaml: apps, secrets, network rules, policy
```

The sandbox is a separate FastAPI app. Locally it runs as its own process. On small hosts it is mounted into the same process (`EMBED_SANDBOX=1`), and then the agent is additionally kept out of the worker's own `/api`, `/docs` and `/static` paths. Either way the agent reaches it over real HTTP through a real browser, exactly like any internal tool.

## The graph

`worker/agent/engine.py` builds a LangGraph `StateGraph`:

```
START -> intake --(questions?)--> human --> act
           |                                ^  |
           +------------> act <-------------+  +--(finish)--> verify --(failed, < 2 rounds)--> act
                           ^ |                                  |
                           | +--(ask_user / approval)--> human   +--> report --> END
```

- **The graph state is two keys.** `route` names the next node; `pending` holds the question waiting for a human. Everything else lives in `RunState`, which is written to `state.json` after every step. Keeping the graph state tiny makes routing obvious, and keeps the big state in one place that the UI, the verifier and the report all read.
- **`human` calls `interrupt(pending)`.** The driver in `Agent.run` sees `__interrupt__` and shows the question (an SSE event, the approval sheet in the UI). It waits for `/reply` and resumes with `Command(resume=answer)`. On resume the node runs again from the top and `interrupt()` returns the answer. That is why nothing with side effects happens before the `interrupt()` call.
- **Checkpointing.** `MemorySaver` keeps one thread per run, with thread id = run id. A durable saver is the obvious next step: see "What I would build next" in the README.
- **One tool call per `act` superstep.** The recursion limit is set from `MAX_STEPS`.

## Models

`worker/llm/langchain_llm.py` wraps any LangChain chat model behind the small `chat()` interface the act loop uses. ChatOllama is the default:

- with `OLLAMA_API_KEY` it talks to `https://ollama.com` using a bearer token
- without a key it uses `OLLAMA_BASE_URL`, i.e. a local server

Tool calls use `bind_tools`, and JSON-mode calls use `format="json"`. If a model refuses native tools, the adapter switches to text tool calls and adds the tool catalogue to the prompt. The same `ChatOllama` object is handed to the Deep Agents investigator.

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
- For grounding (`remember` quotes, page checks, investigator evidence), input values, selected options and link targets are stripped out. So whatever the agent typed, or a search box echoing its own query, can never count as evidence.
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

- **Canonical URLs.** Every URL is percent-decoded, its doubled slashes collapsed and its dot segments resolved before any rule runs, so `/acme/%61dmin` is `/acme/admin`. The browser re-checks where a navigation actually landed, because redirect hops never reach the route handler. The HTTP tool follows redirects by hand and checks every hop.
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
- **Sign-ins.** A request that carries a vault credential is a sign-in, not a data change. It is allowed even in dry run, during verification and on information-only tasks, and it is never recorded as a write.
- **Information-only tasks.** If intake decides the user only asked a question, any data change needs explicit approval.
- **Read-only mode** during verification. **Dry run** records writes without sending them.
- **Secret hygiene.** Values in the write ledger, tool errors and verifier messages pass through the vault scrubber.

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
3. **Investigator.** This handles criteria that code cannot probe. It is a LangChain Deep Agent (`create_deep_agent`) with only the read tools plus `submit_verdict`, and it gets the built-in planner, scratchpad and sub-agents for free. A "pass" counts only if its evidence quote appears in something it observed during its own investigation. Without a LangChain model (for example in scripted runs), a small built-in read-only loop does the same job.
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

## The console

`worker/web/` is a framework-free web app, served by the API at `/`. Every screen is built from the run's event stream:

- **One reducer, live or replayed.** Live runs stream over SSE. Finished runs from earlier server processes are replayed from their `events.jsonl`, so the same code renders both.
- **Ledger.** One row per step: the time, the model's own reason as the title, and the exact action in mono underneath. Clicking a row shows its screenshot and observation.
- **Evidence pane.** Four tabs: Screen (browser frame with prev and next), Facts (value, verbatim quote, blind re-read result), Checks (criteria and probe results), and Changes (the gate's write ledger, approvals, assumptions).
- **Approval sheet.** It shows the policy rule and exactly the labelled fields the gate saw. `A` approves and `D` denies. Clarifications use the same sheet, with the options as buttons.
- **Composer.** Example tasks come from the current seeded world, plus controls to rebuild the company with a seed and chaos modes.

Design tokens are at the top of `app.css`. The direction is a graphite surface ladder with hairline borders, one cobalt accent used only for the main action, green, amber and red only for meaning, and IBM Plex Sans and Mono.
