"""The agent must not contain knowledge of the sandbox company or its domain.
Everything specific has to come from the environment manifest or the task."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_CODE = ["worker/agent", "worker/tools", "worker/llm", "worker/gate.py", "worker/environment.py",
              "worker/service.py", "worker/config.py"]
FORBIDDEN = re.compile(r"acme|invoice|\berp\b|vendor|\bbills?\b|supplier|payable|accounts payable", re.I)


def _files():
    for entry in AGENT_CODE:
        p = os.path.join(ROOT, entry)
        if os.path.isfile(p):
            yield p
        else:
            for d, _, names in os.walk(p):
                for n in names:
                    if n.endswith((".py", ".js")):
                        yield os.path.join(d, n)


def test_agent_code_has_no_domain_knowledge():
    hits = []
    for path in _files():
        with open(path) as f:
            for i, line in enumerate(f, 1):
                if FORBIDDEN.search(line):
                    hits.append(f"{os.path.relpath(path, ROOT)}:{i}: {line.strip()}")
    assert not hits, "domain specific words found in agent code:\n" + "\n".join(hits)
