"""The deep-agent investigator, driven by a fake LangChain chat model."""
import tempfile

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from conftest import reset
from worker.agent.investigator import investigate
from worker.agent.state import RunState
from worker.config import Settings
from worker.environment import load_environment


class FakeToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _Agent:
    """Just the parts of the engine the investigator touches."""

    def __init__(self, base, model):
        from worker.gate import Gate
        from worker.tools.browser import BrowserSession
        from worker.tools.toolbox import Toolbox
        self.state = RunState(task="t")
        env = load_environment(Settings().environment_file, base)
        self.env_text = env.describe()
        self.gate = Gate(env)
        self.gate.read_only = True
        wd = tempfile.mkdtemp()
        self.browser = BrowserSession(self.gate, env.vault, wd)
        self.tools = Toolbox(self.state, env, self.gate, self.browser, wd)
        self.chat_model = model

    async def emit(self, *a, **k):
        pass

    def add_usage(self, u):
        pass


def _call(name, args, i):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{i}", "type": "tool_call"}])


async def _run(sandbox, evidence):
    reset(sandbox, seed=31)
    model = FakeToolModel(responses=[
        _call("browser_goto", {"reason": "look", "url": f"{sandbox}/acme/drive/"}, 1),
        _call("submit_verdict", {"passed": True, "evidence": evidence}, 2),
        AIMessage(content="done"),
    ])
    a = _Agent(sandbox, model)
    await a.browser.start()
    try:
        return await investigate(a, {"id": "s1", "text": "the policy file exists"}, "check the drive")
    finally:
        await a.browser.close()


async def test_investigator_pass_needs_grounded_evidence(sandbox):
    ok = await _run(sandbox, "AP_Policy.pdf")
    assert ok["status"] == "pass", ok
    made_up = await _run(sandbox, "a quote that was never on any page")
    assert made_up["status"] == "unknown", made_up
