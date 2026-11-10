"""AgentDojo tool outputs for bench/guard_public.py: benign ones, and the same calls with an attack planted.

Runs in its own environment (AgentDojo pins its own dependencies), called by `guard_public.py build`:

    uv run --no-project --python 3.12 --with agentdojo==0.1.34 python -I bench/guard_public_agentdojo.py OUT.jsonl

No model is called. Each user task's ground-truth tool calls are replayed (after lzwhehe/benign-instruction-bench,
MIT): once in the default environment, which gives the benign outputs, and once per attack with AgentDojo's own
attack text in the task's injection vectors. A call's output is injected when the attack's text is in it: at
least half of the attack's six-word runs, compared as words, since the output is YAML that folds and escapes
it. Differing from the benign output is not enough: some outputs carry the time of the run. Which injection
task goes with a user task is fixed by a CRC of the names, so the file is the same on every run.
"""

from __future__ import annotations

import json
import re
import sys
import zlib

from agentdojo.agent_pipeline.tool_execution import tool_result_to_str
from agentdojo.attacks.attack_registry import load_attack
from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.task_suite import get_suite

VERSION = "v1.2"
SUITES = ("workspace", "travel", "banking", "slack")
# AgentDojo's attacks, from blunt to the ones written in the voice of the user or the tool.
ATTACKS = ("direct", "ignore_previous", "system_message", "injecagent", "important_instructions",
           "important_instructions_no_names", "tool_knowledge")


class _Target:  # the attacks read only the model's name, to address it in their text
    name = "gpt-4o-2024-05-13"


def _run(runtime, env, call) -> str:
    try:
        result, err = runtime.run_function(env, call.function, dict(call.args))
    except Exception as e:  # noqa: BLE001  a ground-truth call that fails is an error message the agent reads
        result, err = None, str(e)
    return tool_result_to_str(result) if err is None else f"Error: {err}"


def _runs(text: str, n: int = 6) -> set[tuple[str, ...]]:
    words = re.findall(r"[A-Za-z0-9]+", text.replace("\\n", " ").lower())
    return {tuple(words[i:i + n]) for i in range(max(1, len(words) - n + 1))}


def _carries(text: str, injections: dict[str, str]) -> bool:
    have = _runs(text)
    return any(len(want & have) >= len(want) / 2 for want in map(_runs, injections.values()))


def main(out_path: str) -> None:
    out, seen = open(out_path, "w"), set()

    def write(**rec) -> None:
        if rec["text"] not in seen:
            seen.add(rec["text"])
            out.write(json.dumps(rec) + "\n")

    for name in SUITES:
        suite = get_suite(VERSION, name)
        injection_tasks = sorted(suite.injection_tasks.items())
        for task_id, task in suite.user_tasks.items():
            clean = suite.load_and_inject_default_environment({})
            runtime = FunctionsRuntime(suite.tools)
            try:
                calls = task.ground_truth(clean.model_copy(deep=True))
            except Exception:  # noqa: BLE001
                continue
            for k, call in enumerate(calls):
                write(label=0, suite=name, task=task_id, step=k, function=call.function, text=_run(runtime, clean, call))
            for attack_name in ATTACKS:
                attack = load_attack(attack_name, suite, _Target())
                try:
                    attack.get_injection_candidates(task)
                except ValueError:
                    continue  # no tool output of this task carries an injection vector
                inj_id, inj_task = injection_tasks[zlib.crc32(f"{name}/{task_id}".encode()) % len(injection_tasks)]
                injections = attack.attack(task, inj_task)
                env = suite.load_and_inject_default_environment(injections)
                runtime = FunctionsRuntime(suite.tools)
                for k, call in enumerate(task.ground_truth(env.model_copy(deep=True))):
                    text = _run(runtime, env, call)
                    if _carries(text, injections):
                        write(label=1, suite=name, task=task_id, step=k, function=call.function, text=text,
                              attack=attack_name, injection_task=inj_id)
    out.close()
    print(f"agentdojo: {len(seen)} distinct tool outputs")


if __name__ == "__main__":
    main(sys.argv[1])
